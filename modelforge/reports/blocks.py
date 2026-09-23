"""报告的「块」——一份报告就是一串类型化的块。

════════════════════════════════════════════════════════════════════════
这个文件为什么存在（ADR-015）
════════════════════════════════════════════════════════════════════════

数模论文的产出物是一篇**报告**，而报告里最不能出错的两样东西是**数字和图**。

最容易想到的做法是「把整个过程丢给模型，让它写一篇」。它确实最省事，
而且写出来的东西更流畅 —— 但那是**把编造数字的自由交给了模型**：
对话可能几十轮，模型看不见原材料（它的上下文里没有当时那些 stdout），
于是它只能凭印象写，而**编出来的权重和真算出来的权重长得一模一样**。

这里选的是另一条路，和 M5 的「图和数字同源」是同一个思路：

    程序负责（可追溯）   决策记录块 · 图表块 · 代码块   ← 从事件日志投影
    模型负责（措辞）     散文块                       ← 只写过渡、重述、结论

于是报告里**不可能**出现一张没画过的图、一个没算过的数 ——
因为那两样东西根本不是模型放进去的。

════════════════════════════════════════════════════════════════════════
跨语言的契约
════════════════════════════════════════════════════════════════════════

`web/src/lib/report-types.ts` 是这个文件的 TS 镜像，两边**没有任何机制
保证同步**。而不同步的症状是安静的：把 `artifact_id` 写成 `artifactId`，
Python 这边测试全绿、TS 那边编译全绿，只是图**永远不出现**，
而界面上看起来就像「这份报告没有图」。

防线是 `tests/test_report_contract.py`：它**真的调用**这里定义的联合，
把产出的 kind 集合和字段名集合拿去和 TS 文件比对。
（比 M4/M5 的契约测试强一点 —— 那两个只能把 Python 源码当文本正则。）
"""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, Field

__all__ = [
    "BLOCK_KINDS",
    "REPORT_VERSION",
    "CodeBlock",
    "DecisionBlock",
    "FigureBlock",
    "NoteBlock",
    "ProseBlock",
    "ReportBlock",
    "ReportDocument",
    "ReportSection",
]

REPORT_VERSION = 1
"""`.report.json` 的格式版本。

现在就写下来而不是等需要时再加：这个文件会被**存进磁盘**（产物的 sidecar），
于是它比一份内存里的结构多一层负担 —— 用户升级之后，盘上还躺着旧版本写的
文件。有一个版本号，将来要兼容旧格式时至少有个判据。
"""


# ══════════════════════════════════════════════════════ 块


class ProseBlock(BaseModel):
    """模型写的正文段落 —— **报告里唯一由模型生成的东西**。

    界面上它和别的块样式不同（正常正文 vs 带左竖线的引用块），
    这个区别是有意的：用户应该一眼看出「这句话是我自己的过程记录，
    还是模型在发挥」。
    """

    kind: Literal["prose"] = "prose"
    text: str


class DecisionBlock(BaseModel):
    """用户在某个分岔口拍板的记录 —— **从事件日志投影出来的，模型碰不到**。

    这是整份报告里最值得留下来的一块，也是事件日志这个选型（ADR-009）
    最有说服力的地方：它带着**当时的候选、用户选的那个、以及用户写的备注**。

    「你当时为什么选了 AHP」比「你选了 AHP」重要得多 ——
    而快照式的状态存储答不上来这个问题，它只留最终值。
    """

    kind: Literal["decision"] = "decision"
    seq: int
    """这个决策发生在日志里的第几步。给界面做锚点（将来可以跳回对话）。"""

    question: str
    options: list[str]
    choice: str
    """用户选的那个。这里**一定是已答复的** —— 没答的决策不进报告。"""

    note: str = ""
    """用户拍板时写的备注。空串表示他没写。"""


class FigureBlock(BaseModel):
    """一张图 —— 引用产物，不复制内容。

    刻意只存 `artifact_id` 而不是图片的字节或 URL：报告和产物是**两份
    独立的存储**，报告存引用、产物存文件。这样「图和数字同源」是结构上
    成立的，而不是靠生成时拷贝得对。
    """

    kind: Literal["figure"] = "figure"
    artifact_id: str
    """下载/渲染要走 `/api/artifacts/{session}/{id}`。"""

    name: str
    """产物显示名（可能是中文）。下载时的文件名，也是 Markdown 里的图片路径。"""

    caption: str = ""
    """模型画图时自己写的那句话（`mp.save(..., caption=...)`）。

    它比文件名强得多：「熵权法算出的指标权重」比「权重对比.png」多说了
    很多东西。M5 存这个字段时注释就写着「给报告用」，这里终于用上了。

    空串表示读不到 —— 没画过 `.chart.json`、或者文件坏了。**不抛异常**：
    少一句说明远没有「整份报告生成不出来」严重。
    """


class CodeBlock(BaseModel):
    """一次代码执行 —— 建立与求解那一节的证据。"""

    kind: Literal["code"] = "code"
    seq: int
    ok: bool
    """跑没跑通。失败的那次也留着 —— 数模论文的「模型检验」里常有它。"""

    code: str
    """模型写的那段 Python。界面里可以折叠。"""

    stdout: str = ""
    """截断过的输出。**不截断的话一份报告能到几兆**，而且模型也是靠它
    写结论的，所以两份视图共用同一份截断结果 —— 见 `outline.STDOUT_LINES`。"""


class NoteBlock(BaseModel):
    """程序插进来的一句**说明**，不是正文。

    两个用途：
      · 号码核对：这一段的正文里出现了几个在过程记录里找不到出处的数字
      · 没生成出来：这一节的正文没能生成（附原因）

    措辞必须**如实地弱**：说是「找不到出处」，不说「模型编造」。
    它确实会误报派生值（百分比、差值、四舍五入），
    一个动不动喊狼来了的检查比没有检查更糟 —— 用户会学会无视它。
    """

    kind: Literal["note"] = "note"
    text: str


ReportBlock = Annotated[
    ProseBlock | DecisionBlock | FigureBlock | CodeBlock | NoteBlock,
    Field(discriminator="kind"),
]
"""报告的块联合。判别的套路和 `LogEvent` 完全一样（Pydantic + kind）。"""

BLOCK_KINDS = frozenset(
    member.model_fields["kind"].default
    for member in get_args(get_args(ReportBlock)[0])
)
"""五种块的 kind 取值集合。给跨语言契约测试用（同 `LOG_EVENT_KINDS`）。"""


# ══════════════════════════════════════════════════════ 文档


class ReportSection(BaseModel):
    """报告的一节。"""

    title: str
    blocks: list[ReportBlock]


class ReportDocument(BaseModel):
    """一整份报告 —— 这就是 `.report.json` 里存的东西，也是前端渲染的输入。

    和 `.md` 的关系是「同一份数据的两个视图」（ADR-012 在图表上定的形状，
    这里原样复用）：Markdown 给人（可下载、可粘进 Word），
    这个结构给程序（能区分哪块是谁写的、能单独渲染某一块）。
    """

    version: int = REPORT_VERSION
    title: str
    session_id: str
    sections: list[ReportSection]
    notes: list[str] = Field(default_factory=list)
    """文档级的说明。目前用来如实记下「哪一节的正文没生成出来」——
    静默少一节，和少一张图一样，是这一轮要避免的那类问题。"""
