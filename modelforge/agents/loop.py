"""Agent 循环 —— 让模型真的能跑代码。

════════════════════════════════════════════════════════════════════════
一个「回合」里发生了什么
════════════════════════════════════════════════════════════════════════

    用户：帮我算一下 1 到 100 的和
      │
      ├─ 第 1 轮 ────────────────────────────────────────────
      │   模型：我要调用 run_python({"code": "print(sum(range(1,101)))"})
      │   （finish_reason = "tool_calls"）
      │        ↓ 执行
      │   ToolResult(stdout="5050")
      │        ↓ 回填成 {"role": "tool", ...}
      │
      ├─ 第 2 轮 ────────────────────────────────────────────
      │   模型：1 到 100 的和是 **5050**。（finish_reason = "stop"）
      │
      └─ 结束，把 Finish 发出去

**循环的终止条件是模型自己说「我说完了」**，不是我们数够了几轮。
轮数上限只是一道保险丝（见下面「为什么必须有上限」）。

════════════════════════════════════════════════════════════════════════
两个不明显但必须做对的地方
════════════════════════════════════════════════════════════════════════

① **中间那几轮的 Finish 不能直接转发。**

   `Finish` 是整条流的最后一个事件（M1 定的契约）。而每一轮模型生成结束时
   底层都会给一个 Finish —— 如果照单转发，前端会在第一轮工具调用之后就
   以为「完了」，直接退出循环，后面的答案一个字都收不到。

   所以这里的做法是：**扣住每一轮的 Finish，只在真正结束的那一刻发一个。**
   和 M1 里 Provider 扣住底层 Finish 再统一发，是同一个套路，只是又往上
   提了一层。

② **必须把「模型请求调用工具」这条消息写回历史。**

   如果只追加工具结果（role=tool），模型下一轮会看到一个「工具返回了结果」
   却找不到对应的「我请求过这个工具」—— 大多数服务会直接返回 400。
   所以 assistant 消息（带 tool_calls）和 tool 消息必须**成对**出现。

════════════════════════════════════════════════════════════════════════
为什么必须有轮数上限
════════════════════════════════════════════════════════════════════════
模型完全可以陷入「反复调用同一个工具」的死循环 —— 尤其是当它修不好一个
报错的时候，它会一遍遍改、一遍遍失败。没有闸门的话，这就是一台不停烧
token 的机器，而用户只会看到转圈。

上限用完之后不是硬掐断，而是**再问一次但不给工具** —— 逼它用手上已有的
信息作答。给用户一个不完美的答案，比给一个空白页面好。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from modelforge.config import get_settings
from modelforge.providers.base import ChatProvider, Message, ToolSpec
from modelforge.providers.events import (
    Finish,
    FinishReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallAccumulator,
    ToolCallDelta,
)
from modelforge.sandbox.base import CodeExecutor
from modelforge.sandbox.tools import dispatch, format_for_model

logger = logging.getLogger(__name__)

__all__ = ["run_agent_turn"]


@dataclass
class _RoundState:
    """一轮生成的汇总。

    存在的理由：异步生成器没法同时「边 yield 边 return 一个值」。
    所以把汇总通过这个可变对象带出来，让上层在 `async for` 结束后能读到。
    （另一种写法是用 `gen.__anext__()` 手动捕获 StopAsyncIteration.value，
     能用但可读性差很多。）
    """

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    finish: FinishReason = "stop"


def _safe_arguments(raw: str) -> str:
    """确保回传给 API 的 arguments 是合法 JSON。

    模型偶尔会产出被截断的 JSON（撞到 max_tokens）。有些兼容服务在**下一次**
    请求里会去解析历史消息中的 `tool_calls[].function.arguments` —— 一个坏
    JSON 就能让整个请求 400，一次截断毁掉整条对话。

    所以这里兜个底：坏的换成 `{}`。原始内容仍然完整地出现在工具的错误信息里
    （见 `tools.dispatch`），模型看得到自己写歪了什么。
    """
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return "{}"
    return raw


def _assistant_message(text: str, calls: list[ToolCall]) -> Message:
    """把「模型请求调用工具」这一轮，还原成 OpenAI 格式的 assistant 消息。

    格式细节来自 ADR-003：内部统一用 OpenAI 的格式，所以这里不需要任何转换，
    直接拼就行 —— 这正是当初选它当内部标准的回报。
    """
    return {
        "role": "assistant",
        # 模型可能一边说话一边调工具。就算它一个字没说，content 也要给个空串
        # 而不是 None：各家兼容服务对 null 的处理不一致，空串是最安全的。
        "content": text,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": _safe_arguments(call.arguments)},
            }
            for call in calls
        ],
    }


async def _stream_round(
    provider: ChatProvider,
    messages: list[Message],
    *,
    tools: list[ToolSpec] | None,
    temperature: float,
    max_tokens: int | None,
    into: _RoundState,
) -> AsyncIterator[StreamEvent]:
    """跑一轮模型生成：转发事件，同时把结果写进 `into`。

    "边转发边汇总" 是这个函数存在的全部理由 —— 事件的转发必须**即时**，
    攒完再发就没有流式效果了；而汇总只能在流结束后才知道。
    """
    accumulator = ToolCallAccumulator()
    text_parts: list[str] = []

    async for event in provider.stream(
        messages, tools=tools, temperature=temperature, max_tokens=max_tokens
    ):
        if isinstance(event, ToolCallDelta):
            accumulator.feed(event)
            yield event
        elif isinstance(event, Finish):
            # 扣住不发。这一轮可能只是中间轮次，真正该发的 Finish
            # 由外层在整条对话结束时统一发。
            into.finish = event.reason
        else:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            yield event

    into.text = "".join(text_parts)
    into.calls = accumulator.result()


async def run_agent_turn(
    provider: ChatProvider,
    messages: list[Message],
    *,
    executor: CodeExecutor,
    tools: list[ToolSpec] | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    max_rounds: int | None = None,
) -> AsyncIterator[StreamEvent]:
    """跑完一个 Agent 回合，产出**一条连续的、契约完整的**事件流。

    Args:
        provider: 模型接入方。
        messages: 对话历史。**会被就地扩展** —— 每轮的工具调用和结果都要写进去，
            模型下一轮才看得到自己刚才干了什么。调用方应传入一个可变的、
            属于本次请求的列表。
        executor: 代码执行后端。
        tools: 可用的工具。None 或空列表表示这一轮不让模型用工具。
        temperature: 采样温度。
        max_tokens: 单轮输出上限。
        max_rounds: 工具调用轮数上限。None 表示用配置里的默认值。

    Yields:
        `StreamEvent`：文本增量、工具调用碎片、**工具执行结果**、用量、以及
        最后恰好一个 `Finish`。

    契约（与 M1 一致，且更严格）：
      · **恰好一个 Finish，且在最后。** 中间轮次的 Finish 会被吃掉。
      · **不抛异常。** 失败以带内的 ErrorEvent 表达，最后补一个 Finish。
        （`asyncio.CancelledError` 除外 —— 那是上层要停我们，让它传播。）
    """
    settings = get_settings()
    limit = max_rounds if max_rounds is not None else settings.agent_max_tool_rounds
    # 没有工具可用时，「工具调用」这件事根本不存在，轮数上限也就没有意义 ——
    # 直接按一轮处理，省掉一层循环。
    usable_tools = tools or None
    effective_limit = limit if usable_tools else 0

    for round_index in range(effective_limit):
        state = _RoundState()
        async for event in _stream_round(
            provider,
            messages,
            tools=usable_tools,
            temperature=temperature,
            max_tokens=max_tokens,
            into=state,
        ):
            yield event

        # 模型不再要工具了（或者中间出错了）→ 这一轮就是最终答案。
        # 注意 "error" 也会走到这里：Provider 出错后会给 Finish(reason="error")，
        # 我们不重试、也不再调工具，直接把错误如实传达给用户。
        if state.finish != "tool_calls" or not state.calls:
            yield Finish(reason=state.finish)
            return

        logger.info(
            "第 %d/%d 轮工具调用：%s",
            round_index + 1,
            effective_limit,
            [call.name for call in state.calls],
        )

        # 成对写入：assistant（请求）→ tool（结果）。缺一个下一轮就会 400。
        messages.append(_assistant_message(state.text, state.calls))

        for call in state.calls:
            result = await dispatch(call, executor=executor)
            # 先把结果推给前端（用户要能看见它跑了什么、跑出了什么），
            # 再写进历史（模型要看得到）。
            yield result
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": format_for_model(result),
                }
            )

    # 配额用尽。最后再问一次，**这次不给工具** —— 逼它用手上已有的信息作答。
    # 给用户一个「我没能完全搞定」的答案，比给一个空白页面好。
    logger.warning("工具调用达到上限 %d 轮，最后一轮不再提供工具", effective_limit)
    state = _RoundState()
    async for event in _stream_round(
        provider,
        messages,
        tools=None,
        temperature=temperature,
        max_tokens=max_tokens,
        into=state,
    ):
        yield event

    # 正常情况下这一轮不给工具，模型只能说话，finish 会是 "stop"。
    # 但有些模型会硬编一个 tool_calls 出来 —— 那种情况下如实标成 max_rounds。
    yield Finish(reason="max_rounds" if state.finish == "tool_calls" else state.finish)
