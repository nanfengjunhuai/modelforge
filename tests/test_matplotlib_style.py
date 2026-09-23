"""科研风格 —— 在**真的沙箱**里跑一遍，把配置读回来逐项断言。

════════════════════════════════════════════════════════════════════════
为什么这个文件必须「真跑」，别的测试都不行
════════════════════════════════════════════════════════════════════════
`matplotlibrc` 里写错的配置项，matplotlib **不报错**。

M5 开发时那份配置文件里错了 **7 项** —— 其中包括一个根本不存在的 key
（`subplot.hspace`，正确的是 `figure.subplot.hspace`）和一段被行内注释
规则吃掉的配色（`cycler(color=['#2a78d6', ...])` 里的 `#` 被当成注释开头，
整个值被截断）。后果是：

  · 配色**静默**退回 matplotlib 默认的 tab10，图照样出，颜色全错
  · 网格颜色还是默认灰
  · `hspace` 那一项压根没生效

一个类型检查器抓不到其中任何一条 —— 这不是类型问题，是
**「配置写错了但程序照跑」**。唯一能发现它的办法是**把值读回来对**。

这个文件就是那条经验的固化。它比 `scripts/smoke_artifact.py` 更靠近 CI：
那个脚本是给人手动跑的，这个是每次 `pytest` 都跑。

⚠️ 它需要 `.venv-sandbox`（装了 numpy/matplotlib）。没有就跳过 ——
   贡献者 clone 下来只跑 `pip install -e ".[dev]"` 时不该因此变红。
   这一点和 `test_sandbox.py` 用的是 `sys.executable` 是同一个考虑：
   **别让「没装那 300MB 三件套」变成一个失败的测试。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modelforge.config import Settings
from modelforge.sandbox.subprocess_exec import SubprocessExecutor, find_sandbox_python

# 这一整份文件都要真的起沙箱进程，比别的测试慢。
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# 在沙箱里跑的检查脚本。**它自己把值读回来断言**，而不是打印出来让
# 外面去解析 —— 报错信息会带着「期望 vs 实际」一起回到 stderr，
# 失败时不需要再猜是哪一项不对。
_PROBE = """
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

problems = []

def check(key, want):
    got = matplotlib.rcParams[key]
    if got != want:
        problems.append(f"{key}: 期望 {want!r}，实际 {got!r}")

check("axes.unicode_minus", False)     # 不设的话负号变豆腐块
check("axes.grid", True)
check("axes.spines.top", False)
check("axes.spines.right", False)
check("pdf.fonttype", 42)              # 投稿系统拒收 Type 3
check("ps.fonttype", 42)
check("savefig.dpi", 300.0)
check("legend.frameon", False)
check("figure.subplot.left", 0.11)     # 曾经的错误写法：subplot.left

grid = matplotlib.rcParams["grid.color"]
if str(grid).lower() not in ("#e1e0d9", "e1e0d9"):
    problems.append(f"grid.color: 期望 #e1e0d9，实际 {grid!r}")

cycle = [c["color"] for c in matplotlib.rcParams["axes.prop_cycle"]]
want_cycle = ["#2a78d6", "#1baf7a", "#eda100", "#008300",
              "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
if [str(c).lower() for c in cycle] != want_cycle:
    problems.append(f"axes.prop_cycle: 实际 {cycle}")

if problems:
    print("\\n".join(problems))
    raise SystemExit(1)

# 真的画一张带中文的图。缺字形时 matplotlib 会打
# "Glyph ... missing from font" 警告 —— 抓成异常，这是唯一能证明
# 「中文真的显示出来了」的办法。
import warnings
import numpy as np

fig, ax = plt.subplots()
x = np.linspace(0, 10, 40)
ax.plot(x, np.sin(x), label="预测值")
ax.plot(x, np.cos(x), label="实测值")
ax.set_xlabel("时间 t / s")
ax.set_ylabel("归一化响应")
ax.legend()

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    fig.savefig("probe.png")

missing = [str(w.message) for w in caught if "missing from font" in str(w.message)]
if missing:
    print("缺字形：" + "; ".join(missing[:3]))
    raise SystemExit(1)

print("PROBE-OK")
"""


def sandbox_executor(tmp_path: Path, **overrides: object) -> SubprocessExecutor:
    python = find_sandbox_python(Settings())
    if not python.exists():
        pytest.skip(f"没有 .venv-sandbox，跳过（先跑 scripts/setup_sandbox.py）：{python}")

    settings = Settings(
        sandbox_work_dir=tmp_path / "work",
        sandbox_mpl_config_dir=tmp_path / "mplconfig",
        artifacts_dir=tmp_path / "artifacts",
        **overrides,  # type: ignore[arg-type]
    )
    return SubprocessExecutor(settings=settings)


def read_stored(tmp_path: Path, scope: str, name: str) -> bytes:
    """按**显示名**读回一个已经搬走的产物，返回它的内容。

    刻意直接看文件系统：这个文件里的测试要验的是「东西真的落盘了」，
    而「落盘了没有」只有看盘才算数。所以这里不用 store 的 API
    （那等于用一个可能自己也有 bug 的东西去验证自己），
    而是把 uuid 文件名和显示名对一遍再读。

    ⚠️ 比对的是**最后一个**后缀，不是全部。落盘时 `_safe_suffix` 只取
    `Path(name).suffix`，所以 `权重.chart.json` 在盘上叫 `<uuid>.json`。
    「图表数据」和普通 JSON 的区别靠 `ArtifactRef.name` 表达，不是靠后缀 ——
    后缀在落盘那一刻就被简化了，因为模型起的名字完全不可信
    （它可能是 `.tar.gz`、可能是空、也可能带一堆怪字符）。
    """
    directory = tmp_path / "artifacts" / scope
    suffix = Path(name).suffix
    for path in directory.iterdir():
        if path.name.endswith(suffix):
            return path.read_bytes()
    raise AssertionError(f"在 {directory} 里找不到 {name}（后缀 {suffix}）")


# ══════════════════════════════════════════════════════ 风格


async def test_the_scientific_style_actually_takes_effect(tmp_path: Path):
    """**本文件最重要的一条。**

    沙箱里任何 matplotlib 代码都默认套上科研风格 —— 不需要模型调用任何东西。
    这是 M5 图表质量的主要保证，也是「风格靠环境不靠模型自觉」那条设计的验收。

    这条测试的价值不在于它会红，而在于它**把「配置有没有被吃掉」变成了
    一个二值的事实**。看 stderr 是发现不了这件事的：matplotlib 的警告
    混在一堆别的输出里，而且没人会去看「一切正常」的运行日志。
    """
    executor = sandbox_executor(tmp_path)

    result = await executor.run(_PROBE)

    assert result.exit_code == 0, f"沙箱里的自检失败：\n{result.stdout}\n{result.stderr}"
    assert "PROBE-OK" in result.stdout


async def test_the_mpl_config_dir_is_persistent_not_per_run(tmp_path: Path):
    """**回归测试：`MPLCONFIGDIR` 不能指向每次执行都删的工作目录。**

    M3 把它设成了 `<工作目录>/.mplconfig`，而工作目录用完即删 ——
    于是每次执行都要重建一遍字体缓存（实测 0.42 秒，见
    `SubprocessExecutor._prepare_mpl_config` 的说明）。

    这条断言的是**缓存文件留下来了**：跑一次之后，配置目录里应该有一个
    `fontlist-*.json`。如果 `MPLCONFIGDIR` 又被指回了工作目录，
    这个文件会跟着工作目录一起消失，测试就红。

    这是个「症状很温和」的问题 —— 图照出、结果照对，只是每张图慢半秒。
    没有测试盯着的话，它会在某次重构里被悄悄改回去（毕竟「配置目录放在
    工作目录里」看起来更整洁）。
    """
    executor = sandbox_executor(tmp_path)

    result = await executor.run("import matplotlib.pyplot as plt\nprint('ok')")
    assert result.exit_code == 0, result.stderr

    caches = list(executor.mpl_config_dir.glob("fontlist-*.json"))
    assert caches, (
        f"{executor.mpl_config_dir} 里没有字体缓存 —— "
        "MPLCONFIGDIR 大概又指回工作目录了，那会让每次执行都重建一次缓存"
    )
    assert not (executor.work_root / ".mplconfig").exists()


async def test_the_matplotlibrc_is_refreshed_from_the_repo(tmp_path: Path):
    """样式表每次执行都从仓库同步一份。

    这样它**永远和仓库版本一致**。装进 `.venv-sandbox` 的话，改了样式
    不重跑安装脚本就还是旧的，而且不会有任何提示 —— 那正是这个项目里
    反复出现的那类问题（版本漂移 + 静默）。
    """
    executor = sandbox_executor(tmp_path)

    # 先把配置目录里的文件改脏，模拟「有人手改过」或者「版本旧了」
    deployed = executor.mpl_config_dir / "matplotlibrc"
    deployed.write_text("# 被改脏了\n", encoding="utf-8")

    executor2 = sandbox_executor(tmp_path)  # 重新构造 = 重新同步

    assert "axes.prop_cycle" in (executor2.mpl_config_dir / "matplotlibrc").read_text(
        encoding="utf-8"
    )


# ══════════════════════════════════════════════════════ 产物


async def test_a_figure_becomes_a_downloadable_artifact(tmp_path: Path):
    """**M5 的端到端验收，在不需要模型的情况下。**

    一段真实形状的代码：画图 → `mp.save()` → 产物被收走 →
    PNG / PDF / chart.json 三样都在，且 chart.json 里是**真实的数组**。

    这条跑的是整条链路（注入运行时 → 沙箱执行 → 扫描 → 搬运），
    除了 DeepSeek 那一环。要验证「模型会不会照着工具描述用」得靠手动
    端到端，但「用了之后能不能成」在这里就该是确定的。
    """
    executor = sandbox_executor(tmp_path)

    code = """
import numpy as np
import matplotlib.pyplot as plt
import modelforge_plot as mp

methods = ["AHP", "熵权法", "CRITIC"]
weights = np.array([[0.42, 0.31, 0.27], [0.38, 0.34, 0.28]])

fig, ax = plt.subplots()
x = np.arange(3)
for i, name in enumerate(methods[:2]):
    ax.plot(x, weights[i], label=name)
ax.set_xticks(x); ax.set_xticklabels(["指标1", "指标2", "指标3"])
ax.set_xlabel("评价指标"); ax.set_ylabel("权重")
ax.legend()
mp.style_lines(ax)
mp.save(fig, "权重对比", chart={
    "kind": "bar",
    "series": [{"name": n, "data": weights[i]} for i, n in enumerate(methods[:2])],
    "categories": ["指标1", "指标2", "指标3"],
})
"""
    scope = "s" * 32
    result = await executor.run(code, scope=scope)

    assert result.exit_code == 0, result.stderr
    assert sorted(a.name for a in result.artifacts) == [
        "权重对比.chart.json",
        "权重对比.pdf",
        "权重对比.png",
    ]

    png = next(a for a in result.artifacts if a.name.endswith(".png"))
    assert (png.kind, png.mime) == ("image", "image/png")
    pdf = next(a for a in result.artifacts if a.name.endswith(".pdf"))
    assert (pdf.kind, pdf.mime) == ("document", "application/pdf")

    # 看图头，而不是只看大小 —— 「生成了一个几 KB 的文件」什么都说明不了，
    # 它完全可能是一个出错页面或者一个空画布。
    assert read_stored(tmp_path, scope, "权重对比.png").startswith(b"\x89PNG\r\n\x1a\n")
    assert read_stored(tmp_path, scope, "权重对比.pdf").startswith(b"%PDF-")


async def test_chart_json_carries_the_real_arrays(tmp_path: Path):
    """**「图表建立在数据基础之上」的机械验收。**

    模型把 numpy 数组原样交给 `mp.save(chart=...)`，存下来的必须是
    同一批数字（而不是它凭印象重打的）。轴标签也从图里读，不让模型
    再传一遍 —— 能让两处对不上的机会越少越好。

    这条顺便守着 `_as_sequence` 那个「不能只认 list」的教训：
    `isinstance(np.array([1,2]), list)` 是 **False**，只认 list 的话
    最正常的用法反而会被判成格式不对。
    """
    executor = sandbox_executor(tmp_path)
    scope = "s" * 32

    code = """
import numpy as np, matplotlib.pyplot as plt
import modelforge_plot as mp

w = np.array([0.415, 0.311, 0.274])
fig, ax = plt.subplots()
ax.bar(["甲", "乙", "丙"], w, label="熵权法")
ax.set_xlabel("指标"); ax.set_ylabel("权重")
mp.save(fig, "权重", chart={"kind": "bar", "series": [{"name": "熵权法", "data": w}]})
"""
    result = await executor.run(code, scope=scope)
    assert result.exit_code == 0, result.stderr

    spec = json.loads(read_stored(tmp_path, scope, "权重.chart.json").decode("utf-8"))

    assert spec["kind"] == "bar"
    assert spec["title"] == "权重"
    # 从**图里**读出来的轴标签，不是模型传的
    assert spec["x_label"] == "指标"
    assert spec["y_label"] == "权重"
    series = spec["series"][0]
    assert series["name"] == "熵权法"
    # 存下来的必须是**模型手里那批数字**，不是它凭印象重打的
    assert series["data"] == pytest.approx([0.415, 0.311, 0.274], abs=1e-9)
    # 颜色按序号自动分配，和静态图用的是同一套调色板
    assert series["color"] == "#2a78d6"


async def test_a_broken_chart_spec_does_not_lose_the_figure(tmp_path: Path):
    """图是主产物，数据是附带的 —— 不能为了一份附带的东西把主产物丢掉。

    模型完全可能把 `chart` 写歪（忘了 `data`、把类别标签塞进了 data）。
    那时候正确的行为是：**图照存**，同时打一条说清楚问题的警告，
    让模型下次能改对。

    抛出异常的话，模型那段脚本会当场死掉，**后面所有的图都不画了** ——
    为了一份附带的数据，把主产物连同后续全部工作一起丢掉。
    """
    executor = sandbox_executor(tmp_path)

    code = """
import matplotlib.pyplot as plt
import modelforge_plot as mp

fig, ax = plt.subplots()
ax.plot([1, 2, 3])
# 忘了 data —— 这是最容易犯的一种错
mp.save(fig, "半成品", chart={"kind": "line", "series": [{"name": "甲"}]})
print("脚本继续跑到了这里")
"""
    result = await executor.run(code, scope="s" * 32)

    assert result.exit_code == 0, result.stderr
    # 脚本没有被打断
    assert "脚本继续跑到了这里" in result.stdout
    # 警告说清楚了是哪一项不对
    assert "chart 数据没能保存" in result.stdout
    # 图还在
    assert sorted(a.name for a in result.artifacts) == ["半成品.pdf", "半成品.png"]


async def test_artifacts_are_not_collected_without_a_scope(tmp_path: Path):
    """**无状态端点的行为必须和 M3 完全一致。**

    `scope=None` 表示「这次执行没有地方放产物」。与其给它们找一个临时归宿
    然后再想怎么清理，不如照旧跑完即删 —— 少一条需要理解的分支。

    这条测试守的是「没有偷偷给无状态路径也留下文件」。
    """
    executor = sandbox_executor(tmp_path)

    code = """
import matplotlib.pyplot as plt
import modelforge_plot as mp
fig, ax = plt.subplots()
ax.plot([1, 2, 3])
mp.save(fig, "不该被收走")
"""
    result = await executor.run(code)  # ← 没有 scope

    assert result.exit_code == 0, result.stderr
    assert result.artifacts == []
    assert not any((tmp_path / "artifacts").rglob("*.png"))
