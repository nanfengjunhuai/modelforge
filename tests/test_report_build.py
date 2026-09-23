"""报告投影与渲染的纯函数测试。

`tests/test_report_contract.py` 守的是**跨语言的形状**（这个文件产出的东西，
前端读不读得懂）。这里守的是**投影规则本身**：材料从哪来、配得对不对、
截断截在哪、Markdown 长什么样。

两件事分开写，是因为它们的失败方式完全不同：

    契约红了   → 前端会读到一个不存在的字段（图不出现、块消失）
    规则错了   → 内容都在，但**配错了对象**（A 图的说明挂在 B 图下面）

后一种更坏：它不报错、不缺东西，只是把一件真事安在了错误的地方。
一份把熵权法的权重说明挂在 AHP 那张图下面的报告，读起来毫无破绽。
"""

from __future__ import annotations

import json

from modelforge.artifacts.base import ArtifactRef
from modelforge.providers.events import ToolResult
from modelforge.reports import (
    CodeBlock,
    FigureBlock,
    NoteBlock,
    ProseBlock,
    build_outline,
    render_document,
    render_markdown,
)
from modelforge.reports.outline import head_lines, name_key, tail_lines
from modelforge.sessions.base import (
    LogAssistant,
    LogDecisionAnswer,
    LogDecisionRequest,
    LogTool,
    LogUser,
    StoredEvent,
)

STAMP = "2026-01-01T00:00:00.000000+00:00"


def _wrap(events: list[object]) -> list[StoredEvent]:
    return [
        StoredEvent(seq=index, created_at=STAMP, event=event)  # type: ignore[arg-type]
        for index, event in enumerate(events)
    ]


def _ref(name: str, *, kind: str = "image", artifact_id: str = "a" * 32) -> ArtifactRef:
    return ArtifactRef(id=artifact_id, name=name, kind=kind, mime="image/png", size=100)  # type: ignore[arg-type]


def _tool_call(call_id: str, code: str) -> LogAssistant:
    return LogAssistant(
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_python",
                        "arguments": json.dumps({"code": code}),
                    },
                }
            ],
        }
    )


def _tool_result(call_id: str, stdout: str, artifacts: list[ArtifactRef] | None = None) -> LogTool:
    return LogTool(
        message={"role": "tool", "tool_call_id": call_id, "content": stdout},
        result=ToolResult(
            call_id=call_id,
            name="run_python",
            ok=True,
            stdout=stdout,
            artifacts=artifacts or [],
        ),
    )


# ══════════════════════════════════════════════════════ 配 caption


def test_a_caption_lands_on_the_right_figure():
    """**caption 必须配到它自己的那张图上。**

    这是本文件里最要紧的一条。caption 在**另一份产物**（`.chart.json`）里，
    两份之间唯一的联系是显示名 —— 配错的话，报告里每张图都带着别人的说明，
    而且**完全看不出来**：说明本身是通顺的、图也是对的，只是对不上号。

    所以这里放两张图，确认它们的说明没有互换。
    """
    events = _wrap(
        [
            LogUser(content="画两张图"),
            _tool_call("c1", "plot_a()"),
            _tool_result(
                "c1", "ok", [_ref("熵权法权重.png", artifact_id="a" * 32)]
            ),
            _tool_call("c2", "plot_b()"),
            _tool_result(
                "c2", "ok", [_ref("AHP权重.png", artifact_id="b" * 32)]
            ),
        ]
    )
    outline = build_outline(
        events,
        session_id="s1",
        title="t",
        # ⚠️ 键必须是 `name_key(...)` 的结果而不是显示名本身 ——
        # `name_key` 会把大小写拉平（`AHP权重` → `ahp权重`），
        # 所以直接拿显示名当键会**静默查不到**。`_collect_captions` 就是这么建的。
        captions={
            name_key("熵权法权重"): "熵权法算出的指标权重",
            name_key("AHP权重"): "AHP 算出的指标权重",
        },
    )

    figures = [
        block
        for section in outline.sections
        for block in section.blocks
        if isinstance(block, FigureBlock)
    ]
    assert [(f.name, f.caption) for f in figures] == [
        ("熵权法权重.png", "熵权法算出的指标权重"),
        ("AHP权重.png", "AHP 算出的指标权重"),
    ]


def test_a_chart_json_caption_matches_its_sibling_png():
    """`.chart.json` 和跟它同名的 `.png` 要认出彼此。

    这正是 `name_key` 存在的理由：`权重对比.chart.json` 直接去扩展名
    得到的是 `权重对比.chart`，和 `权重对比` 配不上。忘了这一步的话，
    **报告里的图全都没有说明** —— 而多数报告里的图本来就只有编号，
    所以没人会觉得那是 bug。
    """
    events = _wrap(
        [
            LogUser(content="画图"),
            _tool_call("c1", "plot()"),
            _tool_result("c1", "ok", [_ref("权重对比.png")]),
        ]
    )
    outline = build_outline(
        events,
        session_id="s1",
        title="t",
        # caption 表是按**产物的显示名**建的，键是 `.chart.json` 那一份
        captions={"权重对比": "熵权法算出的指标权重"},
    )

    figures = [
        b for s in outline.sections for b in s.blocks if isinstance(b, FigureBlock)
    ]
    assert len(figures) == 1
    assert figures[0].caption == "熵权法算出的指标权重"


def test_name_key_folds_case_and_strips_only_the_chart_suffix():
    """把 `name_key` 的两条规则钉下来 —— 它们都是**故意的**。

    大小写拉平（`AHP权重` 和 `ahp权重` 归一组）：这是分组键不是文件名，
    面板上一张图占三行已经够乱了，再因为大小写分成两组更没道理。
    代价是「只差大小写的两个文件会被并成一组」，对模型生成的名字来说
    不可能撞上。

    只砍 `.chart` 这一个后缀：别的多节扩展名（`data.v2.csv`）保留原样，
    不能一路砍到第一个点 —— 那会把 `data.v2` 和 `data` 错误地并起来。
    """
    assert name_key("AHP权重.png") == name_key("ahp权重.chart.json") == "ahp权重"
    assert name_key("data.v2.csv") == "data.v2"
    assert name_key("无扩展名") == "无扩展名"


def test_a_missing_caption_is_an_empty_string_not_an_error():
    """读不到 caption 就留空串 —— **不能抛**。

    少一句图说明是小事，因为一个读不到的文件让整份报告生成不出来才是大事。
    这和 M5 的取舍一致：`chart` 参数写歪了也不弄死模型那段脚本。
    """
    events = _wrap(
        [
            LogUser(content="画图"),
            _tool_call("c1", "plot()"),
            _tool_result("c1", "ok", [_ref("没有 chart.json 的图.png")]),
        ]
    )
    outline = build_outline(events, session_id="s1", title="t", captions={})
    figures = [
        b for s in outline.sections for b in s.blocks if isinstance(b, FigureBlock)
    ]
    assert figures[0].caption == ""


# ══════════════════════════════════════════════════════ 配代码与结果


def test_code_and_result_are_paired_by_call_id_not_by_order():
    """代码和执行结果在**两种事件**里，靠 `call_id` 配对。

    按顺序配（「第 N 个 assistant 配第 N 个 tool」）在顺序一致时也能跑通 ——
    而顺序**恰好**一致是常态，所以这个错法能活很久。它只在有工具调用被跳过
    （比如两轮之间有别的消息、或者某次调用没落盘）时才暴露，
    而那时配错了的代码和输出**各自都是真的**，只是不是一对。
    """
    events = _wrap(
        [
            LogUser(content="跑两段"),
            # 故意把两段代码**倒序**声明，再正序给出结果
            _tool_call("c2", "print(2)"),
            _tool_call("c1", "print(1)"),
            _tool_result("c1", "1"),
            _tool_result("c2", "2"),
        ]
    )
    outline = build_outline(events, session_id="s1", title="t")
    codes = [
        b for s in outline.sections for b in s.blocks if isinstance(b, CodeBlock)
    ]
    # 输出顺序跟的是 **tool 事件**的顺序（执行顺序），不是声明顺序。
    assert [(b.code, b.stdout) for b in codes] == [("print(1)", "1"), ("print(2)", "2")]


def test_a_broken_arguments_string_does_not_kill_the_report():
    """`arguments` 是模型生成的 JSON 字符串，**可能是坏的**。

    `max_tokens` 截断会留下一段语法不完整的 JSON —— 而那时报告不该整个失败：
    那段文本本身就有用（它说明了模型当时写了什么）。所以解析失败时退回原文。
    """
    events = _wrap(
        [
            LogUser(content="跑"),
            LogAssistant(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "run_python",
                                "arguments": '{"code": "print(1)',  # 故意坏掉
                            },
                        }
                    ],
                }
            ),
            _tool_result("c1", "Traceback"),
        ]
    )
    outline = build_outline(events, session_id="s1", title="t")
    codes = [b for s in outline.sections for b in s.blocks if isinstance(b, CodeBlock)]
    assert codes[0].code == '{"code": "print(1)'


def test_truncation_keeps_the_tail_of_output_and_the_head_of_code():
    """输出留尾、代码留头 —— 两处都**如实说明砍了多少**。

    留错了方向的话没人会发现：输出里全是中间过程（看不到结论），
    代码全是尾巴（看不到入口），而两种都**看起来很完整**。
    """
    code = "\n".join(f"line{i}" for i in range(200))
    assert head_lines(code, 80).startswith("line0")
    assert "其余 120 行略" in head_lines(code, 80)

    out = "\n".join(f"out{i}" for i in range(200))
    assert tail_lines(out, 40).endswith("out199")
    assert "前 160 行略" in tail_lines(out, 40)


# ══════════════════════════════════════════════════════ 渲染


def _doc_with_prose(prose: str, material: str = "材料"):
    events = _wrap([LogUser(content="题目")])
    outline = build_outline(events, session_id="s1", title="熵权法")
    outline.sections[0].brief = material
    return render_document(outline, prose={"一、问题重述": prose})


def test_prose_comes_before_the_program_blocks():
    """一节读起来是「先说这件事，再附证据」。

    反过来的话，读者先看到一大段代码，不知道它在说什么 ——
    而正文在最后出现，读起来像是事后补的说明。
    """
    events = _wrap(
        [
            LogUser(content="题目"),
            _tool_call("c1", "print(1)"),
            _tool_result("c1", "1"),
        ]
    )
    outline = build_outline(events, session_id="s1", title="t")
    doc = render_document(outline, prose={s.title: "正文。" for s in outline.sections})

    solution = next(s for s in doc.sections if "建立与求解" in s.title)
    assert isinstance(solution.blocks[0], ProseBlock)
    assert isinstance(solution.blocks[1], CodeBlock)


def test_invented_numbers_get_a_note_right_after_the_prose():
    """核对结果紧跟在正文后面 —— 那个位置用户才看得见它。

    放到整篇末尾的话，用户读到「四、结论」时早就忘了第三节里那个数字。
    """
    doc = _doc_with_prose("权重是 0.7779。", material="权重: 0.4200")
    blocks = doc.sections[0].blocks
    assert isinstance(blocks[0], ProseBlock)
    assert isinstance(blocks[1], NoteBlock)
    assert "0.7779" in blocks[1].text
    # 措辞必须是弱的那一句 —— 说是「找不到出处」，不是「编造」。
    assert "找不到出处" in blocks[1].text


def test_a_clean_section_gets_no_note():
    """核对通过时**不要**插一条「没有问题」。

    那种「一切正常」的提示只会训练用户忽略这个位置 ——
    等它真的有话要说时，用户已经不看了。
    """
    doc = _doc_with_prose("权重是 0.42。", material="权重: 0.4200")
    assert not any(isinstance(b, NoteBlock) for b in doc.sections[0].blocks)


def test_markdown_has_the_pieces_a_paper_needs():
    events = _wrap(
        [
            LogUser(content="用熵权法算权重"),
            LogDecisionRequest(call_id="c1", question="用哪种赋权？", options=["熵权法", "AHP"]),
            LogDecisionAnswer(call_id="c1", choice="熵权法", note="数据都是全的"),
            _tool_call("c2", "print(1)"),
            _tool_result("c2", "1", [_ref("权重对比.png")]),
        ]
    )
    outline = build_outline(
        events,
        session_id="s1",
        title="熵权法评价",
        captions={"权重对比": "熵权法算出的指标权重"},
    )
    md = render_markdown(
        render_document(outline, prose={s.title: "生成的一行正文。" for s in outline.sections})
    )

    assert md.startswith("# 熵权法评价")
    assert "## 一、问题重述" in md
    assert "## 二、模型假设与方案选择" in md
    # 决策记录：候选、用户的选择、备注，一样不能少
    assert "熵权法 ／ AHP" in md
    assert "**用户选择：** 熵权法" in md
    assert "**备注：** 数据都是全的" in md
    # 图和它自己的说明
    assert "![熵权法算出的指标权重](权重对比.png)" in md
    # 图片用相对路径 —— 开头必须说清楚，否则用户打开就是一堆裂图
    assert "相对路径" in md


def test_markdown_code_fences_survive_backticks_in_the_code():
    """代码里出现三个反引号是**会发生的**（模型打印一段 Markdown 示例）。

    不处理的话围栏会被提前关掉，后面半段代码变成正文 ——
    而渲染出来只是「排版有点怪」，没人会往「围栏被撞破」上想。
    """
    events = _wrap(
        [
            LogUser(content="跑"),
            _tool_call("c1", "print('```markdown')"),
            _tool_result("c1", "ok"),
        ]
    )
    outline = build_outline(events, session_id="s1", title="t")
    md = render_markdown(render_document(outline, prose={}))

    assert "````python" in md, "围栏没有加长，会被代码里的三个反引号撞破"
    assert "````\n" in md
