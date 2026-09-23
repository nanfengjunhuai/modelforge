"""把事件日志**投影**成一份报告的骨架。

════════════════════════════════════════════════════════════════════════
这一层在干什么
════════════════════════════════════════════════════════════════════════

    events（八种 kind）  ──build_outline──►  ReportOutline  ──►  模型写散文
                                                  │
                                                  └─ blocks 是**已经是成品**的，
                                                     不经过模型

每个章节产出一个 `SectionPlan`，里面有两样东西：

    brief    给模型看的材料（纯文本）—— 它据此写这一节的正文
    blocks   程序块（决策记录 / 图表 / 代码）—— 原样进报告

**这两个为什么必须分开**：如果把已经结构化的记录拍平成文本交给模型、
再让它「照着写一遍」，数字就经过了模型的手 —— 而模型会改写、会四舍五入、
会在拿不准的时候编一个。所以模型**只看不抄**：材料用来理解，
成品由程序自己插。

════════════════════════════════════════════════════════════════════════
⚠️ 不要复用 `project.build_messages`
════════════════════════════════════════════════════════════════════════

写报告需要材料，而随手能找到的那份投影好的对话历史看起来正合适。它不行，
两个理由：

  ① 它会注入**蒟蒻人设**（`agents/prompts.py`）。报告是要交上去的论文，
     不能出现「蒟蒻」这个自称（ADR-017）。

  ② 会话处于 `awaiting_user` 时，`project_messages` 会给没有结果的工具调用
     合成一条【这次工具调用没有留下结果】。在**对话**语境里这句话是对的
     （那一轮确实被打断了），在**报告**语境里它是**假的** ——
     工具没被打断，只是用户还没回答那个问题。报告里写一句假话，
     比少写一节严重得多。

所以这里自己走一遍事件，只取它需要的三样：用户说了什么、用户拍板了什么、
代码跑了什么。

**这个模块是纯的** —— 不读盘、不取当前时间、不碰网络。所以它可以用手写的
事件列表穷举测试（`tests/test_outline.py`），而这正是它值得单独成一层的原因。
`caption` 要从 `.chart.json` 里读，那是 I/O，所以由调用方算好传进来。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from modelforge.sessions.base import (
    LogAssistant,
    LogEvent,
    LogTool,
    LogUser,
    StoredEvent,
)
from modelforge.sessions.project import list_decisions

from .blocks import CodeBlock, DecisionBlock, FigureBlock, ReportBlock

__all__ = [
    "CODE_LINES",
    "STDOUT_LINES",
    "ReportOutline",
    "SectionPlan",
    "build_outline",
    "head_lines",
    "name_key",
    "tail_lines",
]

STDOUT_LINES = 40
"""每段代码输出最多留多少行，**从尾部留**。

从尾部而不是从头部：模型打印结果时习惯把结论放在最后
（`print('权重:', w)` 在算完之后）。取头 40 行常常只拿到一堆中间过程。

截断是真实的代价（前面的打印看不到了），所以 `CodeBlock` 存的**就是**
截断后的文本 —— 界面和导出的 Markdown 共用同一份，不会出现
「界面上看得到、导出之后没了」这种两套内容。
"""

CODE_LINES = 80
"""每段代码最多留多少行，**从头部留**（代码的入口在前面）。"""


# ══════════════════════════════════════════════════════ 骨架的类型


class SectionPlan(BaseModel):
    """一节报告在「写之前」的样子。"""

    title: str

    brief: str
    """交给模型的材料。**空串 = 这一节没东西可写**，会被丢掉。"""

    blocks: list[ReportBlock] = Field(default_factory=list)
    """程序块。**这些已经是成品** —— 模型不知道它们长什么样，
    报告渲染时它们会插在正文之后。"""


class ReportOutline(BaseModel):
    """一份报告在「写之前」的样子。"""

    title: str
    session_id: str
    sections: list[SectionPlan]


# ══════════════════════════════════════════════════════ 小工具


def _event(item: StoredEvent | LogEvent) -> LogEvent:
    return item.event if isinstance(item, StoredEvent) else item


def head_lines(text: str, limit: int) -> str:
    """保留前 `limit` 行，并**如实说明砍掉了多少**（不静默截断）。"""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + f"\n…（其余 {len(lines) - limit} 行略）"


def tail_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return f"（前 {len(lines) - limit} 行略）…\n" + "\n".join(lines[-limit:])


def name_key(name: str) -> str:
    """产物的**分组键**：显示名去掉扩展名，再砍掉尾巴上的 `.chart`。

    ⚠️ 不能直接用 `Path(name).stem` —— 那正是这个函数存在的理由：

        权重对比.chart.json   →   stem 是 `权重对比.chart`
        权重对比.png          →   stem 是 `权重对比`

    两个配不上，于是图永远找不到自己的 caption。而 `.chart.json` 和跟它
    同名的图**是同一个东西的两个视图**（ADR-012），本来就该配对。

    前端 `web/src/lib/report-types.ts` 里有一份同样的实现（产物面板按它
    分组），两边的规则必须一致 —— `tests/test_report_contract.py` 盯着。
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if stem.endswith(".chart"):
        stem = stem[: -len(".chart")]
    return stem.strip().lower()


def _extract_code(call: Mapping[str, Any]) -> str:
    """从一次工具调用里抠出 `code` 参数。

    `arguments` 是模型生成的 JSON **字符串**，而且可能已经坏了
    （max_tokens 截断会留下一段语法不完整的 JSON）。这里失败时退回原始
    字符串而不是抛 —— 报告不该因为一段解析不了的代码整个失败，
    而且那段文本本身就有用：它说明了模型当时写了什么。
    """
    function = call.get("function") or {}
    raw = function.get("arguments") or ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(parsed, dict):
        return raw
    code = parsed.get("code")
    return code if isinstance(code, str) else raw


class _ToolRun(BaseModel):
    """一次 `run_python` —— 代码和结果凑齐了，才是报告里的一段证据。"""

    seq: int
    code: str
    ok: bool
    stdout: str
    figures: list[FigureBlock] = Field(default_factory=list)


def _tool_runs(items: Sequence[StoredEvent | LogEvent]) -> list[_ToolRun]:
    """把「代码」和「执行结果」配起来。

    为什么要两遍：**这两样东西在两种事件里**。代码在 assistant 消息的
    `tool_calls[].function.arguments` 里，结果在 `tool` 事件里，靠 `call_id`
    对应。（`project_messages` 也是这么配的，它还要更麻烦，得处理缺结果。）

    输出顺序是 `tool` 事件的顺序，也就是**执行顺序** —— 报告要的是故事的先后，
    不是消息历史的先后。
    """
    code_by_call: dict[str, str] = {}
    for item in items:
        event = _event(item)
        if isinstance(event, LogAssistant):
            for call in event.message.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id:
                    code_by_call[call_id] = _extract_code(call)

    runs: list[_ToolRun] = []
    for index, item in enumerate(items):
        event = _event(item)
        if not isinstance(event, LogTool):
            continue
        result = event.result
        runs.append(
            _ToolRun(
                seq=item.seq if isinstance(item, StoredEvent) else index,
                code=head_lines(code_by_call.get(result.call_id, ""), CODE_LINES),
                ok=result.ok,
                stdout=tail_lines(result.stdout.strip(), STDOUT_LINES),
                figures=[
                    FigureBlock(artifact_id=ref.id, name=ref.name)
                    for ref in result.artifacts
                    if ref.kind == "image"
                ],
            )
        )
    return runs


# ══════════════════════════════════════════════════════ 四个章节
#
# 每一节的形状都一样：`_xxx_section(...) -> SectionPlan`。
# brief 为空就表示这一节没材料，`build_outline` 会把它整个丢掉 ——
# 留一个只有标题的空壳看起来像生成失败。


def _problem_section(users: list[LogUser]) -> SectionPlan:
    title = "一、问题重述"
    if not users:
        return SectionPlan(title=title, brief="")
    brief = "【用户提出的问题】\n" + users[0].content
    if len(users) > 1:
        extra = "\n\n".join(f"- {u.content}" for u in users[1:])
        brief += f"\n\n【用户后来补充的要求】\n{extra}"
    return SectionPlan(title=title, brief=brief)


def _assumptions_section(items: Sequence[StoredEvent | LogEvent]) -> SectionPlan:
    """模型假设与方案选择 —— **报告里最值钱的一节**。

    整节来自 `decision_request` / `decision_answer` 两种事件，带着当时摆出来的
    候选、用户选的那个、和用户写的备注。这正是 ADR-009 选事件日志而不是快照的
    理由：「你当时为什么选 AHP」和「你选了 AHP」一样重要，而快照只留得下后者。

    ⚠️ 只收**已答复**的决策。没答的那些不进报告 —— 报告是「已经做过的事」的
    记录，一条悬而未决的提问放进去会让人以为它被回答过。
    """
    title = "二、模型假设与方案选择"
    answered = [d for d in list_decisions(items) if d.choice is not None]
    if not answered:
        return SectionPlan(title=title, brief="")

    blocks: list[ReportBlock] = [
        DecisionBlock(
            seq=d.seq,
            question=d.question,
            options=list(d.options),
            choice=d.choice or "",
            note=d.note,
        )
        for d in answered
    ]
    lines = [
        "【建模过程中由用户拍板的决策】",
        "（下面每一条都会原样附在正文后面。你只需要把它们串起来、说清每条"
        "决策对模型意味着什么。**不要重复抄写选项列表**，也不要编造用户没说过的理由。）",
    ]
    for index, d in enumerate(answered, start=1):
        note = f"；用户备注：{d.note}" if d.note else ""
        lines.append(
            f"{index}. 问：{d.question}\n"
            f"   候选：{' / '.join(d.options)}\n"
            f"   用户选择：{d.choice}{note}"
        )
    return SectionPlan(title=title, brief="\n".join(lines), blocks=blocks)


def _solution_section(runs: list[_ToolRun]) -> SectionPlan:
    title = "三、模型的建立与求解"
    if not runs:
        return SectionPlan(title=title, brief="")

    blocks: list[ReportBlock] = []
    lines: list[str] = [
        "【求解过程中真正跑过的代码和它的输出】",
        "（下面的代码和输出会原样附在正文后面。你只需要说明每一步在做什么、"
        "得到了什么结论 —— **不要重复代码，也不要把输出里的数字再抄一遍**。）",
    ]
    for index, run in enumerate(runs, start=1):
        status = "成功" if run.ok else "失败"
        lines.append(
            f"\n第 {index} 段（{status}）：\n{run.code}\n--- 输出 ---\n{run.stdout}"
        )
        for figure in run.figures:
            note = f"（{figure.caption}）" if figure.caption else ""
            lines.append(f"（这一段产出了一张图：{figure.name}{note}）")

        blocks.append(
            CodeBlock(seq=run.seq, ok=run.ok, code=run.code, stdout=run.stdout)
        )
        blocks.extend(run.figures)
    return SectionPlan(title=title, brief="\n".join(lines), blocks=blocks)


def _conclusion_section(runs: list[_ToolRun]) -> SectionPlan:
    """结论那一节的材料 —— **只给最后几步的输出**。

    全部输出都给它没有意义：结论要看的是最终结果，而中间过程几十段读不完，
    还会把上下文撑爆。所以取最后三段。
    """
    title = "四、结论"
    if not runs:
        return SectionPlan(title=title, brief="")

    lines = ["【最后一次求解的输出（结论应当基于它们）】"]
    for run in runs[-3:]:
        first_line = run.code.splitlines()[0] if run.code else "（无代码）"
        lines.append(f"\n--- {first_line} ---")
        lines.append(run.stdout)

    figures = [f for run in runs for f in run.figures]
    if figures:
        lines.append("\n【报告里的插图】")
        lines.extend(
            f"- {f.name}：{f.caption}" if f.caption else f"- {f.name}" for f in figures
        )
    lines.append(
        "\n（如果上面这些输出**不足以**支撑一个有依据的结论，就直说"
        "「目前的结果还不足以给出确定结论」，不要为了把这一节写满而推测。）"
    )
    return SectionPlan(title=title, brief="\n".join(lines))


# ══════════════════════════════════════════════════════ 入口


def build_outline(
    items: Sequence[StoredEvent | LogEvent],
    *,
    session_id: str,
    title: str,
    captions: Mapping[str, str] | None = None,
) -> ReportOutline:
    """把事件日志投影成报告骨架。

    Args:
        items: 会话的完整事件日志（按 seq 升序）。
        session_id: 写进文档，前端据此拼产物的下载地址。
        title: 报告标题（一般是会话标题）。
        captions: `name_key(产物显示名) → caption`。**由调用方读盘算好再传进来**
            —— 这个函数必须是纯的（没有 I/O 才能穷举测试），而 caption 藏在
            `.chart.json` 这个**产物文件**里。传 None 或查不到就用空串，
            那张图在报告里就只剩文件名。

    Returns:
        骨架。**空章节会被丢掉**（没有决策记录就没有第二章）——
        留一个只有标题的空壳看起来像生成失败了。
    """
    captions = captions or {}
    users = [e for e in (_event(item) for item in items) if isinstance(e, LogUser)]
    runs = _tool_runs(items)

    # 给图配上 caption。按**分组键**找而不是按产物 id：caption 在另一份产物
    # （`.chart.json`）里，两份产物之间唯一的联系就是显示名。
    for run in runs:
        run.figures = [
            figure.model_copy(update={"caption": captions.get(name_key(figure.name), "")})
            for figure in run.figures
        ]

    plans = [
        _problem_section(users),
        _assumptions_section(items),
        _solution_section(runs),
        _conclusion_section(runs),
    ]
    # 唯一的存活判据：**有材料可写，或者有块可放**。四个章节的差别只是它们
    # 各自从哪种事件里取材料，判据本身只有这一条。
    return ReportOutline(
        title=title,
        session_id=session_id,
        sections=[p for p in plans if p.brief.strip() or p.blocks],
    )
