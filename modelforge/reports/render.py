"""把「骨架 + 模型写的散文」拼成一份报告，再渲染成 Markdown。

════════════════════════════════════════════════════════════════════════
两个视图，同一份数据（ADR-012 的形状）
════════════════════════════════════════════════════════════════════════

    ReportDocument  ──►  `.report.json`  应用内视图（前端按块渲染）
                    └─►  `.md`           给人看的（可下载、可粘进 Word）

这跟 M5 的 `<名字>.png` + `<名字>.chart.json` 是**同一个形状**：
可读的那份给人，结构化的那份给程序。之所以不省掉其中一个：

  · 只留 Markdown → 界面上就分不出「这一段是模型写的、那一块是程序投影的」，
    而那个区别正是这份报告可不可信的全部依据
  · 只留 JSON     → 用户拿不走、粘不进论文，等于没生成

两边由同一个 `ReportDocument` 渲染出来，所以**不可能对不上** ——
不存在「界面上有这张图、导出之后没了」这种事。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .blocks import (
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
from .outline import ReportOutline

__all__ = ["render_document", "render_markdown"]

_MAX_LISTED_NUMBERS = 8
"""核对结果里最多列出几个数字。剩下的用「等 N 个」带过。

全列出来的话，一段正文里飘着二十个数字的那种提示能占满整个面板，
而用户要的是「有没有问题」这个判断，不是一份数字清单。
"""


def _unverified_note(numbers: Sequence[str]) -> str:
    """把核对结果写成一句话 —— **注意措辞的分寸**。

    「找不到出处」不是「编造」。这两句话的差别不是语气问题：
    说成「编造」，用户第一次看到误报（一个合法的百分比）之后就会
    不再相信这个提示，于是它连真话也传不到了。见 `check.py` 的模块注释。
    """
    shown = "、".join(numbers[:_MAX_LISTED_NUMBERS])
    if len(numbers) > _MAX_LISTED_NUMBERS:
        shown += f" 等 {len(numbers)} 个"
    return (
        f"核对：这一段里的 {shown} 在过程记录里找不到出处。"
        "可能是模型自己算出来的（百分比、差值、四舍五入都会这样），"
        "也可能是编的 —— 请你自己扫一眼。"
    )


def render_document(
    outline: ReportOutline,
    prose: Mapping[str, str],
    *,
    notes: Sequence[str] = (),
) -> ReportDocument:
    """把模型写好的散文插进骨架，得到最终文档。

    Args:
        outline: `build_outline` 的产出。
        prose: `{章节标题: 正文}`。**缺的章节不会有正文块**，但它的程序块
            照旧保留 —— 少一段话和少一张图，严重程度差得远。
        notes: 文档级的说明（谁写的、有什么前提）。由调用方给，
            因为「哪个模型」这件事投影层不知道。

    Returns:
        可以直接写进 `.report.json`、也可以直接渲染成 Markdown 的文档。
        正文块**永远排在程序块前面** —— 一节读起来是「先说这件事，
        再附证据」，而不是反过来。
    """
    sections: list[ReportSection] = []
    for plan in outline.sections:
        text = (prose.get(plan.title) or "").strip()
        blocks: list[ReportBlock] = []

        if text:
            blocks.append(ProseBlock(text=text))
            unverified = find_unverified_numbers(text, plan.brief)
            if unverified:
                blocks.append(NoteBlock(text=_unverified_note(unverified)))
        blocks.extend(plan.blocks)

        sections.append(ReportSection(title=plan.title, blocks=blocks))

    return ReportDocument(
        title=outline.title,
        session_id=outline.session_id,
        sections=sections,
        notes=list(notes),
    )


# ══════════════════════════════════════════════════════ Markdown


def _fence(text: str) -> str:
    """挑一个不会被内容本身撞破的围栏。

    代码里出现三个反引号是**会发生的**（模型完全可能打印一段 Markdown 示例），
    而那会把围栏提前关掉、后面半段代码变成正文。多一个反引号就解决了。
    """
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _render_block(block: ReportBlock) -> str:
    if isinstance(block, ProseBlock):
        return block.text

    if isinstance(block, NoteBlock):
        return _quote(block.text)

    if isinstance(block, DecisionBlock):
        lines = [
            f"**过程记录 · 第 {block.seq} 步 · 用户拍板**",
            "",
            f"**问：** {block.question}",
            f"**候选：** {' ／ '.join(block.options)}",
            f"**用户选择：** {block.choice}",
        ]
        if block.note:
            lines.append(f"**备注：** {block.note}")
        return _quote("\n".join(lines))

    if isinstance(block, CodeBlock):
        status = "成功" if block.ok else "失败"
        fence = _fence(block.code)
        parts = [f"**过程记录 · 第 {block.seq} 步 · 代码执行（{status}）**", ""]
        parts.append(f"{fence}python\n{block.code}\n{fence}")
        if block.stdout:
            fence = _fence(block.stdout)
            parts.append("")
            parts.append(f"{fence}text\n{block.stdout}\n{fence}")
        return "\n".join(parts)

    if isinstance(block, FigureBlock):
        # 图片用**相对路径**（就是产物显示名）。一个 `.md` 里塞绝对 URL
        # 看着更方便，但它指向的是本地服务，服务一关就是死链 ——
        # 而且用户不会知道为什么。相对路径至少是诚实的：
        # 图和 md 放同一个目录下就能看。
        alt = block.caption or block.name
        image = f"![{alt}]({block.name})"
        return f"{image}\n\n*{block.caption}*" if block.caption else image

    # 判别联合是穷尽的，走到这里说明加了新块却没加渲染分支。
    # 用 `assert_never` 的话 mypy 会在**加新块那一刻**就报错，而不是等到有人
    # 生成了一份报告、发现少了一块。这里用一句显式的抛出，因为它同时
    # 也是运行时的兜底（mypy 没跑的时候它依然会响）。
    raise AssertionError(f"没有渲染分支的块类型：{block!r}")


def render_markdown(doc: ReportDocument) -> str:
    """`ReportDocument` → Markdown 文本。

    ⚠️ 开头那段说明**不是可有可无的**：图片用的是相对路径，而下载下来的
    `.md` 和它的图**不在一起**。用户打开文件会发现一堆裂图，然后合理地
    以为是生成坏了。与其让他自己猜，不如在文件开头就说清楚 ——
    这和 README 里「沙箱能挡什么、挡不住什么」是同一个做法：
    **知道边界的弱点，比把它藏起来强。**
    """
    parts: list[str] = [f"# {doc.title}", ""]
    parts.append(
        _quote(
            "这份草稿由模型工坊生成。正文段落由模型撰写；"
            "标注为「过程记录」的部分直接取自会话的事件日志，未经模型改写。"
        )
    )
    for note in doc.notes:
        parts.append(_quote(note))
    parts.append("")
    parts.append(
        _quote(
            "⚠️ 文中的图片用相对路径引用。下载这份 Markdown 之后，"
            "请把同一会话里的图片产物下载到**同一个目录**，图片才能显示。"
        )
    )

    for section in doc.sections:
        parts.append("")
        parts.append(f"## {section.title}")
        for block in section.blocks:
            parts.append("")
            parts.append(_render_block(block))

    parts.append("")
    return "\n".join(parts)
