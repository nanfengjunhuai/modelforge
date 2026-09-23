"""Agent 循环的测试 —— 全部离线。

模型和沙箱都换成假的，所以这里测的是**循环本身的逻辑**：
事件顺序、Finish 的唯一性、消息怎么成对写回历史、轮数上限怎么收尾。

这些是目前整个项目里最容易悄悄出错的一块 —— 因为出错的表现不是崩溃，
而是「模型突然开始胡言乱语」（历史写错了）或者「回答少了半截」
（中间轮次的 Finish 被转发出去，前端提前退出了）。
两种都不会有任何报错。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from modelforge.agents.loop import run_agent_turn
from modelforge.providers.base import Message
from modelforge.providers.events import (
    ErrorEvent,
    Finish,
    ProviderEvent,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolResult,
    Usage,
)
from modelforge.sandbox.base import ExecutionResult
from modelforge.sandbox.tools import TOOL_SPECS

# ══════════════════════════════════════════════════════ 测试替身


class _ScriptedProvider:
    """按剧本**分轮次**吐事件的假 Provider。

    每被调用一次 `stream()` 就消耗一轮剧本。这是和 M1 那个假 Provider
    最大的区别 —— 单轮的假 Provider 测不出「多轮之间消息有没有写对」，
    而那恰恰是 Agent 循环最容易错的地方。
    """

    name = "scripted"

    def __init__(self, rounds: list[list[ProviderEvent]]) -> None:
        self._rounds = rounds
        # 每次调用都记下当时的完整消息列表，供断言检查历史是否正确
        self.seen_messages: list[list[Message]] = []
        self.seen_kwargs: list[dict[str, Any]] = []

    async def stream(self, messages: list[Message], **kwargs: Any):
        index = len(self.seen_messages)
        self.seen_messages.append([dict(m) for m in messages])
        self.seen_kwargs.append(kwargs)

        events = self._rounds[index] if index < len(self._rounds) else [Finish()]
        for event in events:
            yield event

    async def aclose(self) -> None:  # pragma: no cover —— 协议要求，测试用不到
        pass


class _FakeExecutor:
    """假执行器：不真的跑代码，只记录收到了什么、返回预设的结果。"""

    name = "fake"

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self._result = result or ExecutionResult(stdout="5050\n", exit_code=0, duration_ms=3)
        self.seen_code: list[str] = []

    async def run(self, code: str, *, timeout: float | None = None) -> ExecutionResult:
        self.seen_code.append(code)
        return self._result


def tool_round(
    *, code: str = "print(sum(range(1, 101)))", call_id: str = "call_1"
) -> list[ProviderEvent]:
    """构造一轮「模型请求调用 run_python」的事件。

    刻意把 `arguments` 切成三个碎片 —— 真实模型就是这么发的，
    而碎片累积是最容易写错的一环（M1 的 ToolCallAccumulator 就是为它存在的）。
    """
    return [
        ToolCallDelta(index=0, id=call_id, name="run_python", arguments_delta='{"code": '),
        ToolCallDelta(index=0, arguments_delta=json.dumps(code)),
        ToolCallDelta(index=0, arguments_delta="}"),
        Finish(reason="tool_calls"),
    ]


def text_round(text: str, reason: str = "stop") -> list[ProviderEvent]:
    return [
        TextDelta(text=text),
        Usage(prompt_tokens=10, completion_tokens=2),
        Finish(reason=reason),  # type: ignore[arg-type]
    ]


async def collect(agen) -> list[StreamEvent]:
    return [event async for event in agen]


def kinds(events: list[StreamEvent]) -> list[str]:
    return [event.type for event in events]


# ══════════════════════════════════════════════════════ 基本路径


async def test_plain_answer_runs_exactly_one_round():
    """模型不需要工具时，循环应当只跑一轮就结束。"""
    provider = _ScriptedProvider([text_round("熵权法是一种客观赋权方法。")])
    executor = _FakeExecutor()

    events = await collect(run_agent_turn(provider, [], executor=executor, tools=TOOL_SPECS))

    assert kinds(events) == ["text_delta", "usage", "finish"]
    assert events[-1].reason == "stop"  # type: ignore[union-attr]
    assert executor.seen_code == []  # 没跑任何代码


async def test_no_tools_configured_skips_the_loop_entirely():
    """没给工具时，连轮数上限都不该生效 —— 直接一轮结束。

    这条测试守的是一个边界：`tools=None` 时 `effective_limit` 会变成 0，
    如果那里写错成「至少跑一轮循环」，会导致一次多余的模型调用
    （多花一次钱，而且结果完全一样）。
    """
    provider = _ScriptedProvider([text_round("好的。")])

    events = await collect(
        run_agent_turn(provider, [], executor=_FakeExecutor(), tools=None)
    )

    assert kinds(events) == ["text_delta", "usage", "finish"]
    assert len(provider.seen_messages) == 1
    assert provider.seen_kwargs[0]["tools"] is None


# ══════════════════════════════════════════════════════ 工具调用


async def test_tool_call_is_executed_and_result_is_forwarded():
    """完整走一遍：模型要工具 → 执行 → 结果推给前端 → 模型接着说话。"""
    provider = _ScriptedProvider([tool_round(), text_round("1 到 100 的和是 5050。")])
    executor = _FakeExecutor(ExecutionResult(stdout="5050\n", exit_code=0, duration_ms=7))

    events = await collect(run_agent_turn(provider, [], executor=executor, tools=TOOL_SPECS))

    assert kinds(events) == [
        "tool_call_delta", "tool_call_delta", "tool_call_delta",  # 碎片原样转发
        "tool_result",                                            # M3 新增
        "text_delta", "usage", "finish",
    ]
    result = events[3]
    assert isinstance(result, ToolResult)
    assert result.ok
    assert result.stdout == "5050\n"
    assert result.duration_ms == 7
    # 三个碎片拼出来的代码，必须完整无缺地送到了执行器
    assert executor.seen_code == ["print(sum(range(1, 101)))"]


async def test_finish_appears_exactly_once_and_last():
    """**本文件最重要的一条。**

    每一轮模型生成结束时，底层都会给一个 Finish。如果循环照单转发，
    前端就会在第一轮工具调用之后以为「说完了」而退出循环 ——
    真正的答案一个字都收不到。

    所以契约是：整条流里**恰好一个** Finish，且在最后。
    """
    provider = _ScriptedProvider(
        [
            tool_round(call_id="call_a"),
            tool_round(call_id="call_b"),
            text_round("好了。"),
        ]
    )

    events = await collect(run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS))

    finishes = [e for e in events if e.type == "finish"]
    assert len(finishes) == 1, f"Finish 出现了 {len(finishes)} 次，应该恰好 1 次"
    assert events[-1].type == "finish"
    # 中间那两轮各自的 Finish(reason="tool_calls") 被吃掉了，
    # 所以 tool_calls 这个取值不该出现在最终的事件流里
    assert finishes[0].reason == "stop"  # type: ignore[union-attr]


async def test_assistant_and_tool_messages_are_written_back_in_pairs():
    """assistant（请求）和 tool（结果）必须成对写回历史。

    只写 tool 不写 assistant 的话，模型下一轮会看到一个「工具返回了结果」
    却找不到对应的「我请求过这个工具」—— 大多数服务会直接返回 400，
    整条对话当场断掉。
    """
    provider = _ScriptedProvider([tool_round(call_id="call_xyz"), text_round("好的。")])

    await collect(run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS))

    # 第二次调用时，历史里应该已经有了一对完整的消息
    second_round_messages = provider.seen_messages[1]
    assert len(second_round_messages) == 2

    assistant_msg, tool_msg = second_round_messages

    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["id"] == "call_xyz"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "run_python"
    # 参数在历史里必须是**字符串**（OpenAI 协议要求），不是解析后的对象
    assert isinstance(assistant_msg["tool_calls"][0]["function"]["arguments"], str)

    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_xyz"  # 必须和上面那个 id 对上
    assert "5050" in tool_msg["content"]


async def test_tool_output_is_formatted_for_the_model():
    """回填给模型的是「渲染过的文本」，不是原始对象。"""
    provider = _ScriptedProvider([tool_round(), text_round("好。")])
    executor = _FakeExecutor(
        ExecutionResult(
            stdout="均值: 3.5\n",
            stderr="DeprecationWarning: 某库过时了\n",
            exit_code=0,
        )
    )

    await collect(run_agent_turn(provider, [], executor=executor, tools=TOOL_SPECS))

    content = provider.seen_messages[1][1]["content"]
    assert "均值: 3.5" in content
    # stderr 有内容但退出码是 0 → 要告诉模型「这不影响执行成功」，
    # 否则它会去修一个根本不需要修的警告
    assert "不影响执行成功" in content


async def test_empty_output_tells_the_model_to_print():
    """空输出是最常见的困惑点 —— 模型忘了 print，然后对着空白发呆。"""
    provider = _ScriptedProvider([tool_round(), text_round("好。")])
    executor = _FakeExecutor(ExecutionResult(stdout="", stderr="", exit_code=0))

    await collect(run_agent_turn(provider, [], executor=executor, tools=TOOL_SPECS))

    assert "print()" in provider.seen_messages[1][1]["content"]


# ══════════════════════════════════════════════════════ 失败与边界


async def test_bad_json_arguments_become_a_failed_result_not_a_crash():
    """参数是坏 JSON 时，要变成一条 ok=False 的结果让模型自己修，
    而不是抛异常把整条流掐断。"""
    truncated = [
        ToolCallDelta(index=0, id="call_1", name="run_python", arguments_delta='{"code": "print(1'),
        # 这里本该还有收尾的 '"}'，但被 max_tokens 截断了
        Finish(reason="tool_calls"),
    ]
    provider = _ScriptedProvider([truncated, text_round("抱歉，我重新写一下。")])
    executor = _FakeExecutor()

    events = await collect(run_agent_turn(provider, [], executor=executor, tools=TOOL_SPECS))

    result = next(e for e in events if e.type == "tool_result")
    assert isinstance(result, ToolResult)
    assert not result.ok
    assert "JSON" in (result.error or "")
    # 执行器根本没被调用 —— 参数都没解析出来，不该往下走
    assert executor.seen_code == []


async def test_truncated_arguments_are_sanitized_before_writing_back():
    """坏 JSON **不能**原样写回历史。

    有些兼容服务在下一轮请求里会去解析历史中的 tool_calls.arguments，
    一个坏 JSON 就让整个请求 400 —— 一次截断毁掉整条对话。
    """
    truncated = [
        ToolCallDelta(index=0, id="call_1", name="run_python", arguments_delta='{"code": "print(1'),
        Finish(reason="tool_calls"),
    ]
    provider = _ScriptedProvider([truncated, text_round("重来。")])

    await collect(run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS))

    sent = provider.seen_messages[1][0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(sent) == {}, "坏参数应当被替换成合法的空对象"


async def test_unknown_tool_name_is_reported_to_the_model():
    """模型幻觉出一个不存在的工具时，要告诉它有哪些工具可用。"""
    hallucinated = [
        ToolCallDelta(index=0, id="call_1", name="run_matlab", arguments_delta="{}"),
        Finish(reason="tool_calls"),
    ]
    provider = _ScriptedProvider([hallucinated, text_round("抱歉，我用错工具了。")])

    events = await collect(run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS))

    result = next(e for e in events if e.type == "tool_result")
    assert not result.ok  # type: ignore[union-attr]
    assert "run_python" in (result.error or "")  # type: ignore[union-attr]


async def test_provider_error_ends_the_turn_cleanly():
    """模型侧出错时，错误要如实传出，并且仍然以一个 Finish 收尾。

    没有这个收尾的话，前端会一直等一个永远不来的结束信号，
    界面上那个「正在思考」的转圈会永远转下去。
    """
    provider = _ScriptedProvider(
        [
            [
                TextDelta(text="半句"),
                ErrorEvent(message="网络断了", retryable=True),
                Finish(reason="error"),
            ]
        ]
    )

    events = await collect(run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS))

    assert kinds(events) == ["text_delta", "error", "finish"]
    assert events[-1].reason == "error"  # type: ignore[union-attr]


async def test_max_rounds_stops_and_asks_one_final_time_without_tools():
    """用完轮数配额之后，不是硬掐断，而是**再问一次但不给工具**。

    给用户一个「我没能完全搞定」的答案，比给一个空白页面好。
    """
    # 剧本只给三轮：前两轮要工具，第三轮是收尾那一次的回答。
    # 一开始这里多写了一个 tool_round 当「不该被用到的一轮」——
    # 结果测试红了，因为**那一轮恰恰就是收尾轮**。
    # 剧本的轮次数必须和「循环实际会调几次模型」严格对上。
    provider = _ScriptedProvider(
        [
            tool_round(call_id="c1"),
            tool_round(call_id="c2"),
            text_round("综合前面的结果，我的结论是……"),
        ]
    )

    events = await collect(
        run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS, max_rounds=2)
    )

    # 一共调了 3 次模型：2 次带工具 + 1 次收尾
    assert len(provider.seen_messages) == 3
    assert provider.seen_kwargs[0]["tools"] is not None
    assert provider.seen_kwargs[1]["tools"] is not None
    assert provider.seen_kwargs[2]["tools"] is None, "收尾那一轮不该再给工具"

    assert events[-1].type == "finish"
    assert events[-1].reason == "stop"  # type: ignore[union-attr]


async def test_max_rounds_is_reported_when_the_model_still_wants_tools():
    """收尾那一轮不给工具了，但模型硬要调 —— 如实标成 max_rounds。

    不能谎报成 "stop"：那会让用户以为这是一段完整的回答。
    """
    provider = _ScriptedProvider(
        [
            tool_round(call_id="c1"),
            tool_round(call_id="c2"),
            # 最后一轮（没有工具可用）模型还是硬返回了 tool_calls
            [
                ToolCallDelta(
                    index=0, id="c3", name="run_python", arguments_delta="{}"
                ),
                Finish(reason="tool_calls"),
            ],
        ]
    )

    events = await collect(
        run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS, max_rounds=2)
    )

    assert events[-1].type == "finish"
    assert events[-1].reason == "max_rounds"  # type: ignore[union-attr]


async def test_parallel_tool_calls_are_all_executed():
    """模型一次请求多个工具时，每一个都要执行、都要有结果、都要成对写回。"""
    parallel = [
        ToolCallDelta(
            index=0, id="call_a", name="run_python", arguments_delta='{"code": "print(1)"}'
        ),
        ToolCallDelta(
            index=1, id="call_b", name="run_python", arguments_delta='{"code": "print(2)"}'
        ),
        Finish(reason="tool_calls"),
    ]
    provider = _ScriptedProvider([parallel, text_round("两个都跑完了。")])
    executor = _FakeExecutor()

    events = await collect(run_agent_turn(provider, [], executor=executor, tools=TOOL_SPECS))

    assert kinds(events).count("tool_result") == 2
    assert executor.seen_code == ["print(1)", "print(2)"]

    # 历史里：1 条 assistant + 2 条 tool，且 tool_call_id 要和请求逐一对上
    second_round = provider.seen_messages[1]
    assert [m["role"] for m in second_round] == ["assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in second_round[1:]] == ["call_a", "call_b"]


async def test_messages_list_is_extended_in_place():
    """循环会把消息写回调用方传进来的那个列表。

    这是刻意的（模型下一轮必须看得到自己刚干了什么），但很反直觉 ——
    大多数函数不会修改自己的入参。所以专门测一下，
    免得将来有人「顺手」改成返回新列表，导致上层拿到的是没更新的历史。
    """
    provider = _ScriptedProvider([tool_round(), text_round("好。")])
    messages: list[Message] = [{"role": "user", "content": "帮我算 1 到 100 的和"}]

    await collect(run_agent_turn(provider, messages, executor=_FakeExecutor(), tools=TOOL_SPECS))

    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]


async def test_temperature_and_max_tokens_are_forwarded_every_round():
    provider = _ScriptedProvider([tool_round(), text_round("好。")])

    await collect(
        run_agent_turn(
            provider,
            [],
            executor=_FakeExecutor(),
            tools=TOOL_SPECS,
            temperature=0.9,
            max_tokens=128,
        )
    )

    assert all(kw["temperature"] == 0.9 for kw in provider.seen_kwargs)
    assert all(kw["max_tokens"] == 128 for kw in provider.seen_kwargs)


@pytest.mark.parametrize("reason", ["length", "content_filter", "stop"])
async def test_non_tool_finish_reasons_end_the_turn_immediately(reason: str):
    """任何不是 tool_calls 的结束原因都直接收尾，不再多跑一轮。

    用参数化而不是写三条：断言逻辑一模一样，只有取值不同。
    失败时 pytest 会告诉你是哪个取值挂了。
    """
    provider = _ScriptedProvider([text_round("半句话", reason=reason)])

    events = await collect(run_agent_turn(provider, [], executor=_FakeExecutor(), tools=TOOL_SPECS))

    assert len(provider.seen_messages) == 1
    assert events[-1].reason == reason  # type: ignore[union-attr]
