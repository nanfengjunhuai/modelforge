"""报告生成 —— 把一次会话里发生过的事，变成一篇能交上去的论文草稿。

════════════════════════════════════════════════════════════════════════
这个包为什么在 `reports/` 而不是 `agents/`
════════════════════════════════════════════════════════════════════════

`agents/__init__.py` 里规划过一个 `write.py`（「成文：生成论文级报告」），
是当初「拆题 → 选模 → 求解 → 验证 → 成文」五节点状态机的一环。

真正动手时发现它不属于那里。理由和 M4 没做那个状态机是同一条
（见 `modelforge/core/__init__.py`）：报告是**从已落盘的事件里投影出来的**，
不是 Agent 在对话中做的某一步。

  · 它**不需要工具**（不跑代码、不问用户），所以不需要 Agent 循环
  · 它**不在对话里发生** —— 用户点「生成报告」时，那一轮对话早就结束了
  · 它读的是**整个会话的日志**，而 `agents/loop.py` 读的是投影出来的消息历史

放这里之后，`agents/` 保持「驱动一轮对话」这一个职责不变。

════════════════════════════════════════════════════════════════════════
分工
════════════════════════════════════════════════════════════════════════

    outline.py   事件日志 → 骨架（哪些节、每节的材料和程序块）  纯函数
    prompt.py    报告语体 + 每节的请求文本
    check.py     核对正文里的数字有没有出处                      纯函数
    render.py    骨架 + 散文 → 文档 → Markdown                  纯函数
    blocks.py    块模型（跨语言的契约，TS 侧有镜像）

除了 `api/reports.py` 里那几次模型调用和读盘，其余全是纯函数 ——
所以它们能被穷举测试，而这一轮最要紧的那条性质（**报告里的图和数字
不经过模型的手**）也就成了一个可以被断言的事实，而不是一句承诺。
"""

from __future__ import annotations

from .blocks import (
    BLOCK_KINDS,
    REPORT_VERSION,
    CodeBlock,
    DecisionBlock,
    FigureBlock,
    NoteBlock,
    ProseBlock,
    ReportBlock,
    ReportDocument,
    ReportSection,
)
from .check import find_unverified_numbers
from .outline import ReportOutline, SectionPlan, build_outline, name_key
from .prompt import REPORT_SYSTEM_PROMPT, section_message
from .render import render_document, render_markdown

__all__ = [
    "BLOCK_KINDS",
    "REPORT_SYSTEM_PROMPT",
    "REPORT_VERSION",
    "CodeBlock",
    "DecisionBlock",
    "FigureBlock",
    "NoteBlock",
    "ProseBlock",
    "ReportBlock",
    "ReportDocument",
    "ReportOutline",
    "ReportSection",
    "SectionPlan",
    "build_outline",
    "find_unverified_numbers",
    "name_key",
    "render_document",
    "render_markdown",
    "section_message",
]
