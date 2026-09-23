"""Provider 抽象层的单元测试。

这些测试**不联网、不需要 API Key**，因为被测的核心逻辑都被设计成了纯函数
（`chunk_to_events`）或可注入的（Provider 的 `_client`）。

一个刻意的选择：构造 chunk 用的全是 `openai` SDK 的**真实类型**，而不是
SimpleNamespace 之类的替身。代价是写起来啰嗦，收益是——如果哪天 SDK 改了
字段结构，这些测试会红，而不是继续绿着骗你。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import (
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage

from modelforge.config import Settings
from modelforge.providers.events import (
    ErrorEvent,
    Finish,
    TextDelta,
    ToolCallAccumulator,
    ToolCallDelta,
    Usage,
)
from modelforge.providers.openai_compat import (
    OpenAICompatProvider,
    chunk_to_events,
    classify_finish_reason,
)
from modelforge.providers.registry import available_providers, get_provider

# ═══════════════════════════════════════════════ 构造测试用 chunk 的工具


def make_chunk(
    *,
    content: str | None = None,
    tool_calls: list[ChoiceDeltaToolCall] | None = None,
    finish_reason: str | None = None,
    usage: CompletionUsage | None = None,
) -> ChatCompletionChunk:
    """按真实 SDK 类型构造一个流式 chunk。"""
    return ChatCompletionChunk(
        id="chunk-test",
        choices=[
            Choice(
                index=0,
                delta=ChoiceDelta(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        created=0,
        model="test-model",
        object="chat.completion.chunk",
        usage=usage,
    )


def make_tool_call_delta(
    *,
    index: int = 0,
    id: str | None = None,
    name: str | None = None,
    arguments: str = "",
) -> ChoiceDeltaToolCall:
    return ChoiceDeltaToolCall(
        index=index,
        id=id,
        type="function",
        function=ChoiceDeltaToolCallFunction(name=name, arguments=arguments),
    )


# ═══════════════════════════════════════════════ chunk → 事件映射


def test_文本增量被映射成_TextDelta() -> None:
    events = chunk_to_events(make_chunk(content="你好"))
    assert events == [TextDelta(text="你好")]


def test_空内容不产生事件() -> None:
    # 很多 chunk 只有 role 或只有 tool_calls，content 是 None。
    # 这类 chunk 不该产出 TextDelta，否则前端会收到一堆空字符串。
    assert chunk_to_events(make_chunk(content=None)) == []


def test_用量信息即使_choices_为空也会被捕获() -> None:
    # 关键：OpenAI 把 usage 放在最后一个 chunk，此时 choices 是空数组。
    # 如果实现里先遍历 choices 再处理 usage，这里就会漏掉用量。
    chunk = make_chunk(
        usage=CompletionUsage(prompt_tokens=12, completion_tokens=34, total_tokens=46)
    )
    events = chunk_to_events(chunk)
    assert Usage(prompt_tokens=12, completion_tokens=34) in events


def test_结束原因被映射成_Finish() -> None:
    events = chunk_to_events(make_chunk(finish_reason="stop"))
    assert events == [Finish(reason="stop")]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", "stop"),
        ("tool_calls", "tool_calls"),
        ("length", "length"),
        ("content_filter", "content_filter"),
        # SDK 类型里还留着这个废弃值，必须折叠成 tool_calls，
        # 否则会穿透到上层的 Literal 类型导致校验失败。
        ("function_call", "tool_calls"),
        (None, "stop"),
    ],
)
def test_结束原因归一化(raw: str | None, expected: str) -> None:
    assert classify_finish_reason(raw) == expected


# ═══════════════════════════════════════════════ 工具调用碎片拼装


def test_工具调用碎片能被完整拼装() -> None:
    """这是 M1 最核心的一条测试：arguments 是分片到达的 JSON。"""
    acc = ToolCallAccumulator()
    for delta in [
        ToolCallDelta(index=0, id="call_abc", name="run_python", arguments_delta=""),
        ToolCallDelta(index=0, arguments_delta='{"code":'),
        ToolCallDelta(index=0, arguments_delta='"import nu'),
        ToolCallDelta(index=0, arguments_delta='mpy"}'),
    ]:
        acc.feed(delta)

    result = acc.result()
    assert len(result) == 1
    assert result[0].id == "call_abc"
    assert result[0].name == "run_python"
    assert result[0].arguments == '{"code":"import numpy"}'
    # 拼完之后必须是一个合法 JSON —— 这是交给沙箱的前提
    import json

    assert json.loads(result[0].arguments) == {"code": "import numpy"}


def test_并行工具调用按_index_分组() -> None:
    """碎片的到达顺序不保证是「第一个调用的全部 → 第二个调用的全部」。"""
    acc = ToolCallAccumulator()
    # 故意交错到达
    acc.feed(ToolCallDelta(index=0, id="call_a", name="run_python", arguments_delta="{"))
    acc.feed(ToolCallDelta(index=1, id="call_b", name="plot", arguments_delta="{"))
    acc.feed(ToolCallDelta(index=0, arguments_delta='"x":1}'))
    acc.feed(ToolCallDelta(index=1, arguments_delta='"y":2}'))

    result = acc.result()
    assert [tc.name for tc in result] == ["run_python", "plot"]
    assert result[0].arguments == '{"x":1}'
    assert result[1].arguments == '{"y":2}'


def test_后续碎片里的_None_不会清空已累积的_id_和_name() -> None:
    # 第二个碎片里 id 和 name 都是 None，如果实现写成 slot["id"] = delta.id，
    # 已经拿到的 id 就会被 None 覆盖掉。
    acc = ToolCallAccumulator()
    acc.feed(ToolCallDelta(index=0, id="call_x", name="foo", arguments_delta="a"))
    acc.feed(ToolCallDelta(index=0, arguments_delta="b"))

    tc = acc.result()[0]
    assert tc.id == "call_x"
    assert tc.name == "foo"
    assert tc.arguments == "ab"


def test_无参数的工具调用也能解析() -> None:
    # 底层可能一个参数字符都不发。给个空对象，保证后续 json.loads 不炸。
    acc = ToolCallAccumulator()
    acc.feed(ToolCallDelta(index=0, id="call_z", name="get_time", arguments_delta=""))

    import json

    assert json.loads(acc.result()[0].arguments) == {}


# ═══════════════════════════════════════════════ Provider 的流式契约


class _FakeCompletions:
    """替代 AsyncOpenAI 的 chat.completions。

    script 里的每一项要么是一个 chunk，要么是一个异常实例
    （遇到异常就抛，用来模拟流中途断掉）。
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = script

    async def create(self, **kwargs: Any) -> Any:
        async def gen():
            for item in self._script:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return gen()


def make_provider(script: list[Any]) -> OpenAICompatProvider:
    provider = OpenAICompatProvider(
        name="fake", base_url="http://fake.local/v1", model="fake-model", api_key="fake-key"
    )
    # 注入替身 —— 这正是「把 client 作为实例属性」的好处：测试可以替换它。
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=_FakeCompletions(script))
    )
    return provider


async def collect(provider: OpenAICompatProvider) -> list[Any]:
    return [event async for event in provider.stream([{"role": "user", "content": "hi"}])]


async def test_正常流以_Finish_收尾() -> None:
    provider = make_provider(
        [
            make_chunk(content="你"),
            make_chunk(content="好"),
            make_chunk(finish_reason="stop"),
        ]
    )
    events = await collect(provider)

    assert [e.type for e in events] == ["text_delta", "text_delta", "finish"]
    assert isinstance(events[-1], Finish)


async def test_Finish_永远是最后一个事件_即使_usage_后到() -> None:
    """这是「Finish 必须最后」这条契约最容易破的地方。

    真实情况下 finish_reason 出现在倒数第二个 chunk，usage 在最后一个。
    如果实现直接转发，顺序会变成 [... Finish, Usage]，
    消费方的 `while event.type != 'finish'` 就会提前退出、丢掉用量。
    """
    provider = make_provider(
        [
            make_chunk(content="答案"),
            make_chunk(finish_reason="stop"),
            make_chunk(
                usage=CompletionUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12)
            ),
        ]
    )
    events = await collect(provider)

    assert [e.type for e in events] == ["text_delta", "usage", "finish"]
    assert isinstance(events[-1], Finish)
    assert events[-1].reason == "stop"


async def test_流中途出错时_产出_ErrorEvent_并以_Finish_error_收尾() -> None:
    provider = make_provider(
        [
            make_chunk(content="前半句"),
            ConnectionError("连接被重置"),
        ]
    )
    events = await collect(provider)

    assert [e.type for e in events] == ["text_delta", "error", "finish"]
    assert isinstance(events[-1], Finish)
    # 契约要求：即使出错也要补 Finish，否则消费方会一直等下去
    assert events[-1].reason == "error"
    assert isinstance(events[1], ErrorEvent)


async def test_连接阶段就失败也会产出事件而不是抛异常() -> None:
    # 这是「Provider 不抛异常、错误在带内」这条设计的验证
    provider = make_provider([ConnectionError("无法建立连接")])
    events = await collect(provider)

    assert [e.type for e in events] == ["error", "finish"]


# ═══════════════════════════════════════════════ 注册表


def test_已注册的_provider_列表() -> None:
    assert set(available_providers()) >= {"deepseek", "ollama"}


def test_未知名字给出可读的错误() -> None:
    with pytest.raises(ValueError, match="未知的 provider"):
        get_provider("gpt-5-ultra", settings=Settings())


def test_缺失_API_Key_时提前报错而不是等到发请求() -> None:
    cfg = Settings(deepseek_api_key=None)
    with pytest.raises(ValueError, match="需要 API Key"):
        get_provider("deepseek", settings=cfg)


def test_能装配出_deepseek_provider() -> None:
    cfg = Settings(deepseek_api_key="sk-test", deepseek_model="deepseek-chat")
    provider = get_provider("deepseek", settings=cfg)

    assert provider.name == "deepseek"
    assert provider.model == "deepseek-chat"


def test_ollama_不需要_API_Key() -> None:
    # 本地模型服务没有 Key，不该被「缺 Key」的检查拦住
    provider = get_provider("ollama", settings=Settings())
    assert provider.name == "ollama"
