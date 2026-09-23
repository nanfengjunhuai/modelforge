"""M2 SSE 端点的测试。

全部离线：把 Provider 换成一个按剧本吐事件的假实现（monkeypatch 掉
`chat.get_provider`），所以不需要 API Key，也不会真的联网。

这里要盯住的核心契约只有一条：
    **SSE 帧的格式，以及「Finish 一定是最后一个事件」。**
前者被前端 60 行手写解析器依赖，后者被前端的 for-await 循环依赖。
两边都不在同一个进程里，改坏了不会有编译错误 —— 只能靠这些测试拦。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from modelforge.api import chat
from modelforge.main import create_app
from modelforge.providers.events import (
    ErrorEvent,
    Finish,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from tests import helpers

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════ 测试替身


class _FakeProvider:
    """按剧本吐事件的假 Provider。

    刻意**不做**任何异常处理 —— 它要能忠实地扮演一个「守规矩的 Provider」，
    把契约是否被 SSE 层正确传递下去暴露出来。
    """

    name = "fake"

    def __init__(self, events: list[StreamEvent] | None = None) -> None:
        self._events = events if events is not None else [TextDelta(text="你好"), Finish()]
        # 记下收到的东西，供断言检查「消息是否被正确拼装后传下来」
        self.seen_messages: list[dict[str, Any]] = []
        self.seen_kwargs: dict[str, Any] = {}
        self.closed = False

    async def stream(self, messages: list[dict[str, Any]], **kwargs: Any):
        # ⚠️ 必须**快照**，不能直接存引用。
        #
        # Agent 循环会把传进去的那个列表**就地扩展**（每轮的工具调用和结果
        # 都要写回历史，模型下一轮才看得到）。存引用的话，测试事后读到的是
        # 「后来又变了」的内容。
        #
        # M4 之前循环只在工具轮扩展它，所以这个替身一直是「对」的 ——
        # 直到 M4 让**每一轮**都扩展它才暴露：一个普通回答的测试突然发现
        # 消息多了一条。这类 bug 最难的地方在于**它长得像是被测代码的错**。
        self.seen_messages = [dict(m) for m in messages]
        self.seen_kwargs = kwargs
        for event in self._events:
            yield event

    async def aclose(self) -> None:
        self.closed = True


class _ExplodingProvider:
    """模拟「我们自己的代码有 bug」—— 生成器吐了一半直接抛异常。

    用来验证 chat.py 里那道保险：即使上层炸了，前端也必须收到一个
    结构化的结束信号，而不是一条永远等不到 Finish 的断流。
    """

    name = "boom"

    async def stream(self, messages: list[dict[str, Any]], **kwargs: Any):
        yield TextDelta(text="半句")
        raise RuntimeError("内部炸了")

    async def aclose(self) -> None:
        pass


# ══════════════════════════════════════════════════════ 夹具


# Provider 缓存的清理在 conftest.py 里（autouse），
# test_sessions_api.py 也需要同一份。


@pytest.fixture
def client():
    # 用 with 而不是直接构造：这样会真正跑一遍 lifespan
    # （启动钩子 + 关闭钩子）。关闭钩子里的 close_providers() 也就被覆盖到了。
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeProvider:
    """默认剧本的假 Provider，并把它装进 chat 模块。"""
    return _install(monkeypatch)


def _install(monkeypatch: pytest.MonkeyPatch, events: list[StreamEvent] | None = None) -> Any:
    provider = _FakeProvider(events)
    # 替换的是 chat 模块命名空间里的 get_provider —— _acquire_provider 在调用时
    # 才从模块全局里查这个名字，所以换掉就能生效。
    monkeypatch.setattr(chat, "get_provider", lambda name=None, **_: provider)
    return provider


# ══════════════════════════════════════════════════════ 工具函数


# 帧解析器在 helpers.py 里 —— test_sessions_api.py 也要用同一份。
# 同一个语言里放两份实现没有「互相验证」的价值，只会各自漂移。
parse_frames = helpers.frames_of


def post(client: TestClient, **payload: Any):
    body = {"messages": [{"role": "user", "content": "你好"}], **payload}
    return client.post("/api/chat/stream", json=body)


# ══════════════════════════════════════════════════════ 帧格式


def test_format_sse_shape():
    """一帧的形状：event 行 + data 行 + 一个空行收尾。"""
    frame = chat.format_sse(TextDelta(text="熵"))

    assert frame.startswith("event: text_delta\ndata: ")
    assert frame.endswith("\n\n")
    # 中间不能出现空行 —— 那会被前端当成帧的结束，把一帧切成两帧。
    assert "\n\n" not in frame[:-2]
    # 帧里只能有一个换行分隔 event 和 data（data 本身是单行 JSON）
    assert frame[:-2].count("\n") == 1

    assert json.loads(frame.split("data: ", 1)[1]) == {"type": "text_delta", "text": "熵"}


def test_format_sse_is_single_line_even_for_multiline_text():
    """文本里带换行时，data 仍然必须是一行。

    这是 `model_dump_json()` 的功劳：JSON 会把真实换行转义成 \\n。
    如果哪天有人图省事改成手工拼字符串，这个测试会立刻抓住。
    """
    frame = chat.format_sse(TextDelta(text="第一行\n第二行"))

    assert frame.endswith("\n\n")
    assert "\n\n" not in frame[:-2]
    assert json.loads(frame.split("data: ", 1)[1])["text"] == "第一行\n第二行"


@pytest.mark.parametrize(
    "event",
    [
        TextDelta(text="x"),
        ToolCallDelta(index=0, id="call_1", name="run_python", arguments_delta='{"a"'),
        Usage(prompt_tokens=10, completion_tokens=3),
        Finish(reason="length"),
        ErrorEvent(message="boom", retryable=True),
    ],
)
def test_every_event_type_survives_a_roundtrip(event: StreamEvent):
    """五种事件都能干净地过一遍 SSE 编解码。

    用参数化而不是写五个测试函数：这条断言的逻辑完全一样，
    只有输入不同。参数化失败时会明确告诉你是哪个取值挂了。
    """
    frame = chat.format_sse(event)
    name, data = parse_frames(frame)[0]

    assert name == event.type
    # 判别子必须原样往返 —— 前端整个 switch 都建立在它上面
    assert data["type"] == event.type
    assert data == json.loads(event.model_dump_json())


# ══════════════════════════════════════════════════════ 端点行为


def test_endpoint_streams_events_in_order(client: TestClient, fake: _FakeProvider):
    resp = post(client)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # 这三个头是给中间层（nginx 等）看的，少了会导致「整段一起蹦出来」
    assert resp.headers["cache-control"] == "no-cache, no-transform"
    assert resp.headers["x-accel-buffering"] == "no"

    frames = parse_frames(resp.text)
    assert [name for name, _ in frames] == ["text_delta", "finish"]
    assert frames[0][1]["text"] == "你好"


def test_finish_is_always_last(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """契约：Finish 一定是最后一条。

    构造一条「Finish 后面还跟着东西」的剧本是不合法的，
    但真实模型确实可能把 usage 和 finish 放在不同 chunk 里乱序到达 ——
    M1 的 Provider 负责把顺序理好，SSE 层负责不要再打乱它。
    """
    _install(
        monkeypatch,
        [TextDelta(text="a"), Usage(prompt_tokens=5, completion_tokens=1), Finish()],
    )

    frames = parse_frames(post(client).text)

    assert frames[-1][0] == "finish"
    assert [n for n, _ in frames].count("finish") == 1


def test_error_is_in_band_and_http_stays_200(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """**这是本文件最重要的一条测试。**

    流开始之后的错误必须走带内事件，HTTP 状态码保持 200。
    因为响应头在第一个字节发出时就定死了，之后改不了 ——
    前端读 `response.ok` 会以为是成功的，然后靠事件循环里的 error 分支处理。
    这两条路径必须都通，否则某类失败会变成白屏。
    """
    _install(
        monkeypatch,
        [
            TextDelta(text="前"),
            ErrorEvent(message="网络断了", retryable=True),
            Finish(reason="error"),
        ],
    )

    resp = post(client)

    assert resp.status_code == 200
    frames = parse_frames(resp.text)
    assert [name for name, _ in frames] == ["text_delta", "error", "finish"]
    assert frames[1][1]["retryable"] is True
    assert frames[2][1]["reason"] == "error"


def test_exploding_provider_still_ends_with_finish(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """上层代码炸了，前端也不能被吊死。

    没有这道保险的话，生成器抛异常 → 响应被截断 → 前端的 for-await 循环
    正常退出（它只看流结束，不区分「正常结束」和「被截断」）→
    界面停在「正在思考」的转圈上，永远不动。
    """
    monkeypatch.setattr(chat, "get_provider", lambda name=None, **_: _ExplodingProvider())

    resp = post(client)

    assert resp.status_code == 200
    frames = parse_frames(resp.text)
    assert [name for name, _ in frames] == ["text_delta", "error", "finish"]
    assert frames[-1][1]["reason"] == "error"
    # 错误信息里要带上异常类型，方便排查；但它是给开发者看的
    assert "RuntimeError" in frames[1][1]["message"]


# ══════════════════════════════════════════════════════ 请求组装


def test_system_prompt_is_injected(client: TestClient, fake: _FakeProvider):
    post(client)

    assert fake.seen_messages[0]["role"] == "system"
    assert "蒟蒻" in fake.seen_messages[0]["content"]
    assert fake.seen_messages[1] == {"role": "user", "content": "你好"}


def test_existing_system_prompt_is_not_overridden(client: TestClient, fake: _FakeProvider):
    """前端自己带了 system 消息时，不能把人设硬塞进去变成两条。"""
    post(
        client,
        messages=[{"role": "system", "content": "你是猫"}, {"role": "user", "content": "喵"}],
    )

    assert len(fake.seen_messages) == 2
    assert fake.seen_messages[0]["content"] == "你是猫"


def test_temperature_and_max_tokens_are_forwarded(client: TestClient, fake: _FakeProvider):
    post(client, temperature=0.9, max_tokens=128)

    assert fake.seen_kwargs["temperature"] == 0.9
    assert fake.seen_kwargs["max_tokens"] == 128


def test_provider_cache_reuses_the_same_instance(client: TestClient, fake: _FakeProvider):
    """两次请求应当复用同一个 Provider（含它的连接池）。

    不缓存的话每个请求都要重做一次 TCP + TLS 握手，首字延迟会翻倍。
    """
    post(client)
    post(client)

    assert list(chat._providers) == ["deepseek"]
    assert chat._providers["deepseek"] is fake


# ══════════════════════════════════════════════════════ 参数校验


def test_empty_messages_rejected(client: TestClient):
    resp = client.post("/api/chat/stream", json={"messages": []})

    # 422 是 FastAPI 对请求体校验失败的固定状态码。
    # 这条错误发生在流开始之前，所以能正常用状态码表达。
    assert resp.status_code == 422


def test_unknown_provider_returns_400(client: TestClient):
    """未知 provider 走真实代码路径（不 monkeypatch），验证 400 + 人话。"""
    resp = post(client, provider="不存在的模型")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "不存在的模型" in detail
    # 错误信息要能指导用户下一步做什么，而不只是宣布失败
    assert "deepseek" in detail


def test_temperature_out_of_range_rejected(client: TestClient, fake: _FakeProvider):
    resp = post(client, temperature=5.0)

    assert resp.status_code == 422
