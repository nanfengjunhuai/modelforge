"""`.chart.json` 的契约 —— 沙箱写出来的形状 vs 前端读它的形状。

════════════════════════════════════════════════════════════════════════
这个文件守的是什么
════════════════════════════════════════════════════════════════════════
M5 让 `mp.save(fig, name, chart={...})` 在存图的同时写一份
`<名字>.chart.json`（ADR-012），M5b 让工作台把它渲染成可交互的图。
于是这份 JSON 成了一份**跨语言的契约**，而它的两端分别是：

    写入端  modelforge/sandbox/runtime/modelforge_plot.py
            `_build_chart_spec()` —— 键名 `x_label` / `series[].data` …
    读取端  web/src/lib/chart-spec.ts
            `ChartSpec` / `ChartSeries` —— 字段名一模一样地抄一遍

两边**没有任何机制保证同步**。而不同步的症状是安静的：把 `x_label`
写成 `xLabel`，TS 那边编译全绿、Python 这边测试全绿，
只是图上的横轴标签**永远是空的**，而且没人会觉得那是 bug ——
多数图表本来就不标轴名。

════════════════════════════════════════════════════════════════════════
它比 M4/M5 那两个契约测试强在哪
════════════════════════════════════════════════════════════════════════
`test_event_contract.py` 只能把 Python 源码**当文本正则**（事件模型是
Pydantic 类，抠字段名要绕一圈）。这里可以**真的调用那个函数**：

  · `modelforge_plot.py` 在后端 venv 里能干净 import —— 它对 matplotlib
    的依赖全在函数体内（顶层只有 json / math / pathlib / typing）。
  · `_axis_labels(fig)` 只做 `getattr(fig, "axes", None)`，**鸭子类型**，
    所以传一个 `axes=[]` 的假对象就能跑，全程不需要 matplotlib。

于是断言的是**真实产出的键集合**，而不是我对源码的解读。
「读回来对」这个判据，M5 已经用一整个 `test_matplotlib_style.py` 证明过比
「有没有报错」可靠得多。
"""

from __future__ import annotations

import importlib.util
import re
from typing import Any, ClassVar

import pytest

from modelforge.paths import PROJECT_ROOT

RUNTIME_DIR = PROJECT_ROOT / "modelforge" / "sandbox" / "runtime"
PLOT_MODULE = RUNTIME_DIR / "modelforge_plot.py"
CHART_SPEC_TS = PROJECT_ROOT / "web" / "src" / "lib" / "chart-spec.ts"


# ══════════════════════════════════════════════════════ 沙箱侧


@pytest.fixture(scope="module")
def plot_module() -> Any:
    """真的导入 `modelforge_plot` 一次（不是正则抠源码）。

    导入会**执行整个模块**，所以语法错误、拼错的常量名、误加的顶层
    import 都会在这里暴露 —— 这些是正则证明不了的。
    """
    spec = importlib.util.spec_from_file_location("_mf_plot_chart_contract", PLOT_MODULE)
    assert spec and spec.loader, f"加载不了 {PLOT_MODULE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeFigure:
    """`_axis_labels()` 只要有 `axes` 这个属性就够了。

    这就是它能被离线测的原因：真实的轴标签是从 figure 里读的
    （不让模型再传一遍），而那一步只依赖这一个属性。
    这里给一个空列表 —— 于是轴标签是空串，正好也是「模型没设轴名」那条路径。
    """

    # ClassVar 而不是实例属性：这个假对象没有 __init__，
    # 而一个可变的类属性会被 ruff 判为「所有实例共享同一个列表」——
    # 这里确实是共享的（而且没人改它），所以用 ClassVar 把意图写明。
    axes: ClassVar[list[Any]] = []


def build_spec(plot_module: Any, chart: dict[str, Any]) -> dict[str, Any]:
    return plot_module._build_chart_spec(
        chart, title="权重对比", caption="", fig=_FakeFigure()
    )


# ══════════════════════════════════════════════════════ 前端侧


@pytest.fixture(scope="module")
def ts_source() -> str:
    assert CHART_SPEC_TS.exists(), (
        f"找不到 {CHART_SPEC_TS}。这个测试要同时看到前后端两个文件，"
        "确认你在完整的仓库里跑它。"
    )
    return CHART_SPEC_TS.read_text(encoding="utf-8")


def _ts_type_fields(source: str, type_name: str) -> set[str]:
    """抠出 `export type X = { ... }` 里的顶层字段名。

    按**类型名**定位，不是按「找某个字符串」—— M5 写类似测试时踩过一次：
    锚在类型体内的某个字符串上，于是往后找的 `export type` 变成了
    **下一个**类型块，测试指着一个完全无关的类型说它少了字段。
    """
    match = re.search(rf"export type {type_name} = \{{(.*?)\n\}}", source, re.S)
    assert match, f"chart-spec.ts 里找不到 `export type {type_name} = {{...}}`"
    return set(re.findall(r"^\s*(\w+)\??:", match.group(1), re.M))


def _ts_union_members(source: str, name: str) -> set[str]:
    """抠出 `export const X = ['a', 'b'] as const` 里的字面量。"""
    match = re.search(rf"export const {name} = \[(.*?)\] as const", source, re.S)
    assert match, f"chart-spec.ts 里找不到 `export const {name} = [...] as const`"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


# ══════════════════════════════════════════════════════ 契约


def test_chart_spec_keys_match(plot_module: Any, ts_source: str):
    """**本文件的核心断言**：写入端产出的键 == 读取端声明的字段。

    红的含义很具体：某一侧改了键名而另一侧没跟着改。
    最常见的是 `x_label` ↔ `xLabel` 这种大小写分歧 ——
    它不会让任何东西编译不过，只会让轴标签永远是空串。
    """
    produced = build_spec(
        plot_module, {"kind": "bar", "series": [{"name": "AHP", "data": [1, 2]}]}
    )
    declared = _ts_type_fields(ts_source, "ChartSpec")

    # `categories` 和 `caption` 是可选的，只在给了的时候才出现 ——
    # 所以这里比的是**必然出现的那几个键**是声明的子集，
    # 再加上「可选的那两个必须也在类型里声明」。
    required = {"version", "kind", "title", "x_label", "y_label", "series"}
    assert required <= produced.keys(), (
        f"写入端少产出了：{sorted(required - produced.keys())}"
    )
    assert required <= declared, f"读取端少声明了：{sorted(required - declared)}"

    optional = {"categories", "caption"}
    assert optional <= declared, (
        f"可选字段没在 ChartSpec 里声明：{sorted(optional - declared)}"
    )
    assert produced.keys() <= declared, (
        f"\n写入端产出了、读取端没声明的键：{sorted(produced.keys() - declared)}"
        f"\n（读取端：{CHART_SPEC_TS}）"
    )


def test_categories_and_caption_use_the_same_keys(plot_module: Any, ts_source: str):
    """可选字段也要验一遍 —— 「只在给了的时候才出现」正是漏测的温床。

    上面那条用的是「最简 chart」，`categories` / `caption` 都没给，
    所以它俩压根没进产出的键集合。这里把两个都给上再比一次。
    """
    produced = build_spec(
        plot_module,
        {
            "kind": "line",
            "series": [{"name": "A", "data": [1, 2, 3]}],
            "categories": ["甲", "乙", "丙"],
        },
    )
    # caption 走的是 save() 的参数，这里直接调 _build_chart_spec 传进去
    with_caption = plot_module._build_chart_spec(
        {"kind": "line", "series": [{"name": "A", "data": [1]}]},
        title="t",
        caption="一句话说明",
        fig=_FakeFigure(),
    )
    assert "categories" in produced
    assert "caption" in with_caption

    declared = _ts_type_fields(ts_source, "ChartSpec")
    assert {"categories", "caption"} <= declared


def test_series_keys_match(plot_module: Any, ts_source: str):
    """`series` 里每一项的字段名同样要对得上。

    这一层漏掉的表现比上一层更隐蔽：`data` 写成 `values` 的话，
    图上什么都没有，而错误信息会指向一个完全无关的地方。
    """
    produced = build_spec(
        plot_module, {"kind": "bar", "series": [{"name": "AHP", "data": [1, 2]}]}
    )
    produced_keys = set(produced["series"][0])
    declared = _ts_type_fields(ts_source, "ChartSeries")

    assert produced_keys == declared, (
        f"\n写入端产出、读取端没声明：{sorted(produced_keys - declared)}"
        f"\n读取端声明、写入端没产出：{sorted(declared - produced_keys)}"
    )


def test_chart_kinds_match(plot_module: Any, ts_source: str):
    """两边认得的图型必须一模一样。

    ⚠️ 这一条盯的是一个**已知的薄弱点**：`_build_chart_spec` 目前
    **不校验** `kind` —— 任何非空字符串都放行（只打一句警告）。
    也就是说「写入端认得的」是一份**文档约定**，不是类型约束。

    所以这条测试的意义是：哪天有人把 kind 收窄成 `Literal`、
    或者在这里加了第五种图型，两边会立刻对不上并变红，
    逼着人把前端一起改了。前端不认得的图型，用户就只看到一张表。
    """
    python_side = set(plot_module.CHART_KINDS)
    ts_side = _ts_union_members(ts_source, "KNOWN_CHART_KINDS")

    assert python_side == ts_side, (
        f"\n沙箱有、前端没有：{sorted(python_side - ts_side)}"
        f"\n前端有、沙箱没有：{sorted(ts_side - python_side)}"
        f"\n（前端文件：{CHART_SPEC_TS}）"
    )


def test_grayscale_line_styles_are_mirrored(plot_module: Any, ts_source: str):
    """折线的「线型 × 标记」表两边要**一样长**。

    这张表（`GRAYSCALE_STYLES` ↔ `LINE_STYLES`）是数模论文黑白打印时
    唯一的辨认手段，也是 ADR-006「图表不能只靠颜色区分系列」的落地。
    两边的表长不一样的话，第 N 个系列在静态图和交互图上会拿到**不同的
    线型** —— 用户切换视图时得重新认一遍，而且会觉得是自己记错了。

    只比长度不比内容：绘制库不同（matplotlib 的 `':'` vs ECharts 的
    `'dotted'`），逐项比对没有意义，而**长度**是「按序号取模」的那个周期。
    """
    python_side = len(plot_module.GRAYSCALE_STYLES)
    match = re.search(r"const LINE_STYLES[^=]*=\s*\[(.*?)\n\]", ts_source, re.S)
    assert match, "chart-spec.ts 里找不到 LINE_STYLES"
    ts_side = len(re.findall(r"\{\s*dash:", match.group(1)))

    assert python_side == ts_side, (
        f"沙箱那边 {python_side} 组，前端 {ts_side} 组 —— "
        "两边的周期对不上，同一个系列会拿到不同的线型"
    )


# ══════════════════════════════════════════════════════ 边界（行为）


def test_non_finite_values_are_rejected_with_a_readable_message(plot_module: Any):
    """NaN / Infinity 必须被**拒绝**，而不是静默写进 JSON。

    这条守的是一个真发生过的坑：Python 的 `json.dumps` 默认
    `allow_nan=True`，会把 `NaN` / `Infinity` **原样写出去** ——
    而这两个字面量**不是合法 JSON**，浏览器那边 `JSON.parse` 直接抛。

    后果的形状很坏：PNG 好好的、`.chart.json` 也在磁盘上、Python 这边
    读回来也没问题，**只有浏览器读不了**；而前端的报错是
    「读不到这份数据（文件可能已经不在了）」—— 一句假话。

    所以这里同时断言两件事：**拒绝了**，以及**拒绝的理由里提到了来源**
    （`np.nan` / `0/0` 之类），因为模型看到的是这句话，它得能照着改。
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError) as caught:
            build_spec(
                plot_module,
                {"kind": "bar", "series": [{"name": "A", "data": [1.0, bad]}]},
            )
        message = str(caught.value)
        assert "非有限值" in message
        assert "nan" in message.lower() or "0/0" in message


def test_an_unknown_kind_is_accepted_but_warned_about(
    plot_module: Any, capsys: pytest.CaptureFixture[str]
):
    """未知 `kind` **不能抛异常**（图是主产物），但**必须说出来**。

    ADR-012 定过这个取舍：`chart` 参数只影响可交互版本，为了一份附带的
    东西让模型那段脚本当场死掉、后面所有的图都不画，是错的。

    但「不报错」不等于「不吭声」：数据存下来了、工作台却渲染不出来，
    这件事模型和用户都该知道。所以断言的是**警告确实打了**。
    """
    spec = build_spec(
        plot_module, {"kind": "radar", "series": [{"name": "A", "data": [1, 2]}]}
    )
    assert spec["kind"] == "radar", "未知 kind 应该原样存下来"
    assert "radar" in capsys.readouterr().out, "未知 kind 应该打一句警告"


def test_the_dataset_key_set_is_exactly_what_the_frontend_parses(
    plot_module: Any, ts_source: str
):
    """`parseChartSpec` 认识的键，必须覆盖写入端会产出的**全部**键。

    前端那个运行时校验函数（`parseChartSpec`）是最后一道防线 ——
    它把来路不明的 JSON 收窄成 `ChartSpec`。如果它不认某个键，
    那个键就会被**静默丢掉**：数据还在文件里，界面上却没有。

    所以这里把 `parseChartSpec` 读到的键名抠出来，和真实产出比对。
    """
    produced = build_spec(
        plot_module,
        {
            "kind": "bar",
            "series": [{"name": "A", "data": [1, 2]}],
            "categories": ["甲", "乙"],
        },
    )

    match = re.search(
        r"export function parseChartSpec.*?\n\}", ts_source, re.S
    )
    assert match, "chart-spec.ts 里找不到 parseChartSpec"
    # `record.<key>` 是那个函数读取输入的唯一方式，所以抠它最准确。
    parsed_keys = set(re.findall(r"record\.(\w+)", match.group(0)))

    assert produced.keys() <= parsed_keys, (
        f"\n写入端会产出、parseChartSpec 却不读的键："
        f"{sorted(produced.keys() - parsed_keys)}"
        "\n这些键会在解析那一步被静默丢掉。"
    )
