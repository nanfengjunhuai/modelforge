"""OpenAI 兼容协议的 Provider 实现 —— 本项目的主力实现。

覆盖：DeepSeek、通义千问、Kimi、Ollama、vLLM、以及任何实现了
`/v1/chat/completions` 的服务。它们之间只有 base_url 和 model 名不同。

────────────────────────────────────────────────────────────────────────
关于「不重试」
────────────────────────────────────────────────────────────────────────
`AsyncOpenAI(max_retries=N)` 的重试**只覆盖连接建立阶段**的失败
（DNS 解析失败、握手超时、429/5xx 在首个字节之前返回）。
流一旦开始吐字节，SDK 就不再重试了 —— 这与我们「流开始后不重试」的决策一致。

我们自己也不额外重试：把每次失败都变成一个 `ErrorEvent` 交给上层。
"""

from __future__ import annotations

import logging
from typing import Any

import openai
from openai import AsyncOpenAI

from modelforge.providers.base import Message, ToolSpec
from modelforge.providers.events import (
    ErrorEvent,
    Finish,
    FinishReason,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    Usage,
)

logger = logging.getLogger(__name__)

__all__ = ["OpenAICompatProvider", "chunk_to_events", "classify_finish_reason"]

# 厂商取值 → 我们的事件模型取值。
#
# 用一张显式的映射表而不是 set + 分支判断，是因为：
#   · 表本身就是文档，一眼能看全所有已知取值及其去向；
#   · 类型标注 dict[str, FinishReason] 让 mypy 帮你验证「表里的每个目标值都合法」；
#   · 加新厂商时只需加一行，不会漏改任何分支。
_FINISH_MAP: dict[str, FinishReason] = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "length": "length",
    "content_filter": "content_filter",
    "error": "error",
    # SDK 类型里还留着这个废弃的旧协议取值，折叠成等价的现代写法
    "function_call": "tool_calls",
}


def classify_finish_reason(raw: str | None) -> FinishReason:
    """把厂商给的 finish_reason 折叠到我们的取值集合里。

    为什么要折叠而不是原样透传？因为不同兼容服务的取值集合有细微差异
    （比如 SDK 里还留着废弃的 `function_call`，有些服务会返回 `null`）。
    让非法值穿透到上层的 Literal 字段会直接触发 Pydantic 校验失败，
    把「一个不认识的结束原因」升级成「整个流崩掉」—— 不值得。

    未知取值一律按 "stop" 处理：结束就是结束，原因不认识不影响后续流程。
    """
    if raw is None:
        return "stop"
    return _FINISH_MAP.get(raw, "stop")


def chunk_to_events(chunk: Any) -> list[StreamEvent]:
    """把一个 OpenAI 流式 chunk 映射成零到多个归一化事件。

    **这是一个纯函数** —— 不碰网络、不发请求、没有副作用。
    这么设计是为了可测：单元测试可以直接构造 chunk 对象喂进来断言输出，
    不需要 API Key，也不需要联网。

    一个 chunk 为什么可能产出「多个」事件？因为同一次增量里可能同时包含
    文本内容和工具调用碎片（模型一边说话一边决定调工具）。
    """
    events: list[StreamEvent] = []

    # 用量信息在最后一个 chunk 里，此时 choices 是空数组。
    # 所以这个判断必须在遍历 choices 之前。
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        events.append(
            Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )
        )

    for choice in getattr(chunk, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        if delta is not None:
            # 文本增量
            content = getattr(delta, "content", None)
            if content:
                events.append(TextDelta(text=content))

            # 工具调用碎片
            for tc in getattr(delta, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                events.append(
                    ToolCallDelta(
                        index=getattr(tc, "index", 0) or 0,
                        id=getattr(tc, "id", None),
                        name=getattr(fn, "name", None) if fn else None,
                        # arguments 是 JSON 字符串的碎片，这里**不做解析**
                        arguments_delta=(getattr(fn, "arguments", None) or "") if fn else "",
                    )
                )

        # 结束标志。这里先发出去，由 Provider 负责「扣住它、最后再吐」
        raw_reason = getattr(choice, "finish_reason", None)
        if raw_reason:
            events.append(Finish(reason=classify_finish_reason(raw_reason)))

    return events


def _is_retryable(exc: BaseException) -> bool:
    """判断这个异常重试有没有意义。

    注意：这个判断结果只是「提示」。Provider 自己不重试（已定决策），
    它把结论打包进 ErrorEvent，由上层状态机决定怎么办 ——
    因为只有上层知道当前跑到哪一步、用户等多久了、要不要降级到别的模型。
    """
    retryable_types = tuple(
        t
        for t in (
            getattr(openai, "APIConnectionError", None),
            getattr(openai, "APITimeoutError", None),
            getattr(openai, "RateLimitError", None),
            getattr(openai, "InternalServerError", None),
        )
        if t is not None
    )
    return isinstance(exc, retryable_types)


class OpenAICompatProvider:
    """面向任何 OpenAI 兼容服务的 Provider。

    满足 `modelforge.providers.base.ChatProvider` 协议。
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
        include_usage: bool = True,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url
        self.include_usage = include_usage

        self._client = AsyncOpenAI(
            # Ollama 之类的本地服务不需要 Key，但 SDK 要求非空，给个占位符。
            api_key=api_key or "not-needed",
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ):
        """流式生成。契约见 `ChatProvider.stream` 的文档字符串。"""
        # 只传真正有值的参数 —— 有些兼容服务会拒绝不认识的字段。
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if self.include_usage:
            kwargs["stream_options"] = {"include_usage": True}

        # 类型标注必须是 FinishReason 而不是 str：写成 str 会让窄类型在这里被拓宽，
        # 后面传给 Finish(reason=...) 时 mypy 就会报错——它是对的，那确实是个漏洞。
        finish_reason: FinishReason = "stop"

        try:
            stream = await self._client.chat.completions.create(**kwargs)

            async for chunk in stream:
                for event in chunk_to_events(chunk):
                    # 扣住 Finish，保证它是整条流的最后一个事件 ——
                    # 因为底层把结束标志和用量信息放在不同 chunk 里，
                    # 直接转发的话 Finish 会排在 Usage 前面，契约就不成立了。
                    if isinstance(event, Finish):
                        finish_reason = event.reason
                        continue
                    yield event

        except Exception as exc:
            logger.warning("[%s] 流式请求失败: %s", self.name, exc)
            yield ErrorEvent(
                message=f"{type(exc).__name__}: {exc}",
                retryable=_is_retryable(exc),
            )
            # 即使出错也要补上 Finish，否则消费方的「Finish 一定最后出现」
            # 这个假设就被打破了，它会一直等下去。
            yield Finish(reason="error")
            return

        yield Finish(reason=finish_reason)

    async def aclose(self) -> None:
        """释放底层 HTTP 连接池。长驻进程退出时应当调用。"""
        await self._client.close()
