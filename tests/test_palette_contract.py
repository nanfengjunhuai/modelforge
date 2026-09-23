"""配色契约 —— 三处色值必须完全一致。

════════════════════════════════════════════════════════════════════════
这个文件守的是 ADR-006 那条「唯一真相源」
════════════════════════════════════════════════════════════════════════
ADR-006 定下「图表与 UI 共用一套色彩体系」，真相源是
`web/src/app/globals.css`。但 M5 之后这份色值要在**三个地方**出现：

    ① web/src/app/globals.css          --chart-cat-1..8    ← 真相源
    ② modelforge/sandbox/runtime/matplotlibrc
                                       axes.prop_cycle     ← 论文插图用
    ③ modelforge/sandbox/runtime/modelforge_plot.py
                                       COLORS              ← 模型手动指定颜色用

②③ 在沙箱里跑，而沙箱是**另一个 Python 环境** —— 它没法 import 前端的
CSS，也没法在运行时读它（那样每次执行都要多一次文件读，而且一旦路径不对
就静默退回默认配色）。所以只能是三份拷贝。

**三份拷贝就是三个会漂移的地方。** 而这个漂移的症状极其温和：
颜色稍微偏了一点，图照样出、照样能看，没有任何东西报错。你会看到
工作台里的交互图和论文插图颜色对不上，然后怀疑是渲染库的问题。

所以这里用和 `test_event_contract.py` 一样的办法：**把三边都当纯文本读进来比对。**
那边管「Python 和 TypeScript 的事件字段名」，这边管「CSS 和沙箱的颜色」。

────────────────────────────────────────────────────────────────────────
为什么「没报错」从来不是判据
────────────────────────────────────────────────────────────────────────
M5 开发时 `matplotlibrc` 里写错了 **7 个配置项**，matplotlib 一个错都没报，
只是往 stderr 打了几行警告然后**静默失效** —— 配色悄悄退回了默认的 tab10，
网格颜色还是老样子，`subplot.hspace` 根本不存在。

发现它靠的不是看日志，是**把值读回来逐项断言**。
`scripts/smoke_artifact.py` 里那条「读回来对」的做法就是这条经验的产物；
这个文件则是它在 CI 里的那一半。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from modelforge.paths import PROJECT_ROOT

GLOBALS_CSS = PROJECT_ROOT / "web" / "src" / "app" / "globals.css"
RUNTIME_DIR = PROJECT_ROOT / "modelforge" / "sandbox" / "runtime"
MPLSTYLE = RUNTIME_DIR / "matplotlibrc"
PLOT_MODULE = RUNTIME_DIR / "modelforge_plot.py"

#: 分类色槽位数。设计系统的结论是「第 9 个系列不存在」——
#: 超出就归入「其他」或改用小倍数图。见 globals.css 里的注释。
SLOT_COUNT = 8

#: 亮色模式的分类色。**暗色模式那组不参与这个契约** ——
#: 论文是印在白纸上的，所以 matplotlib 只用亮色那组（matplotlibrc 里有说明）。
_CSS_VAR = re.compile(r"--chart-cat-(\d+):\s*(#[0-9a-fA-F]{6})")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def _read(path: Path) -> str:
    assert path.exists(), f"少了这个文件：{path}"
    return path.read_text(encoding="utf-8")


def css_var(name: str) -> str:
    """从 globals.css 里取一个颜色变量的**亮色**值。

    ⚠️ 关键是「取**第一次**出现」而不是「找某个块」。

    文件里同一个变量出现两次：`--chart-grid` 在亮色块里是 `#e1e0d9`，
    在暗色块里是 `#2c2c2a`。用 `re.findall` + `dict()` 的话会**留最后一个**
    —— 于是这条测试会拿暗色值去比对 matplotlibrc（那是给白纸印的），
    然后失败在一个看起来毫无道理的地方。

    （这个坑在写这个文件的时候真的踩了一次。）

    按「第一次出现」取天然就是亮色那组，而且不用写一个会随文件结构调整
    而失效的块解析器。前提是「亮色块在暗色块之前」，而那是这个文件从
    一开始就是的结构 —— 真有人调换了顺序，断言会**明确地**红
    （取出来的值变成暗色的），不会静默通过。
    """
    pattern = re.compile(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})")
    match = pattern.search(_read(GLOBALS_CSS))
    assert match, f"globals.css 里找不到 {name}"
    return match.group(1).lower()


def css_categorical() -> list[str]:
    """亮色模式的 8 个分类色。"""
    return [css_var(f"--chart-cat-{i}") for i in range(1, SLOT_COUNT + 1)]


def mplstyle_value(key: str) -> str:
    """从 matplotlibrc 里取一个生效配置项的值（去掉外层引号）。

    两个细节，都是踩过的：

      · **只取没有被注释掉的行。** 文件里为 `axes.prop_cycle` 写了大段说明，
        而那些说明里也带着同样的色值 —— 不过滤的话会把注释里的例子也算进来。
      · **去掉外层双引号。** 颜色值必须加引号（否则 `#` 会被 rc 解析器
        当成行内注释砍掉），所以读回来时要还原，否则比的是 `"#e1e0d9"`
        和 `#e1e0d9` —— 两个看起来像但不相等的字符串。
    """
    for line in _read(MPLSTYLE).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith(f"{key}:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if value[:1] == value[-1:] == '"':
            value = value[1:-1]
        return value
    pytest.fail(f"matplotlibrc 里找不到生效的 {key}")


def mplstyle_prop_cycle() -> list[str]:
    """从 matplotlibrc 的 `axes.prop_cycle` 里取出颜色。"""
    return [c.lower() for c in _HEX.findall(mplstyle_value("axes.prop_cycle"))]


def plot_module_colors() -> list[str]:
    """把 `modelforge_plot.py` 真的**导入**一次，读它的 `COLORS`。

    用 importlib 从文件路径加载，因为它在沙箱运行时目录里、不是包的一部分
    （它会被复制进沙箱工作目录，模型直接 `import modelforge_plot` 用）。

    为什么是导入而不是正则抠源码：导入会**真的执行一遍这个模块**，
    所以语法错误、拼错的常量名、误加的顶层 import 都会在这里暴露。
    正则只能证明「文本里有这串字符」，证明不了「这个模块能用」。

    （后端环境里 import 它是安全的：`matplotlib` 只出现在函数体内，
    模块顶层只有 `json` / `pathlib` / `typing` 三个标准库 import。）
    """
    spec = importlib.util.spec_from_file_location("_mf_plot_under_test", PLOT_MODULE)
    assert spec and spec.loader, f"加载不了 {PLOT_MODULE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [str(c).lower() for c in module.COLORS]


# ══════════════════════════════════════════════════════ 契约


def test_css_exposes_the_expected_slots():
    """先确认真相源本身是完整的 —— 否则下面几条可能只是在比「空 vs 空」。"""
    assert len(css_categorical()) == SLOT_COUNT


def test_the_sandbox_palette_matches_the_design_system():
    """**本文件的核心断言。**

    沙箱里那两份配色（rcParams 用的和模型手动指定颜色用的）必须和
    `globals.css` 逐字节一致。

    这里比的是**小写十六进制字符串**：`#2A78D6` 和 `#2a78d6` 在人眼里
    是同一个颜色，比字符串的话会误报。而比颜色是否「相等」需要解析
    （那就要在后端环境里引入颜色处理的逻辑），不值得 —— 规范大小写之后
    字符串相等就是颜色相等。
    """
    expected = css_categorical()

    assert mplstyle_prop_cycle() == expected, (
        "matplotlibrc 的 axes.prop_cycle 和 globals.css 的 --chart-cat-* 对不上。\n"
        "改色值时要三处一起改（见本文件开头的说明）。"
    )
    assert plot_module_colors() == expected, (
        "modelforge_plot.COLORS 和 globals.css 的 --chart-cat-* 对不上。"
    )


def test_the_palette_order_is_not_rearranged():
    """顺序**本身就是 CVD 安全机制**，不是随手排的。

    它由「枚举所有排列，取最小相邻色差最大的那一个」得来（见 globals.css
    和 matplotlibrc 里的注释）。重排之后色值一个没少、图照样出，
    但色盲用户看到的两条相邻曲线会变成同一个颜色 —— 而这在开发者的
    屏幕上完全看不出来。

    这条测试没法验证「当前顺序是最优的」（那要跑 validate_palette.js），
    它能做的是**在顺序被改动时把人叫醒**，让人去看那段注释。
    """
    expected = css_categorical()

    # 一个具体的、容易发生的改动：把蓝和青对调（它们看起来最像「同一族」）
    moved = expected.copy()
    moved[0], moved[1] = moved[1], moved[0]
    assert moved != expected, "前提不成立：前两个色值相同，这条测试没有意义"

    assert mplstyle_prop_cycle()[0] == expected[0]


def test_grid_color_matches_the_design_system():
    """网格线颜色也必须同源。

    它比分类色更容易被忽略，因为「网格线稍微深一点」几乎没人会注意到 ——
    直到把工作台截图和论文插图并排放。

    这条顺带守住了 matplotlibrc 里那个**外层双引号**：少了它，`#` 会被
    rc 解析器当成行内注释砍掉，整行变成空值然后静默失效
    （matplotlib 只往 stderr 打一行警告，程序照跑）。
    """
    assert mplstyle_value("grid.color") == css_var("--chart-grid")


def test_the_palette_is_never_cycled_beyond_eight():
    """第 9 个系列不存在。

    这条测的是「没有人往这三份列表里偷偷加了第 9 个颜色」。
    真要画 9 个系列，正确的做法是归入「其他」或改用小倍数图 ——
    那是个设计决定，不该在某次顺手改配置时被绕过。
    """
    for name, colors in (
        ("globals.css", css_categorical()),
        ("matplotlibrc", mplstyle_prop_cycle()),
        ("modelforge_plot.COLORS", plot_module_colors()),
    ):
        assert len(colors) == SLOT_COUNT, f"{name} 里的分类色不是 {SLOT_COUNT} 个"
