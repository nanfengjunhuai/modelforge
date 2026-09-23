"""路径解析 —— 一个地方回答「东西该放哪」。

════════════════════════════════════════════════════════════════════════
这个模块存在的理由：项目里有两种路径，混淆它们的代价不对称
════════════════════════════════════════════════════════════════════════

  ① **项目自己带的东西** —— 数据库、配置、日志
     → 锚定 `PROJECT_ROOT`，**不锚当前工作目录**

     锚 CWD 的坑：在 `web/` 目录下启动一次后端，就会得到一个空数据库，
     然后开始怀疑「为什么我的会话全不见了」。这个坑 M4 已经在
     `sqlite_store.resolve_db_path` 的注释里记过一次。

  ② **运行时生成的东西** —— 沙箱工作目录、图表产物
     → 落在**系统目录**，绝不能进项目

     进项目的坑更隐蔽：uvicorn 的 `--reload` 盯着整个项目目录，新文件
     一出现就触发热重载，而重载发给服务器的终止信号会顺着控制台传到
     正在跑代码的沙箱子进程上 —— 表现为**随机的 `KeyboardInterrupt`**
     （退出码 3221225786）。M3 为此丢过一次排查时间，详见
     `sandbox/subprocess_exec.py` 的模块注释「⑤」。

两者的规则是反的，所以不能靠自觉，得有个地方把它们写死。

────────────────────────────────────────────────────────────────────────
为什么不用 platformdirs 之类的库
────────────────────────────────────────────────────────────────────────
`platformdirs` 是干这个的标准库，但这段逻辑一共二十行，而它是**唯一**
一个让这个项目依赖平台约定的地方。为了二十行引一个依赖（还要在
每台机器上装一份），不划算。等真有第三个平台要支持时再说。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "PROJECT_ROOT",
    "is_inside",
    "resolve_project_path",
    "user_data_dir",
]

# `<root>/modelforge/paths.py` 往上数两级。
#
# 用 `__file__` 而不是 `os.getcwd()`：结果不该随「在哪个目录敲命令」而变。
# 这正是本模块开头说的「规矩 ①」。
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_project_path(path: Path) -> Path:
    """把相对路径按项目根目录展开；绝对路径原样返回。

    所有「项目自己带的东西」的路径都要经过它。
    """
    return path if path.is_absolute() else PROJECT_ROOT / path


def is_inside(child: Path, parent: Path) -> bool:
    """child 是否在 parent 目录树里（含 parent 自身）。

    比字符串前缀比较可靠：要真正解析成绝对路径再比，
    否则 `C:/a/bc` 会被误判成在 `C:/a/b` 里面。
    """
    try:
        resolved_child = child.resolve()
        resolved_parent = parent.resolve()
    except OSError:  # pragma: no cover —— 路径无法解析时保守地当作不在里面
        return False
    return resolved_child == resolved_parent or resolved_parent in resolved_child.parents


def user_data_dir(app: str = "modelforge") -> Path:
    """本应用存放**运行时数据**的目录（跨平台）。

    返回的是绝对路径，这样它天然满足「规矩 ②」—— 永远不会被当成
    相对路径锚到项目上去。

         Windows   %LOCALAPPDATA%\\<app>          （一般是
                   C:\\Users\\<你>\\AppData\\Local\\<app>）
         macOS     ~/Library/Application Support/<app>
         Linux     $XDG_DATA_HOME/<app> 或 ~/.local/share/<app>

    为什么 Windows 用 LOCALAPPDATA 而不是 APPDATA？因为 Roaming 配置
    会被域环境同步到服务器，而图表产物可能有几十 MB —— 那不是配置，
    不该跟着漫游。

    注意这里**只算路径，不创建目录**。调用方在真正要写的时候再 mkdir，
    免得光是导入配置就在用户机器上凭空多出几层空目录。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / app
