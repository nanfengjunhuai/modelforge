"""跨语言契约：Python 的事件模型 vs TypeScript 的镜像。

════════════════════════════════════════════════════════════════════════
为什么需要这个文件
════════════════════════════════════════════════════════════════════════
`modelforge/providers/events.py` 定义事件，`web/src/lib/stream-types.ts` 手抄一份。
两份文件之间**没有任何机制保证同步**：

  · 后端加了第 7 种事件 → 前端 switch 走进 default 分支 → 静默不显示
  · 后端改了枚举取值   → 前端少一个分支 → 同样静默

「静默」是这里最要命的词。没有报错、没有异常、没有日志，
只是某个功能莫名其妙不工作，而你会先去怀疑网络、怀疑浏览器、怀疑模型。

所以这里做一件很土但很有效的事：**把两个文件都当文本读进来，
抠出各自的 type 取值集合，比对。** 这不是优雅的做法，
但它把「靠人记住」换成了「机器每次跑测试都检查一遍」。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from modelforge.providers.events import FinishReason, ProviderEvent, StreamEvent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TS_TYPES = PROJECT_ROOT / "web" / "src" / "lib" / "stream-types.ts"
TS_LOG_TYPES = PROJECT_ROOT / "web" / "src" / "lib" / "log-types.ts"


# ══════════════════════════════════════════════════════ Python 侧


def _union_members(annotated_union: object) -> list[type]:
    """从 `Annotated[A | B | ..., Field(discriminator=...)]` 里取出 A、B、...

    `get_args` 在外面那层返回 `(联合本身, FieldInfo)`，
    再对联合本身调一次才拿到各个成员。这个嵌套结构是 Pydantic 判别联合的
    实现细节，所以封成一个小函数，免得测试正文里到处是 `get_args(...)[0]`。
    """
    outer = get_args(annotated_union)
    assert outer, "这不是一个 Annotated 类型"
    return list(get_args(outer[0]))


def _python_event_types(union: object) -> set[str]:
    """抠出每种事件的 `type` 字段的默认值 —— 也就是判别子的取值。"""
    return {member.model_fields["type"].default for member in _union_members(union)}  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════ TypeScript 侧


@pytest.fixture(scope="module")
def ts_source() -> str:
    assert TS_TYPES.exists(), (
        f"找不到 {TS_TYPES}。这个测试要同时看到前后端两个文件，"
        "确认你在完整的仓库里跑它。"
    )
    return TS_TYPES.read_text(encoding="utf-8")


def _ts_union_members(source: str, name: str) -> set[str]:
    """把 `export type X = 'a' | 'b' | ...` 里的字面量抠出来。

    只匹配以 `|` 开头的连续行 —— 我们的联合都写成了每行一个成员的形式。
    这么写不只是为了好看：加成员时 diff 只有一行，
    而且这个正则也才能可靠地知道联合在哪里结束。
    """
    match = re.search(
        rf"export type {name}\s*=\s*((?:\s*\|[^\n]*\n)+)",
        source,
    )
    assert match, f"在 {TS_TYPES.name} 里找不到 `export type {name} = ...` 的定义"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def _ts_event_types(source: str) -> set[str]:
    """抠出所有事件类型定义里的 `type: 'xxx'` 字段值。

    每种事件（TextDelta / ToolResultEvent / …）都有一个 `type` 字段当判别子，
    值就是它在协议里的名字。全部抓出来就是「前端认识的事件集合」。
    """
    return set(re.findall(r"^\s*type:\s*'([a-z_]+)'", source, re.M))


# ══════════════════════════════════════════════════════ 断言


def test_event_types_match_between_python_and_typescript(ts_source: str):
    """**本文件的核心断言**：两边认识的事件类型必须一模一样。

    这条测试红了，通常意味着：后端加了新事件但没改前端。
    去 `web/src/lib/stream-types.ts` 补一个对应的 `type` 定义即可。
    """
    python_side = _python_event_types(StreamEvent)
    ts_side = _ts_event_types(ts_source)

    assert python_side == ts_side, (
        f"\n后端有、前端没有：{sorted(python_side - ts_side)}"
        f"\n前端有、后端没有：{sorted(ts_side - python_side)}"
        f"\n（前端文件：{TS_TYPES}）"
    )


def test_finish_reasons_match_between_python_and_typescript(ts_source: str):
    """结束原因的取值集合也必须一致。

    为什么这条单独有一条测试？因为它是**另一类**不同步风险：
    事件类型没变，但某个事件的字段取值范围变了。
    只测事件类型的话，「加了 max_rounds 却忘了更新前端标签」会漏掉。
    """
    python_side = set(get_args(FinishReason))
    ts_side = _ts_union_members(ts_source, "FinishReason")

    assert python_side == ts_side, (
        f"\n后端有、前端没有：{sorted(python_side - ts_side)}"
        f"\n前端有、后端没有：{sorted(ts_side - python_side)}"
    )


# ══════════════════════════════════════════════════════ 事件日志的契约
#
# M4 新增的一整层。在它之前，这份文件**只管流事件** ——
# 而日志事件有七个 kind、各有一组字段，跨语言的手动同步和流事件一样脆弱：
# 把 `call_id` 写成 `callId` 不会有任何东西变红，只会在某个刷新之后
# 静默地少显示一张决策卡片。


@pytest.fixture(scope="module")
def ts_log_source() -> str:
    assert TS_LOG_TYPES.exists(), (
        f"找不到 {TS_LOG_TYPES}。这个测试要同时看到前后端两个文件，"
        "确认你在完整的仓库里跑它。"
    )
    return TS_LOG_TYPES.read_text(encoding="utf-8")


def _python_log_fields() -> dict[str, set[str]]:
    """{kind 取值: 该类字段名集合}。"""
    from modelforge.sessions.base import LogEvent

    return {
        member.model_fields["kind"].default: set(member.model_fields)  # type: ignore[attr-defined]
        for member in _union_members(LogEvent)
    }


def _ts_log_fields(source: str) -> dict[str, set[str]]:
    """从 log-types.ts 里抠出「每种日志事件的 kind 和顶层字段名」。

    只认那些**含 `kind` 字段**的 `export type Xxx = { ... }` 块 ——
    `LoggedMessage` / `LoggedToolCall` 同样是对象类型，但它们是日志事件
    的**零件**，不是日志事件本身。
    """
    result: dict[str, set[str]] = {}
    for match in re.finditer(r"export type (\w+) = \{(.*?)\n\}", source, re.S):
        body = match.group(2)
        kind = re.search(r"^\s*kind:\s*'([a-z_]+)'", body, re.M)
        if not kind:
            continue
        # `?` 是可选字段（TS 里 `note?: string`）。日志事件的顶层字段
        # 目前都是必填，但这个正则要能容忍注释里带冒号的行 ——
        # 所以锚在行首、且要求字段名是合法标识符。
        result[kind.group(1)] = set(re.findall(r"^\s*(\w+)\??:", body, re.M))
    return result


def test_log_event_kinds_match_between_python_and_typescript(ts_log_source: str):
    """两边认识的日志事件 kind 必须一模一样。

    这条红了通常意味着：后端加了一种日志记录但没改前端 ——
    于是那种记录在前端的 `switch` 里静默落进 default 分支，永远不显示。
    """
    from modelforge.sessions.base import LOG_EVENT_KINDS

    ts_side = _ts_log_fields(ts_log_source)
    assert set(ts_side) == set(LOG_EVENT_KINDS), (
        f"\n后端有、前端没有：{sorted(set(LOG_EVENT_KINDS) - set(ts_side))}"
        f"\n前端有、后端没有：{sorted(set(ts_side) - set(LOG_EVENT_KINDS))}"
        f"\n（前端文件：{TS_LOG_TYPES}）"
    )


def test_log_event_fields_match_between_python_and_typescript(ts_log_source: str):
    """**每种日志事件的字段名也要一致。**

    为什么 kind 对上了还不够？因为最容易犯的错不是「少了一种事件」，
    而是「字段名对不上」：后端 `call_id`、前端 `callId`。
    那种情况下 kind 检查全绿，而前端拿到的是一个 `undefined` ——
    决策卡片提交时带上 `callId: undefined`，后端返回 409，
    用户看到「页面上的卡片过期了，刷新一下」，刷新之后还是不行。

    这类 bug 的定位成本极高，而拦住它只需要十几行正则。
    """
    python_side = _python_log_fields()
    ts_side = _ts_log_fields(ts_log_source)

    mismatches = {
        kind: (python_side.get(kind, set()), fields)
        for kind, fields in ts_side.items()
        if python_side.get(kind, set()) != fields
    }
    assert not mismatches, "\n".join(
        f"{kind}: 后端 {sorted(py)} vs 前端 {sorted(ts)}"
        for kind, (py, ts) in mismatches.items()
    )
    assert set(python_side) == set(ts_side), "kind 集合对不上（见上一条测试）"


def test_provider_events_are_a_strict_subset_of_stream_events():
    """Provider 能产出的事件，必须是整条流上事件集合的**真子集**。

    这是 M3 把事件模型拆成两个联合的全部意义：
    多出来的那个（ToolResult）只有 Agent 循环能造，Provider 造不出来。

    如果哪天有人图省事把两个联合合并了，这条测试会红 —— 它守的不是
    「功能正常」，而是「类型没有撒谎」。
    """
    provider_side = _python_event_types(ProviderEvent)
    stream_side = _python_event_types(StreamEvent)

    assert provider_side < stream_side, "真子集关系被破坏了"
    # 多出来的这两个，**只有 Agent 循环能造**：
    #   tool_result      —— 沙箱跑完代码之后
    #   decision_request —— 循环决定该停下来问用户了
    # 每加一个都会让这里变红，逼着人想清楚「Provider 到底有没有能力产出它」。
    assert stream_side - provider_side == {"tool_result", "decision_request"}


async def test_every_stream_event_type_can_actually_be_produced():
    """每个事件类型都得有人**真的**产出它 —— 不能有「定义了但永远不发」的类型。

    死类型不只是浪费，它会误导读者：看到某个定义会以为「某处应该会发它」，
    然后去找半天。

    ⚠️ 这条测试曾经是个**假测试**：它只断言了三个函数 `callable`，
    而没有真的跑它们。上面那段注释当时甚至写着「这三个模块合起来覆盖了
    全部六种」—— 覆盖是覆盖了，但**测试一个字都没验证**。
    M4 新增 `decision_request` 时它没有守住任何东西，于是顺手改成真的。

    做法：把循环用剧本驱动一遍，收集所有**实际出现过**的事件类型，
    和 `StreamEvent` 的定义集合比对。多一个少一个都会红。
    """
    from modelforge.agents.loop import run_agent_turn
    from modelforge.providers.events import (
        ErrorEvent,
        Finish,
        TextDelta,
        ToolCallDelta,
        Usage,
    )

    class _Scripted:
        """按轮次吐事件的假 Provider。"""

        name = "scripted-for-contract"

        def __init__(self, rounds: list[list[object]]) -> None:
            self._rounds = rounds
            self._index = 0

        async def stream(self, messages: object, **kwargs: object):
            events = self._rounds[self._index]
            self._index += 1
            for event in events:
                yield event

        async def aclose(self) -> None:
            pass

    class _Executor:
        name = "fake"

        async def run(self, code: str, *, timeout: float | None = None, scope: str | None = None):
            from modelforge.sandbox.base import ExecutionResult

            return ExecutionResult(stdout="ok\n", exit_code=0)

    class _Recorder:
        async def assistant_round(self, message: object) -> None: ...
        async def tool_result(self, message: object, result: object) -> None: ...
        async def usage(self, **kwargs: object) -> None: ...
        async def decision_requested(self, request: object) -> None: ...

    def ask_arguments() -> str:
        import json

        return json.dumps({"question": "选哪个？", "options": ["A", "B"]}, ensure_ascii=False)

    # 一个把七种类型全踩一遍的剧本：
    #   第 1 轮：调 run_python → tool_call_delta + tool_result
    #   第 2 轮：调 ask_user   → decision_request + finish(awaiting_user)
    #   第 3 轮：模型侧出错     → error
    scripted = _Scripted(
        [
            [
                TextDelta(text="先算一下。"),
                ToolCallDelta(
                    index=0, id="c1", name="run_python",
                    arguments_delta='{"code": "print(1)"}',
                ),
                Usage(prompt_tokens=5, completion_tokens=1),
                Finish(reason="tool_calls"),  # type: ignore[arg-type]
            ],
            [
                ToolCallDelta(index=0, id="c2", name="ask_user", arguments_delta=ask_arguments()),
                Finish(reason="tool_calls"),  # type: ignore[arg-type]
            ],
        ]
    )
    # 第三轮单独跑一次（出错的那条剧本），因为 await_user 会让循环提前结束
    exploding = _Scripted([[ErrorEvent(message="网络断了"), Finish(reason="error")]])  # type: ignore[arg-type]

    from modelforge.sandbox.tools import ASK_USER_SPEC, TOOL_SPECS

    seen: set[str] = set()
    for provider in (scripted, exploding):
        async for event in run_agent_turn(
            provider,  # type: ignore[arg-type]
            [],
            executor=_Executor(),  # type: ignore[arg-type]
            recorder=_Recorder(),  # type: ignore[arg-type]
            tools=[*TOOL_SPECS, ASK_USER_SPEC],
        ):
            seen.add(event.type)

    assert seen == _python_event_types(StreamEvent), (
        f"\n定义了但没有任何人产出：{sorted(_python_event_types(StreamEvent) - seen)}"
        f"\n产出了但没定义：{sorted(seen - _python_event_types(StreamEvent))}"
    )


# ══════════════════════════════════════════════════════ 产物的字段名


def test_tool_result_artifacts_field_exists_on_both_sides(ts_source: str, ts_log_source: str):
    """`ToolResult.artifacts` 必须两边都有。

    M5 给 `ToolResult` 加了这个字段，而它要穿过**四个**文件：

        providers/events.py       ToolResult            ← 真相源
        sessions/base.py          LogTool.result        （直接复用上面那个模型，自动跟着走）
        web/src/lib/stream-types.ts   ToolResultEvent
        web/src/lib/log-types.ts      LoggedToolResult

    后两个是手写的镜像，而**跨语言字段名写错不会有任何东西变红** ——
    把 `artifacts` 写成 `artifact` 的话，前端只是永远拿到 undefined，
    然后安静地不显示任何图。后端测试全绿，前端编译也全绿。

    这正是 M4 那个日志事件字段名契约测试要防的同一类问题，
    所以沿用同样的办法：把两边的源码当纯文本读进来比对。
    """
    from modelforge.providers.events import ToolResult

    assert "artifacts" in ToolResult.model_fields, "后端的 ToolResult 上没有 artifacts 字段"

    # 前端那两个镜像**类型名不同**（一个带 Event 后缀、一个没有），
    # 所以要分别按类型名定位，不能用「找 tool_result 这个字符串」——
    # 那个字符串会先落在类型体内的 `type: 'tool_result'` 上，
    # 于是往后找的 `export type` 变成**下一个**类型块，
    # 测试会指着一个完全无关的类型说它少了字段。（写这个的时候真踩了一次。）
    for source, label, type_name in (
        (ts_source, "stream-types.ts", "ToolResultEvent"),
        (ts_log_source, "log-types.ts", "LoggedToolResult"),
    ):
        block = re.search(rf"export type {type_name} = \{{(.*?)\n\}}", source, re.S)
        assert block, f"{label} 里找不到 {type_name} 的定义"
        assert re.search(r"^\s*artifacts\??:", block.group(1), re.M), (
            f"{label} 的 {type_name} 里没有 artifacts 字段"
        )


def test_artifact_ref_fields_are_identical_on_both_sides(ts_source: str):
    """`ArtifactRef` 的字段名也要一致。

    这个模型会在下载链接、缩略图、文件大小提示里被用到 ——
    少一个字段的表现是「界面上少显示一点东西」，没人会当成 bug 来报。

    比的是**字段名集合**而不是顺序：TypeScript 那边的字段顺序和 Python
    不一样，而那不影响任何行为。
    """
    from modelforge.artifacts.base import ArtifactRef

    python_fields = set(ArtifactRef.model_fields)

    match = re.search(r"export type ArtifactRef = \{(.*?)\n\}", ts_source, re.S)
    assert match, "stream-types.ts 里找不到 ArtifactRef"

    ts_fields = set(re.findall(r"^\s*(\w+)\??:", match.group(1), re.M))

    assert python_fields == ts_fields, (
        f"\n只在 Python 里有：{sorted(python_fields - ts_fields)}"
        f"\n只在 TypeScript 里有：{sorted(ts_fields - python_fields)}"
    )
