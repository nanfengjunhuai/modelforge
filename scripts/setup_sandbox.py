"""创建沙箱的独立 Python 环境（ADR-005）。

用法（在项目根目录）：

    .venv/Scripts/python scripts/setup_sandbox.py
    .venv/Scripts/python scripts/setup_sandbox.py --mirror    # 国内网络用清华源

────────────────────────────────────────────────────────────────────────
为什么不能直接用后端的 .venv
────────────────────────────────────────────────────────────────────────
沙箱跑的是**模型生成的代码**，它不可信。而 `.venv` 里躺着 `DEEPSEEK_API_KEY`
（通过 .env 加载进环境变量），还有整个项目的源码。一段
`import os; print(os.environ["DEEPSEEK_API_KEY"])` 就能把密钥带走。

所以这里另起一个环境。它在物理上就是另一个目录，装的东西也不一样：
后端只有 FastAPI 那一套，沙箱只有 numpy / scipy / matplotlib。
`pyproject.toml` 里刻意没有后者 —— 那不是遗漏，是安全边界。

────────────────────────────────────────────────────────────────────────
为什么不能拿 `.venv/Scripts/python.exe` 去建这个环境
────────────────────────────────────────────────────────────────────────
从虚拟环境里创建虚拟环境，会继承一堆说不清的状态，得到的不是干净环境。
必须用**创建 `.venv` 时用的那个基础解释器**。

Python 把这个信息放在 `sys.base_prefix` 里 —— 它指向基础安装的根目录，
而不是当前 venv。这正是我们要的。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_DIR = PROJECT_ROOT / ".venv-sandbox"

# 数模三件套。用 >= 而不是钉死版本：这个环境只跑一次性脚本，
# 不需要 pyproject 那种可复现性级别的严格（那是后端的事）。
PACKAGES = ["numpy>=1.26", "scipy>=1.11", "matplotlib>=3.8"]

TSINGHUA_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 装完之后跑这个，验证环境真的能用。
# 不只是 import —— 还要真的算一下、真的出张图，把三个包都走一遍。
VERIFY_CODE = """
import numpy as np, scipy, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

x = np.linspace(0, 6.28, 50)
print("numpy", np.__version__, "求和的平方根 =", round(float(np.sum(x**2)) ** 0.5, 4))
print("scipy", scipy.__version__)
plt.plot(x, np.sin(x))
plt.savefig("_verify.png", dpi=60)
import pathlib
size = pathlib.Path("_verify.png").stat().st_size
print("matplotlib", matplotlib.__version__, "出图", size, "字节")
"""


def sandbox_python() -> Path:
    """沙箱环境里的解释器路径（按平台惯例不同）。"""
    return SANDBOX_DIR / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def base_python(override: str | None) -> Path:
    """定位「基础解释器」—— 也就是创建当前 venv 时用的那个。"""
    if override:
        return Path(override)

    exe = "python.exe" if sys.platform == "win32" else "bin/python3"
    candidate = Path(sys.base_prefix) / exe
    if not candidate.exists():
        raise SystemExit(
            f"找不到基础解释器：{candidate}\n"
            f"（当前解释器在 {sys.executable}，它的 base_prefix 是 {sys.base_prefix}）\n"
            "请用 --python 显式指定一个 Python 3.12 的路径。"
        )
    return candidate


def run(cmd: list[str], what: str) -> None:
    print(f"\n▶ {what}\n  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        raise SystemExit(f"\n✗ {what} 失败（退出码 {result.returncode}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="创建沙箱的独立 Python 环境")
    parser.add_argument("--python", help="用于创建环境的基础解释器路径")
    parser.add_argument("--mirror", action="store_true", help="用清华 PyPI 镜像（国内网络）")
    parser.add_argument("--force", action="store_true", help="已存在也重建")
    args = parser.parse_args()

    if SANDBOX_DIR.exists() and not args.force:
        print(f"沙箱环境已存在：{SANDBOX_DIR}")
        print("要重建请加 --force")
        return 0

    if SANDBOX_DIR.exists():
        import shutil

        print(f"删除旧环境：{SANDBOX_DIR}")
        shutil.rmtree(SANDBOX_DIR, ignore_errors=True)

    python = base_python(args.python)
    print(f"基础解释器：{python}")
    print(f"沙箱目录　：{SANDBOX_DIR}")

    run([str(python), "-m", "venv", str(SANDBOX_DIR)], "创建虚拟环境")

    py = str(sandbox_python())
    index_args = ["-i", TSINGHUA_MIRROR] if args.mirror else []

    run([py, "-m", "pip", "install", "--upgrade", "pip", *index_args], "升级 pip")
    run([py, "-m", "pip", "install", *index_args, *PACKAGES], "安装 numpy / scipy / matplotlib")

    # 验证：在沙箱里真跑一段代码，确认三件套都能用
    print("\n▶ 验证环境")
    verify = subprocess.run([py, "-c", VERIFY_CODE], cwd=str(SANDBOX_DIR))
    if verify.returncode != 0:
        raise SystemExit("\n✗ 验证失败 —— 环境装好了但用不了，往上翻看报错")

    print(
        f"\n✓ 沙箱就绪：{py}\n"
        "\n"
        "它和后端环境是**完全隔开**的：\n"
        "  · 沙箱里没有 DEEPSEEK_API_KEY 等任何密钥（环境变量白名单）\n"
        "  · 后端环境里没有 numpy/scipy/matplotlib（pyproject.toml 刻意没写）\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
