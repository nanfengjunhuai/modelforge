"""报告的跨语言契约 —— 后端产出的形状 vs 前端读它的形状。

════════════════════════════════════════════════════════════════════════
这个文件守的是什么
════════════════════════════════════════════════════════════════════════
M6a 让「生成报告」从事件日志里投影出一串**类型化的块**，存成
`<标题>.report.json`；前端照着它渲染。

于是这份 JSON 又成了一份跨语言契约，两端分别是：

    写入端  modelforge/reports/blocks.py      —— 键名 `artifact_id` / `stop_lines`…
    读取端  web/src/lib/report-types.ts       —— `FigureBlock` / `CodeBlock`…
    外加    modelforge/reports/outline.py::name_key  ↔  artifact-format.ts::nameKey

两边**没有任何机制保证同步**，而不同步的症状是安静的：把 `artifact_id`
写成 `artifactId`，Python 全绿、tsc 全绿，只是图**永远不出现**，
而且界面上看起来就像「这份报告本来就没有图」。

════════════════════════════════════════════════════════════════════════
它比 M4/M5 的契约测试强在哪
════════════════════════════════════════════════════════════════════════
`test_event_contract.py` 只能把 Python 源码**当文本正则**（事件模型是
Pydantic 类，抠字段名要绕一圈）。这里可以**真的构造块**：

    build_outline(合成事件)  →  ReportOutline  →  render_document  →  真的块

断言的是**真实产出的键集合**，而不是我对源码的解读。
`test_chart_contract.py` 是最接近的先例，这一份沿用它整套手法。
"""

from __future__ import annotations

import re
from typing import Any, get_args

import pytest

from modelforge.artifacts.base import ArtifactRef
from modelforge.paths import PROJECT_ROOT
from modelforge.providers.events import ToolResult
from modelforge.reports import (
    BLOCK_KINDS,
    build_outline,
    find_unverified_numbers,
    render_document,
)
from modelforge.reports.blocks import ReportBlock, ReportDocument, ReportSection
from modelforge.reports.outline import name_key
from modelforge.sessions.base import (
    LogAssistant,
    LogDecisionAnswer,
    LogDecisionRequest,
    LogReport,
    LogTool,
    LogUser,
    StoredEvent,
    parse_log_event,
)

REPORT_TYPES_TS = PROJECT_ROOT / "web" / "src" / "lib" / "report-types.ts"
ARTIFACT_FORMAT_TS = PROJECT_ROOT / "web" / "src" / "lib" / "artifact-format.ts"
OUTLINE_PY = PROJECT_ROOT / "modelforge" / "reports" / "outline.py"


# ══════════════════════════════════════════════════════ 合成一份会话


def _ref(name: str, *, kind: str = "image") -> ArtifactRef:
    return ArtifactRef(id="a" * 32, name=name, kind=kind, mime="image/png", size=1234)


def _session() -> list[StoredEvent]:
    """一个最小但**四节齐全**的会话：说过话、拍过板、跑过代码、出过图。

    刻意四节都凑齐 —— 只测一节的话，另外三节的块类型漏了契约也没人发现。
    """
    events = [
        LogUser(content="用熵权法给我这组数据算个权重"),
        LogDecisionRequest(
            call_id="c1", question="用哪种赋权方法？", options=["熵权法", "AHP"]
        ),
        LogDecisionAnswer(call_id="c1", choice="熵权法", note="数据都是全的"),
        LogAssistant(
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "run_python", "arguments": '{"code": "print(1)"}'},
                    }
                ],
            }
        ),
        LogTool(
            message={"role": "tool", "tool_call_id": "c2", "content": "1"},
            result=ToolResult(
                call_id="c2",
                name="run_python",
                ok=True,
                stdout="权重: 0.42",
                artifacts=[_ref("权重对比.png")],
            ),
        ),
    ]
    return [
        StoredEvent(seq=index, created_at="2026-01-01T00:00:00.000000+00:00", event=event)
        for index, event in enumerate(events)
    ]


@pytest.fixture(scope="module")
def ts_source() -> str:
    assert REPORT_TYPES_TS.exists(), (
        f"找不到 {REPORT_TYPES_TS}。这个测试要同时看到前后端两个文件，"
        "确认你在完整的仓库里跑它。"
    )
    return REPORT_TYPES_TS.read_text(encoding="utf-8")


def _ts_type_fields(source: str, type_name: str) -> set[str]:
    """抠出 `export type X = { ... }` 里的顶层字段名。

    按**类型名**定位，不是按「找某个字符串」—— M5 写类似测试时踩过一次：
    锚在类型体内的某个字符串上，于是往后找的 `export type` 变成了
    **下一个**类型块，测试指着一个完全无关的类型说它少了字段。
    """
    match = re.search(rf"export type {type_name} = \{{(.*?)\n\}}", source, re.S)
    assert match, f"report-types.ts 里找不到 `export type {type_name} = {{...}}`"
    return set(re.findall(r"^\s*(\w+)\??:", match.group(1), re.M))


# ══════════════════════════════════════════════════════ 契约


def test_every_block_kind_is_declared_on_both_sides(ts_source: str):
    """**本文件的核心断言**：两边认得的块类型必须一模一样。

    红的含义很具体：某一侧加了新块而另一侧没跟上。前端的 `switch` 遇到
    不认识的 `kind` 会落到 default —— 那一块**静默消失**，而报告看起来
    只是「这里内容少了一点」。
    """
    ts_side = set(re.findall(r"'(\w+)'", re.search(
        r"export const BLOCK_KINDS = \[(.*?)\] as const", ts_source, re.S
    ).group(1)))  # type: ignore[union-attr]

    assert set(BLOCK_KINDS) == ts_side, (
        f"\n后端有、前端没有：{sorted(set(BLOCK_KINDS) - ts_side)}"
        f"\n前端有、后端没有：{sorted(ts_side - set(BLOCK_KINDS))}"
        f"\n（前端文件：{REPORT_TYPES_TS}）"
    )


@pytest.mark.parametrize(
    "python_name",
    ["ProseBlock", "DecisionBlock", "FigureBlock", "CodeBlock", "NoteBlock"],
)
def test_block_fields_match(ts_source: str, python_name: str):
    """每种块的字段名要一一对上。

    这一层漏掉的表现比上一层更隐蔽：`artifact_id` 写成 `artifactId` 之后，
    图**不报错、不出现**，而界面上看起来就像「这份报告本来就没有图」。
    """
    python_side = _python_block_fields(python_name)
    ts_side = _ts_type_fields(ts_source, python_name)

    assert python_side == ts_side, (
        f"\n后端 {python_name} 有、前端没声明：{sorted(python_side - ts_side)}"
        f"\n前端声明、后端没有：{sorted(ts_side - python_side)}"
    )


def _python_block_fields(name: str) -> set[str]:
    """从 `ReportBlock` 那个判别联合里取出某个成员类的字段名集合。

    **真的去联合里找**，而不是 `import ProseBlock` 之后读字段 ——
    后者会漏掉「类定义对了、但忘了加进联合」这种错，
    而那正是「加了一种新块」时最容易漏的一步。
    """
    for member in get_args(get_args(ReportBlock)[0]):
        if member.__name__ == name:
            return set(member.model_fields)
    raise AssertionError(f"ReportBlock 联合里没有 {name} —— 加了类但忘了加进联合？")


def test_document_and_section_fields_match(ts_source: str):
    """文档和节的结构也要对得上（前端整份文档就是照这个渲染的）。"""
    for python_cls, ts_name in (
        (ReportSection, "ReportSection"),
        (ReportDocument, "ReportDocument"),
    ):
        python_side = set(python_cls.model_fields)
        ts_side = _ts_type_fields(ts_source, ts_name)
        assert python_side == ts_side, (
            f"\n后端 {ts_name} 有、前端没声明：{sorted(python_side - ts_side)}"
            f"\n前端声明、后端没有：{sorted(ts_side - python_side)}"
        )


def test_the_real_pipeline_produces_only_declared_blocks(ts_source: str):
    """**整条流水线跑一遍**，产出的块类型必须是前端声明过的那些。

    上面几条测的是「类型定义对不对」，这一条测的是**真实产出**——
    将来有人在 `outline.py` 里插了一种新块却忘了两边同步，
    这条会最先红。
    """
    outline = build_outline(
        _session(), session_id="s1", title="熵权法评价", captions={"权重对比": "熵权法权重"}
    )
    doc = render_document(outline, prose={s.title: "正文。" for s in outline.sections})

    produced = {
        block.kind
        for section in doc.sections
        for block in section.blocks
    }
    assert produced <= set(BLOCK_KINDS), (
        f"产出了没在前端声明的块类型：{sorted(produced - set(BLOCK_KINDS))}"
    )
    # 四节都该在（合成数据是刻意凑齐的），否则上面那条可能因为「什么都没产出」
    # 而假绿 —— 一个空集合永远是任何集合的子集。
    assert len(doc.sections) == 4, [s.title for s in doc.sections]
    assert {"decision", "figure", "code"} <= produced


def test_name_key_matches_between_python_and_typescript():
    """产物分组键的规则两边必须一致。

    它决定「`.png` 能不能配到自己的 `.chart.json` 的 caption」。
    两边不一致的症状是**报告里的图全都没有说明**，而且没人会觉得那是 bug ——
    多数报告里的图本来就只有编号。
    """
    source = ARTIFACT_FORMAT_TS.read_text(encoding="utf-8")
    assert "nameKey" in source, (
        f"{ARTIFACT_FORMAT_TS} 里没有 nameKey —— 产物面板按它分组，"
        "报告里的图也靠它配 caption"
    )
    # TS 那边的正则形态：先去最后一节扩展名，再砍掉尾巴上的 .chart。
    # 只要它提到这两步，规则就还在（逐字比对两种语言的实现没有意义）。
    body = source[source.index("nameKey") :]
    body = body[: body.index("\n}") if "\n}" in body else len(body)]
    assert ".chart" in body, "nameKey 没有处理 `.chart.json` 这个双扩展名"
    assert "lastIndexOf" in body or "substring" in body or "slice" in body

    # 顺带把三个真实形态钉下来 —— Python 这一侧的行为是契约。
    assert name_key("权重对比.png") == name_key("权重对比.chart.json")
    assert name_key("权重对比.chart.json") == "权重对比"


def test_the_log_report_event_is_in_the_ts_mirror():
    """`LogReport` 这一条也要出现在前端的日志镜像里。

    ⚠️ 这条比看上去重要：`test_event_contract.py` 比对的是**两边的 kind 集合**，
    而它只能证明「前端有一个叫 report 的类型」。这里补的是
    「它真的有 document / sidecar 两个产物引用」—— 少了它们，
    前端就没法从产物清单里认出「这一份是报告、可以点开读」。
    """
    source = (PROJECT_ROOT / "web" / "src" / "lib" / "log-types.ts").read_text(
        encoding="utf-8"
    )
    assert _ts_type_fields(source, "LogReportEvent") == set(LogReport.model_fields)


# ══════════════════════════════════════════════════════ 边界（行为）


def test_invented_numbers_are_flagged_but_rounded_ones_are_not():
    """核对器要抓「材料里没有的数字」，**但不能抓四舍五入**。

    这两件事的边界就是它的全部价值。抓得太狠的话，用户第一次看到误报
    就不再相信这个提示了 —— 而一个被无视的检查比没有检查更糟，
    因为它还占着屏幕。
    """
    material = "权重: 0.4200\n样本量: 8\n第一年 2024 年"

    # 材料里没有的 → 标出来
    assert find_unverified_numbers("权重约为 0.7779。", material) == ["0.7779"]

    # 材料里有，只是写法不同（0.4200 vs 0.42）→ **不**标
    assert find_unverified_numbers("权重是 0.42。", material) == []

    # 小整数一律放过（「第 3 步」「5 个指标」）—— 它们占误报的绝大多数
    assert find_unverified_numbers("第 3 步算了 5 个指标。", material) == []

    # 大整数要标（材料里只有 8 和 2024）
    assert find_unverified_numbers("共 999 个样本。", material) == ["999"]


def test_the_number_checker_keeps_order_and_dedupes():
    """同一段里重复出现的数字只报一次，且保持**首次出现**的顺序。

    界面上是一句人话（「这几个数字找不到出处：……」），
    顺序乱掉或者重复三遍都会让它读起来像机器在报错。
    """
    result = find_unverified_numbers("先看 0.91，再看 0.73，最后回到 0.91。", "无关材料")
    assert result == ["0.91", "0.73"]


def test_empty_sections_are_dropped():
    """没有材料的章节**整个丢掉**，不留一个只有标题的空壳。

    「二、模型假设与方案选择」下面空空如也，读起来像生成失败了；
    而没有那一节，读起来只是「这次建模没做过需要拍板的决定」——
    后者才是事实。
    """
    only_chat = [
        StoredEvent(
            seq=0,
            created_at="2026-01-01T00:00:00.000000+00:00",
            event=LogUser(content="你好"),
        )
    ]
    outline = build_outline(only_chat, session_id="s1", title="闲聊")

    titles = [section.title for section in outline.sections]
    assert titles == ["一、问题重述"], titles


def test_a_pending_decision_never_reaches_the_report():
    """**没答复的决策不进报告。**

    报告记的是「已经做过的事」。把一条悬而未决的提问放进去，
    读起来像是它被回答过 —— 而那是编造，只不过编造的是流程而不是数字。
    """
    items = [
        StoredEvent(
            seq=0,
            created_at="2026-01-01T00:00:00.000000+00:00",
            event=LogUser(content="选一个"),
        ),
        StoredEvent(
            seq=1,
            created_at="2026-01-01T00:00:00.000000+00:00",
            event=LogDecisionRequest(call_id="c9", question="用哪个？", options=["A", "B"]),
        ),
    ]
    outline = build_outline(items, session_id="s1", title="t")
    assert [s.title for s in outline.sections] == ["一、问题重述"]


def test_the_report_event_round_trips_through_json():
    """`LogReport` 要能原样存进日志再读回来。

    `sqlite_store` 存的是 `model_dump_json()`，读回来走 `parse_log_event`。
    两个产物引用的字段一个都不能丢 —— 丢了的话，日志里那条记录还在、
    报告在界面上却点不开。
    """
    ref = _ref("报告.md", kind="document")
    sidecar = _ref("报告.report.json", kind="data")
    event: Any = LogReport(
        title="熵权法评价",
        document=ref,
        sidecar=sidecar,
        provider="deepseek",
        model="deepseek-chat",
    )

    restored = parse_log_event(event.model_dump_json())
    assert isinstance(restored, LogReport)
    assert restored == event
    assert restored.document.id == ref.id
    assert restored.sidecar.name == "报告.report.json"
