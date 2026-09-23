"""给沙箱里的模型用的绘图辅助模块。

════════════════════════════════════════════════════════════════════════
它**不是**风格的必要条件
════════════════════════════════════════════════════════════════════════

科研风格已经由 `matplotlibrc` 保证了（`MPLCONFIGDIR` 指向它），模型一行
代码都不写也是对的。这个模块只负责三件**光靠 rcParams 做不到**的事：

  ① **存对地方** —— 把图存进 `artifacts/`，产物链路才收得走。
     模型自己 `plt.savefig("chart.png")` 存到工作目录根下的话，
     执行结束时那整个目录会被删掉，图就没了。

  ② **同时吐数据** —— 存图的同时写一份 `<名字>.chart.json`。
     这样工作台里将来能用**同一批数组**画交互图，而不是让模型
     把数字重新打一遍（那样一定会对不上）。

  ③ **灰度可辨** —— 数模论文经常黑白打印，只靠颜色区分系列会糊成一片。
     `style_lines()` 按系列序号再叠一层线型和标记形状。

所以可以把它理解成「一个更方便的出口」，不是「一道必须过的关」。

════════════════════════════════════════════════════════════════════════
它跑在沙箱里，所以规矩和别处不一样
════════════════════════════════════════════════════════════════════════

这个文件**不在后端的 `.venv` 里**，它会被复制进沙箱的临时工作目录，
由 `.venv-sandbox` 的 python 执行。因此：

  · 只能 import numpy / matplotlib / 标准库 —— 沙箱里没有别的东西
  · 不能 import modelforge 的任何模块 —— 那是后端环境的东西
  · 它就是**模型拿得到的一手材料**，API 要按「模型读得懂」来设计，
    而不是按「Python 程序员读得懂」

最后一条决定了下面几个函数的参数都用具名参数、都带中文说明 ——
模型是照着 docstring 和工具描述写的。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["ARTIFACTS_DIR", "COLORS", "GRAYSCALE_STYLES", "save", "style", "style_lines"]

#: 产物目录名。**必须**是这个名字 —— 沙箱执行器扫的就是它（见
#: `modelforge/sandbox/subprocess_exec.py` 的 `_ARTIFACT_DIRNAME`）。
ARTIFACTS_DIR = "artifacts"

#: 分类色，和 `matplotlibrc` 的 `axes.prop_cycle` 是同一组值
#: （最终来自 `web/src/app/globals.css` 的 `--chart-cat-*`，ADR-006）。
#: 模型要手动指定颜色时用它，不要自己编十六进制色值。
COLORS = [
    "#2a78d6",  # 蓝
    "#1baf7a",  # 青
    "#eda100",  # 黄
    "#008300",  # 绿
    "#4a3aa7",  # 紫
    "#e34948",  # 红
    "#e87ba4",  # 品红
    "#eb6834",  # 橙
]

#: 灰度打印时用来区分系列的线型 × 标记组合。
#:
#: 4 种线型 × 8 种标记 = 32 种组合，远超数模图里可能出现的系列数。
#: 顺序本身不重要，重要的是**同一个系列每次拿到同一个组合**。
GRAYSCALE_STYLES = [
    ("-", "o"),
    ("--", "s"),
    ("-.", "^"),
    (":", "D"),
    ("-", "v"),
    ("--", "P"),
    ("-.", "X"),
    (":", "*"),
]


def style() -> None:
    """套用 ModelForge 科研风格。

    正常情况下**不需要调用** —— `MPLCONFIGDIR` 里的 `matplotlibrc` 已经
    让沙箱里所有 matplotlib 代码默认就是这套风格了。

    只有在你手动 `plt.rcdefaults()` 把配置清掉了之后才需要调用它。
    """
    import matplotlib as mpl

    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [
                "Noto Sans SC",
                "Microsoft YaHei",
                "SimHei",
                "DejaVu Sans",
            ],
            # 不设这个，坐标轴上的负号会变成方框
            "axes.unicode_minus": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.axisbelow": True,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "legend.frameon": False,
            "axes.prop_cycle": mpl.cycler(color=COLORS),
        }
    )


def style_lines(ax: Any) -> None:
    """给坐标轴上的每条线分配一套**灰度也能区分**的线型和标记。

    为什么需要它：设计系统的两条硬约束是「亮色模式有 3 个分类色对比度
    低于 3:1」和「暗色模式最差相邻色差落在危险区」，合起来就是
    **图表永远不能只靠颜色区分系列**。而数模国赛的论文经常被黑白打印 ——
    那时候所有颜色的区分度归零，只剩线型和标记能救。

    用法（画完所有线之后调一次）：

        lines = [ax.plot(x, y_i, label=name_i) for ...]
        mp.style_lines(ax)

    Args:
        ax: 一个 matplotlib 的 Axes。它上面**已经画好的**线会被重新设样式；
            在此之后画的线不受影响。
    """
    lines = list(ax.get_lines())
    if not lines:
        return

    # 数据点很多的时候每个点都画标记会糊成一团。按点数稀疏地放标记，
    # 让图上大约出现 6~10 个，既能辨认又不会盖住曲线本身。
    n = max((len(line.get_xdata()) for line in lines), default=0)
    markevery = max(1, n // 8) if n else 1

    for index, line in enumerate(lines):
        linestyle, marker = GRAYSCALE_STYLES[index % len(GRAYSCALE_STYLES)]
        line.set_linestyle(linestyle)
        line.set_marker(marker)
        line.set_markevery(markevery)
        # 标记比线稍微小一点，否则点缀会喧宾夺主
        line.set_markersize(4.0)
        line.set_markerfacecolor(line.get_color())
        line.set_markeredgecolor(line.get_color())


def save(
    fig: Any,
    name: str,
    *,
    chart: dict[str, Any] | None = None,
    caption: str = "",
    formats: tuple[str, ...] = ("png", "pdf"),
) -> Path:
    """把一张图存成产物，返回 PNG 的路径。

    ⚠️ **画完图一定要调用它**，不要直接用 `plt.savefig()`。
    自己存的话文件会落在沙箱工作目录的根下，而那整个目录在代码执行结束时
    会被删掉 —— 图就没了，用户什么也看不到。存进 `artifacts/` 才会被收走。

    同时会输出两个格式，各有各的用处：

        <名字>.png    300 dpi 位图。贴进 Word 的论文正文用这个。
        <名字>.pdf    矢量图。放 LaTeX、或者直接投期刊用这个。
                      字体以 TrueType 嵌入（`pdf.fonttype: 42`），
                      很多投稿系统拒收 Type 3 字体。

    Args:
        fig: 要保存的 figure（就是 `plt.subplots()` 返回的第二个东西上面那个，
            或者 `plt.gcf()`）。
        name: 图的名字，中文可以。**当成给用户看的标题来起**，
            比如 `"三种赋权方法的权重对比"`。不要带扩展名。
        chart: 可选。**同一个图的数据**，写成一份结构化的描述。
            它会和图片一起存下来，工作台据此画出可交互版本，
            用户能悬停看具体数值。

            形状是这样（`series` 里每项的名字要和图上图例一致）：

                chart={
                    "kind": "bar",          # bar | line | scatter | pie
                    "series": [
                        {"name": "AHP", "data": [0.42, 0.31, 0.27]},
                        {"name": "熵权法", "data": [0.38, 0.34, 0.28]},
                    ],
                    "categories": ["指标1", "指标2", "指标3"],   # 可选
                }

            ⚠️ `data` 里必须是**你算出来的真实数组**，不能凭印象重打一遍。
            这个参数存在的全部意义就是「图和数字同源」，手打就失去意义了。
            颜色不用写，会按顺序自动取调色板。
        caption: 可选的一句话说明，存进 chart.json，给报告用。
        formats: 要输出的格式，默认 PNG + PDF 都要。

    Returns:
        存好的 PNG 路径。
    """
    from matplotlib.figure import Figure

    if not isinstance(fig, Figure):
        raise TypeError(
            f"save() 的第一个参数要是一个 Figure，收到的是 {type(fig).__name__}。"
            "常见错误：把 Axes 传进来了 —— 它应该是 plt.subplots() 返回的第二个值，"
            "第一个才是 Figure；或者直接用 plt.gcf() 拿当前 figure。"
        )

    out_dir = Path(ARTIFACTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = _clean_name(name)
    written: list[str] = []

    for fmt in formats:
        target = out_dir / f"{stem}.{fmt}"
        # bbox_inches="tight" 会裁掉四周留白 —— 论文里图不占满整栏时，
        # 留白会让它在 Word 里被自动缩小。rcParams 里已经设了，这里显式
        # 再写一遍是因为 savefig() 的关键字优先级高于 rcParams，
        # 而这一条对成图效果影响很大，不想让它取决于配置有没有被改过。
        fig.savefig(target, format=fmt, bbox_inches="tight")
        written.append(f"{target.name}（{target.stat().st_size // 1024} KB）")

    if chart is not None:
        # ⚠️ 这里**故意不把异常抛出去**，而是打一条说明白问题的警告。
        #
        # 理由是两种失败的代价不对称：
        #   · 抛出异常 → 模型这段脚本当场死掉，**后面所有的图都不画了**
        #   · 只警告   → 图照存，只是少了可交互的那份数据
        #
        # 图是主产物，数据是附带的。为了一份附带的东西把主产物连同后面
        # 所有的工作一起丢掉，是错的取舍。
        #
        # 但警告必须是**能照着改的**——所以下面把原始报错原样打出来，
        # 那句报错里已经写清了哪一项不对、应该是什么形状。
        try:
            spec = _build_chart_spec(chart, title=stem, caption=caption, fig=fig)
        except (TypeError, ValueError) as exc:
            print(
                f"【chart 数据没能保存】图片已经存好了，但 chart 参数有问题：{exc}\n"
                "（这只影响工作台里的可交互版本，图片本身不受影响。"
                "如果你需要可交互版本，按上面这句提示改一下 chart 再存一次。）"
            )
        else:
            payload = out_dir / f"{stem}.chart.json"
            payload.write_text(
                json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            written.append(f"{payload.name}（{len(spec['series'])} 个系列）")

    # 打印出来是给**模型**看的 —— 它会从这里知道文件叫什么名字，
    # 从而能在回答用户时说清是哪儿张图。
    print(f"[已保存产物] {stem}：{'、'.join(written)}")
    return out_dir / f"{stem}.png"


def _clean_name(name: str) -> str:
    """把模型给的名字变成一个安全的文件名。

    ⚠️ 这**不是安全措施** —— 真正的防线在产物存储那边：落盘用的是
    uuid，模型给的名字从头到尾没有碰过文件系统（见 `artifacts/base.py`
    的模块注释）。这里做的是**整洁性**：把路径分隔符去掉，
    免得图被塞进嵌套子目录、或者在界面上显示成一个带斜杠的怪名字。
    """
    cleaned = str(name).strip().replace("/", "-").replace("\\", "-")
    cleaned = cleaned.replace("..", "-").strip(". ")
    return cleaned or "未命名图"


def _build_chart_spec(
    chart: dict[str, Any], *, title: str, caption: str, fig: Any
) -> dict[str, Any]:
    """校验并补全 chart 描述。

    「校验」在这里是**引导模型自己发现错误**，而不是拦住它 ——
    报错信息写清楚哪一项不对、应该是什么形状，模型下一次就能改对。
    这和 `sandbox/tools.py` 里 `BadArguments` 的待遇是一样的：
    参数写错是模型能自己修的问题，不该悄悄放过（图会画出来，
    但交互版本会缺数据），也不该让整段代码失败。
    """
    if not isinstance(chart, dict):
        raise TypeError(
            f"chart 参数要是一个字典，收到的是 {type(chart).__name__}。"
            '形状见 save() 的 docstring，最少要有 {"kind": "bar", "series": [...]}。'
        )

    kind = chart.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError(
            'chart 里缺少 "kind" 字段。它说明这是什么图，'
            '取值可以是 "bar" / "line" / "scatter" / "pie"。'
        )

    raw_series = chart.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise ValueError(
            'chart 里缺少 "series" 字段，或者它是空的。'
            "它应该是一个列表，每项形如 "
            '{"name": "AHP", "data": [0.42, 0.31, 0.27]}，名字要和图上图例一致。'
        )

    series: list[dict[str, Any]] = []
    for index, item in enumerate(raw_series):
        if not isinstance(item, dict):
            raise ValueError(f"series 的第 {index} 项不是字典，而是 {type(item).__name__}。")
        values = _as_sequence(item.get("data"))
        if not values:
            raise ValueError(
                f"series 的第 {index} 项缺少 data，或者 data 是空的。"
                "它必须是真实的数值数组。"
            )
        series.append(
            {
                "name": str(item.get("name", f"系列{index + 1}")),
                # 用 float() 过一遍：numpy 的标量不是 JSON 可序列化的，
                # 而模型几乎总是直接把手里的 np.ndarray 传进来。
                # 不转的话 json.dumps 会抛 TypeError，报错还很难懂。
                "data": [_number(v, index) for v in values],
                # 颜色按**序号**取，和 matplotlib 的 prop_cycle 同一套顺序，
                # 所以交互图和静态图的颜色是对得上的。
                "color": COLORS[index % len(COLORS)],
            }
        )

    # 轴标签**从图里读**，而不是让模型再传一遍。
    # 理由和「data 必须是真实数组」一样：能让两处对不上的机会越少越好。
    x_label, y_label = _axis_labels(fig)

    spec: dict[str, Any] = {
        "version": 1,
        "kind": kind,
        "title": title,
        "x_label": x_label,
        "y_label": y_label,
        "series": series,
    }

    categories = chart.get("categories")
    if categories is not None:
        spec["categories"] = [str(c) for c in categories]

    if caption:
        spec["caption"] = caption

    return spec


def _as_sequence(value: Any) -> list[Any] | None:
    """把「一串数」统一成 list；不是序列就返回 None。

    ⚠️ **不能只认 `list`。** 模型手里的数据几乎总是 numpy 数组
    （`np.array([...])`、矩阵的一行、`np.linspace` 的结果），
    而 `isinstance(np.array([1,2]), list)` 是 False —— 只认 list 的话，
    最正常的那种用法反而会被判成「格式不对」，报错还指着说
    「它必须是真实的数值数组」，而传进来的明明就是。

    所以这里按**能力**判断（能不能迭代）而不是按**类型**判断。
    字符串和字典虽然可迭代，但它们表达的不是一串数，排除掉。
    """
    if isinstance(value, (str, bytes, dict)):
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if hasattr(value, "__iter__"):
        try:
            return list(value)
        except TypeError:  # pragma: no cover —— 极少数迭代器会在这里报错
            return None
    return None


def _number(value: Any, series_index: int) -> float:
    """把任意数值转成 float，转不了就给出人话报错。"""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"series 第 {series_index} 项的 data 里有非数值：{value!r}（{type(value).__name__}）。"
            "如果这是类别标签，它应该放进 chart 的 categories 字段。"
        ) from exc


def _axis_labels(fig: Any) -> tuple[str, str]:
    """从 figure 的第一个子图里读轴标签。读不到就返回空串。"""
    axes = getattr(fig, "axes", None)
    if not axes:
        return "", ""
    first = axes[0]
    try:
        return str(first.get_xlabel() or ""), str(first.get_ylabel() or "")
    except Exception:  # pragma: no cover —— 轴被模型改坏时不该让保存失败
        return "", ""
