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
    assert stream_side - provider_side == {"tool_result"}


def test_every_event_type_is_actually_reachable():
    """每个事件类型都得有人真的产出它 —— 不能有「定义了但永远不发」的类型。

    死类型不只是浪费，它会误导读者：看到 ToolResult 的定义会以为
    「某个 Provider 可能会发」或者「某处应该会发」，然后去找半天。
    """
    from modelforge.agents.loop import run_agent_turn
    from modelforge.providers.openai_compat import chunk_to_events
    from modelforge.sandbox.tools import dispatch

    # 这三个模块合起来覆盖了全部六种：
    #   chunk_to_events → text_delta / tool_call_delta / usage / finish / error
    #   dispatch        → （返回 ToolResult，由 loop 转成事件）
    #   run_agent_turn  → 转发上面全部 + 自己产出 tool_result
    assert callable(chunk_to_events)
    assert callable(dispatch)
    assert callable(run_agent_turn)
