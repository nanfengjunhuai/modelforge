"""SSE 流式对话端点 —— 把 M1 的事件模型推到浏览器。

════════════════════════════════════════════════════════════════════════
这个模块在整条链路上的位置
════════════════════════════════════════════════════════════════════════

    Provider（M1）          本模块（M2）                 前端
    StreamEvent       ──►   SSE 帧文本           ──►   fetch + ReadableStream
    （Pydantic 对象）        "event:..\\ndata:..\\n\\n"      （web/src/lib/sse.ts）

M1 当初选 Pydantic 判别联合的回报，在这里兑现：`event.model_dump_json()`
一行就把对象变成 SSE 的 data 载荷，一行手写序列化都不需要。
连带的好处是 `type` 字段（判别子）天然就是 SSE 的 `event:` 名 —— 前端靠它分派。

════════════════════════════════════════════════════════════════════════
关于「错误分成两种」
════════════════════════════════════════════════════════════════════════
这是本模块最需要想清楚的一件事：

    流开始**之前**的错误  →  HTTP 状态码（400 / 401 / 500）
    流开始**之后**的错误  →  带内的 `error` 事件，HTTP 仍然是 200

为什么？因为 HTTP 状态码和响应头必须在**第一个字节**发出前就定下来。
一旦我们开始 yield SSE 帧，200 已经写在响应头里了，此时再想报错，
唯一的通路就是「在流里面塞一条错误事件」——这就是 events.py 里
ErrorEvent 存在的意义（当时写的理由是「省掉一次格式转换」，现在兑现了）。

对应到前端：`fetch()` 的 `response.ok` 管前者，事件循环里的 `error` 分支管后者。
两边都要处理，缺一个就会有某个失败路径变成白屏。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from modelforge.agents.prompts import SYSTEM_PROMPT
from modelforge.config import get_settings
from modelforge.providers.base import ChatProvider, Message
from modelforge.providers.events import ErrorEvent, Finish, StreamEvent
from modelforge.providers.registry import get_provider

logger = logging.getLogger(__name__)

__all__ = ["close_providers", "format_sse", "router"]

router = APIRouter(prefix="/chat", tags=["chat"])


# ══════════════════════════════════════════════════════ SSE 帧格式


def format_sse(event: StreamEvent) -> str:
    """把一个事件渲染成一帧 SSE 文本。

    SSE 的帧语法：若干行 `字段: 值`，然后一个**空行**表示这一帧结束。
    我们只用到两个字段：

        event  —— 事件名，前端用它分派。正好等于 Pydantic 的 type 判别子。
        data   —— 载荷，一行 JSON。

    为什么 data 一行就够、不需要规范里那套「多行 data 拼接」？
    因为 `model_dump_json()` 产出的 JSON 里不可能有裸换行（都会被转义成 \\n）。
    规范留那条路是给「原始文本」用的，我们传的是 JSON，用不上。

    ⚠️ 结尾的 `\\n\\n` 不能少也不能多。少一个 → 前端收不到完整帧，会一直等；
       多一个 → 前端解析出空帧。
    """
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


# 这几个响应头都是为了让**中间层**别缓冲。
#
# 这是 SSE 最经典的翻车点：本地开发一切正常，一上服务器就变成「等了 5 秒
# 整段蹦出来」——因为 nginx / 各种网关默认会把响应攒够一块再转发。
# 具体：
#   · X-Accel-Buffering: no  → nginx 专用开关
#   · no-transform           → 禁止代理压缩/改写（压缩会破坏流的即时性）
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# ══════════════════════════════════════════════════════ 请求模型


class ChatRequest(BaseModel):
    """前端发来的一轮对话请求。

    `messages` 直接用 OpenAI 的 chat 格式（ADR-003），不做二次封装。
    这不是偷懒：多一层自定义格式就多一层翻译，而我们内部本来就统一用它。
    """

    messages: list[dict[str, object]] = Field(
        min_length=1,
        description="对话历史，OpenAI chat 格式：[{role, content}, ...]",
    )
    provider: str | None = Field(
        default=None,
        description="模型供应方，不传则用配置里的默认值（目前是 deepseek）。",
    )
    system_prompt: str | None = Field(
        default=None,
        description="覆盖默认的蒟蒻人设提示词。主要给调试用。",
    )
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


# ══════════════════════════════════════════════════════ Provider 缓存

# 进程级的 Provider 缓存：名字 → 实例
_providers: dict[str, ChatProvider] = {}


def _acquire_provider(name: str | None) -> ChatProvider:
    """按名字取 Provider，带进程级缓存。

    为什么必须缓存？因为 `AsyncOpenAI` 内部维护着一个 httpx **连接池**，
    而连接池是跟着客户端实例走的。如果每个请求都新建一个 Provider，
    就等于每个请求都要重新握手 —— 到 DeepSeek 的 TCP + TLS 握手是几百毫秒，
    和首字延迟（实测 571ms）是同一个量级。不缓存的话首字延迟直接翻倍。

    缓存的 key 用「解析后的名字」而不是用户传进来的原始值
    （原始值可能是 None，直接当 key 会让默认 provider 和显式指定的
      deepseek 变成两个不同的缓存项，白白多建一个连接池）。

    Raises:
        ValueError: 名字未注册，或者该 provider 需要的 API Key 没配。
    """
    key = (name or get_settings().default_provider).lower()
    provider = _providers.get(key)
    if provider is None:
        provider = get_provider(key)
        _providers[key] = provider
        logger.info("已装配 provider: %s", key)
    return provider


async def close_providers() -> None:
    """关闭所有缓存的 Provider，释放连接池。

    由应用关闭钩子调用（见 main.py 的 lifespan）。不关的话，uvicorn 退出时
    会留下未关闭的 httpx 连接，在 --reload 反复重启的开发场景下会越积越多。
    """
    for name, provider in _providers.items():
        try:
            await provider.aclose()
        # 关闭失败不该阻止进程退出，所以吞掉异常只记日志。
        except Exception:
            logger.warning("关闭 provider %s 时出错", name, exc_info=True)
    _providers.clear()


# ══════════════════════════════════════════════════════ 端点


@router.post("/stream", summary="流式对话（SSE）")
async def stream_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    """把一轮对话以 SSE 推给前端，逐字输出。

    返回的是一条 `text/event-stream`，每一帧形如：

        event: text_delta
        data: {"type":"text_delta","text":"熵"}

    事件类型共五种，定义在 `modelforge/providers/events.py`。前端在
    `web/src/lib/stream-types.ts` 里有一份对应的 TypeScript 定义。

    注意 `request` 参数：表面上没用到，但它会让 FastAPI 注入 Request 对象。
    Starlette 的 StreamingResponse 会**并发监听** `http.disconnect`，一旦
    浏览器断开（关页面、点停止），它会取消我们这个生成器任务 ——
    于是正在跑的模型请求也就跟着停了，不会继续烧 token。
    这个取消是以 `CancelledError`（BaseException 的子类）抛进来的，
    所以下面那个 `except Exception` **故意不拦它**，让它正常向上传播。
    """
    try:
        provider = _acquire_provider(payload.provider)
    except ValueError as exc:
        # 配置类错误（provider 名字写错 / API Key 没配）发生在流开始之前，
        # 可以正常用 HTTP 状态码表达，前端读 response.ok 就能拿到。
        # 这正是上面「错误分两种」里说的第一种。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    messages: list[Message] = list(payload.messages)
    # 前端不传 system 消息时兜底注入蒟蒻人设。
    # 判断放在服务端而不是前端，是因为人设是服务端资产 ——
    # 换人设不该要求用户清浏览器缓存。
    if not any(m.get("role") == "system" for m in messages):
        messages.insert(
            0,
            {"role": "system", "content": payload.system_prompt or SYSTEM_PROMPT},
        )

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in provider.stream(
                messages,
                temperature=payload.temperature,
                max_tokens=payload.max_tokens,
            ):
                yield format_sse(event)
        except Exception as exc:
            # 最后一道保险。Provider 内部已经把网络异常转成了 ErrorEvent，
            # 能走到这里说明是上游代码本身的 bug。
            # 即便如此也必须补一个 Finish —— 否则前端会一直等一个永远不来的
            # 结束信号，转圈转到用户失去耐心。契约（Finish 一定最后出现）
            # 是跨层承诺，每一层都有责任维护它。
            logger.exception("SSE 流异常终止")
            yield format_sse(ErrorEvent(message=f"{type(exc).__name__}: {exc}"))
            yield format_sse(Finish(reason="error"))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
