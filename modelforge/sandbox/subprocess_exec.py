"""子进程执行器 —— M3 的默认沙箱实现。

安全边界能挡什么、挡不住什么，写在 `base.py` 的模块注释里。**改这个文件之前
先读那一节**，别在没想清楚的情况下把某个限制放开。

════════════════════════════════════════════════════════════════════════
三道防线，以及每一道为什么是这么做的
════════════════════════════════════════════════════════════════════════

① 独立解释器（ADR-005）
   跑的是 `.venv-sandbox` 里的 python，不是后端自己的 `.venv`。
   所以模型代码里 `import numpy` 能用，`import modelforge` 会 ImportError。

② 环境变量白名单
   不是「删掉几个敏感变量」，而是**从一个空环境开始，只往里加必要的**。
   这个方向很重要：黑名单是漏的（今天是 DEEPSEEK_API_KEY，明天加了
   AWS_SECRET_ACCESS_KEY 就漏了），白名单不会漏。
   顺带把 HOME / USERPROFILE 也指向临时目录 —— 这样代码里的 `~`
   不会指向你真实的用户目录。

③ 输出走文件而不是管道
   这是个不太直觉的选择，值得解释：

     用管道（PIPE）的话，程序输出超过管道缓冲区（约 64KB）就会**阻塞**，
     等我们去读。而如果我们只在进程结束后才读，就会**死锁** ——
     它等我们读，我们等它退出。
     `communicate()` 解决了死锁，但它是把全部输出读进内存的：
     一段 `while True: print("x")` 在超时前的 10 秒里能塞满几个 GB 内存。

     重定向到文件同时解决了这两个问题：文件不会满，进程不会阻塞；
     而我们在读取时可以只取前 N 个字节，内存占用有上界。
     代价是磁盘上可能出现一个大文件 —— 但那个临时目录用完就删，
     而且磁盘打满比内存打满温和得多（前者报错，后者连累整个后端）。

    另外设了 PYTHONUNBUFFERED=1，否则 Python 会按块缓冲，短脚本的输出
   要等进程退出才落盘。现在这样将来才好做「执行过程中实时推流」。

④ 用线程跑阻塞式 subprocess，而不是 asyncio 的子进程 API
   这一条是**踩了坑之后才加的**，值得完整讲一遍，因为它是本项目里最典型的
   「单元测试全绿但真实运行爆炸」案例。

   初始实现用的是 `asyncio.create_subprocess_exec`。24 个离线测试全过，
   单独跑执行器也正常 —— 但一接到 uvicorn 上就报：

       NotImplementedError:            ← 注意，错误信息是空的

   空消息的 NotImplementedError 不好查。原因是这样的：

     Windows 上 asyncio 有两种事件循环。**SelectorEventLoop 根本不支持
     子进程**（`_make_subprocess_transport` 直接 raise NotImplementedError），
     ProactorEventLoop 才支持。

     · pytest 用的是默认策略 → Proactor → 测试通过
     · uvicorn 加了 `--reload` 时，因为要开子进程做热重载，
       会**刻意选择 SelectorEventLoop**（见 uvicorn/loops/asyncio.py 的
       `asyncio_loop_factory(use_subprocess=True)`）→ 爆炸

   而且这个选择发生在我们代码被 import **之前**
   （`Server.run()` 先建好循环，`Config.load()` 才 import 应用），
   所以应用层无论怎么设置事件循环策略都来不及。

   结论：**不要把功能正确性押在「服务器碰巧选了哪个事件循环」上。**
   改用 `asyncio.to_thread()` 包一个阻塞式的 `subprocess.Popen`：
   任何事件循环都能跑，而且顺带把超时逻辑从 async 版的别扭写法
   （`wait_for` 包裹 + 取消语义）换成了 Popen 原生的 `wait(timeout=)`。

   代价是每次执行占用一个线程。可以接受 —— Agent 循环里工具是**顺序**执行的，
   一个请求最多同时占一个；而且这些线程 99% 的时间在等子进程，不烧 CPU。

   顺带修好的一件事：现在请求被取消（用户关页面）时，我们能在
   CancelledError 里主动杀掉子进程，而不是傻等它跑完超时。

⑤ 工作目录必须在项目**之外**
   又一条踩坑之后的产物，而且这个坑比「④」更隐蔽 —— 因为它
   **时灵时不灵**，很容易被当成「模型偶尔抽风」。

   症状：模型时不时收到这样的报错，而且重试往往就好了：

       KeyboardInterrupt
       退出码 3221225786   （= 0xC000013A = STATUS_CONTROL_C_EXIT）

   起因是我们的沙箱实现细节和 uvicorn 的一个开发期特性撞上了：

     1. 沙箱每次执行都要写一个 `solution.py` 到工作目录
     2. 如果工作目录在项目里，uvicorn 的 `--reload` 就会**看见这个新文件**
        （它盯着整个项目目录）
     3. 于是热重载被触发 —— uvicorn 日志里明明白白写着
        「WatchFiles detected changes in 'sandbox_tmp\\run-xxx\\solution.py'. Reloading...」
     4. 重载要终止旧的服务器进程，而这个终止信号会顺着**控制台**传到
        同一控制台里的所有进程 —— 包括我们那个正在 `import numpy` 的沙箱子进程
     5. 子进程收到 Ctrl+C，Python 抛 KeyboardInterrupt

   为什么看起来像「第一次必挂、重试就好」？因为这是个竞态，而
   **第一次 import numpy 最慢**（冷启动约 170ms，之后约 65ms），
   窗口最宽，所以挨中的概率最高。一旦缓存热了，信号还没到进程就已经跑完了。

   修法很直接：把工作目录挪到系统临时目录（`config.py` 的默认值就是这样）。
   另外执行器在启动时会检查这个目录，发现它在项目里就打一条警告 ——
   把「随机崩」变成一个会自己开口解释的错误。

   ⚠️ 顺带一个给 M5 的提醒：将来要把生成的图表存成「产物」时，
   **不要**存到项目里的 `artifacts/`。那会重新引入这个问题。
   存系统目录 + 由接口提供下载，或者给 uvicorn 配 `--reload-exclude`。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from modelforge.config import Settings, get_settings
from modelforge.sandbox.base import ExecutionResult

logger = logging.getLogger(__name__)

__all__ = ["SubprocessExecutor", "find_sandbox_python"]

# 项目根目录。__file__ 是 <root>/modelforge/sandbox/subprocess_exec.py，
# 往上数三级。用 __file__ 而不是 os.getcwd()，是为了让结果不随「在哪个目录
# 敲命令」而变。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def find_sandbox_python(settings: Settings) -> Path:
    """定位沙箱解释器。

    优先用配置里显式指定的路径；否则按平台惯例在项目根目录下找
    `.venv-sandbox`。找不到不算错误 —— 由调用方在真正执行时
    变成一条**人话**的错误而不是崩溃（见 `SubprocessExecutor.run`）。
    """
    if settings.sandbox_python:
        return Path(settings.sandbox_python)

    # Windows 是 Scripts/python.exe，POSIX 是 bin/python
    relative = Path("Scripts/python.exe") if sys.platform == "win32" else Path("bin/python")
    return PROJECT_ROOT / ".venv-sandbox" / relative


def _resolve(path: Path) -> Path:
    """把相对路径按项目根目录展开。

    为什么不按当前工作目录（CWD）？因为 CWD 是不可靠的 —— 用户可能从任何
    地方敲启动命令。项目内部的路径一律锚定在项目根目录上。
    """
    return path if path.is_absolute() else PROJECT_ROOT / path


def _is_inside(child: Path, parent: Path) -> bool:
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


class SubprocessExecutor:
    """满足 `CodeExecutor` 协议的子进程执行器。"""

    name = "subprocess"

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.python = find_sandbox_python(self._settings)
        self.work_root = _resolve(self._settings.sandbox_work_dir)

        # 工作目录落进项目里是个**看起来无害、实际会间歇性炸**的配置。
        # 与其等着别人踩，不如在构造的时候就喊一声（原因见模块注释「⑤」）。
        if _is_inside(self.work_root, PROJECT_ROOT):
            logger.warning(
                "沙箱工作目录 %s 位于项目目录内。如果后端用 uvicorn --reload 启动，"
                "沙箱每次执行生成的 solution.py 都会触发一次热重载，"
                "重载信号可能打断正在执行的代码，表现为随机的 KeyboardInterrupt。"
                "建议改用项目外的目录（默认值就是系统临时目录）。",
                self.work_root,
            )

    # ────────────────────────────────────────────── 环境变量

    def _build_env(self, work_dir: Path) -> dict[str, str]:
        """从零搭一个最小环境。

        ⚠️ 这里**不是**在 os.environ 的副本上删东西，而是从空字典开始加。
        黑名单漏一个变量就前功尽弃（比如明天加了新的密钥变量），
        白名单则不会 —— 没写进去的天然就不存在。

        注意 DEEPSEEK_API_KEY 之类根本不在白名单里，所以模型代码里
        `os.environ.get("DEEPSEEK_API_KEY")` 会拿到 None。
        这正是 ADR-005 要防的那件事。
        """
        # 这些是操作系统层面的必需品，不给的话 python 自己都起不来。
        # （Windows 上缺 SYSTEMROOT 会直接报「无法初始化」之类的怪错。）
        passthrough = (
            ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH")
            if sys.platform == "win32"
            else ("PATH", "LANG", "LC_ALL")
        )
        env = {k: os.environ[k] for k in passthrough if k in os.environ}

        env.update(
            {
                # 强制 UTF-8：否则在 Windows 上子进程的 stdout 是 GBK，
                # 打印中文直接 UnicodeEncodeError，而模型看到这个报错会一脸茫然。
                "PYTHONIOENCODING": "utf-8",
                # 关掉缓冲，输出立刻落盘（见模块注释）
                "PYTHONUNBUFFERED": "1",
                # matplotlib 用非交互后端：默认后端会尝试开窗口，
                # 在无头环境里会卡住甚至崩掉。
                "MPLBACKEND": "Agg",
            }
        )

        # 把「家目录」也指到临时目录。这样代码里的 `~` / Path.home()
        # 落在沙箱里，而不是你的真实用户目录。
        env["HOME"] = str(work_dir)
        env["USERPROFILE"] = str(work_dir)
        env["TMPDIR"] = str(work_dir)
        env["TEMP"] = str(work_dir)
        env["TMP"] = str(work_dir)
        # matplotlib 要一个可写的配置目录，否则每次运行都会打印一条
        # 「Matplotlib is building the font cache」警告，污染 stderr。
        env["MPLCONFIGDIR"] = str(work_dir / ".mplconfig")

        return env

    # ────────────────────────────────────────────── 执行

    async def run(self, code: str, *, timeout: float | None = None) -> ExecutionResult:
        """在沙箱里跑一段代码。契约见 `CodeExecutor.run`。"""
        limit = timeout if timeout is not None else self._settings.sandbox_timeout

        if not self.python.exists():
            # 这是「我们这边没准备好」，不是「代码写错了」。
            # 必须区分开：前者要用户去跑安装脚本，后者要模型去改代码。
            return ExecutionResult(
                error=(
                    f"沙箱解释器不存在：{self.python}\n"
                    "先运行 .venv/Scripts/python scripts/setup_sandbox.py 创建它。"
                )
            )

        self.work_root.mkdir(parents=True, exist_ok=True)
        # 每次执行一个全新目录。不这么做的话，两次并发执行会互相看见
        # 甚至覆盖对方的文件。
        work_dir = Path(tempfile.mkdtemp(prefix="run-", dir=self.work_root))
        script = work_dir / "solution.py"
        # 用 UTF-8 写文件并显式声明编码：Windows 上 Python 源文件默认按
        # 系统编码解析，中文注释会直接 SyntaxError。
        script.write_text(f"# -*- coding: utf-8 -*-\n{code}\n", encoding="utf-8")

        stdout_path = work_dir / "_stdout.txt"
        stderr_path = work_dir / "_stderr.txt"

        # 进程句柄要先「登记」出来，取消时才有东西可杀。
        # 用一个 dict 当盒子而不是 nonlocal 变量，是因为跨线程写
        # nonlocal 可读性差，而这里读和写发生在不同线程。
        box: dict[str, subprocess.Popen[bytes]] = {}

        started = time.monotonic()
        try:
            result = await asyncio.to_thread(
                self._run_blocking, script, work_dir, stdout_path, stderr_path, limit, box
            )
        except asyncio.CancelledError:
            # 用户关掉页面 / 点了停止。Starlette 会取消我们这个协程
            # （见 api/chat.py 的说明），但**线程是取消不掉的** ——
            # 它会一直等到子进程结束。所以这里主动把它杀掉，
            # 否则那段代码会继续占着 CPU 跑满整个超时时间。
            proc = box.get("proc")
            if proc is not None:
                logger.info("请求已取消，终止沙箱进程 pid=%s", proc.pid)
                _kill_tree(proc)
            raise
        finally:
            # 至少把大文件清掉。整个目录留给系统临时目录清理也可以，
            # 但主动删更干净 —— M5 要做「产物面板」时会改成按需保留。
            _safe_rmtree(work_dir)

        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def _run_blocking(
        self,
        script: Path,
        work_dir: Path,
        stdout_path: Path,
        stderr_path: Path,
        limit: float,
        box: dict[str, subprocess.Popen[bytes]],
    ) -> ExecutionResult:
        """真正干活的部分，**同步**的，跑在工作线程里。

        为什么整段都是阻塞代码？因为这样它就和事件循环彻底解耦了 ——
        不管是 Selector、Proactor 还是 uvloop，对这里都没有影响。
        详见模块注释的「④」。
        """
        env = self._build_env(work_dir)

        # 输出重定向到文件而不是管道。原因见模块注释的「③」——
        # 管道会死锁，communicate() 会吃内存，文件同时避开这两个坑。
        try:
            with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
                proc = subprocess.Popen(
                    [str(self.python), str(script)],
                    cwd=str(work_dir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    # ⚠️ 这一行不是可选的，而且只在 POSIX 上真的起作用。
                    #
                    # `_kill_tree` 在 POSIX 上用的是 `os.killpg(os.getpgid(pid), ...)`
                    # —— 如果子进程跟我们在同一个进程组里，这个调用会打到**整个组**，
                    # 也就是顺手把 uvicorn 自己一起杀掉。让它自成会话就没这个问题。
                    #
                    # Windows 上这个参数被 CPython 当作 unused 直接忽略
                    # （那边走 `taskkill /T` 杀进程树），所以可以无条件传。
                    start_new_session=True,
                )
                box["proc"] = proc

                timed_out = False
                try:
                    proc.wait(timeout=limit)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_tree(proc)
        except OSError as exc:
            return ExecutionResult(error=f"启动沙箱进程失败：{exc}")

        # 文件必须在上面的 with 退出之后再读 —— 那时写入端才真的关闭、
        # 缓冲才真的落盘。
        stdout, stdout_cut = self._read_capped(stdout_path)
        stderr, stderr_cut = self._read_capped(stderr_path)

        if stdout_cut:
            stdout += f"\n…（输出过长，已截断，仅保留前 {self._settings.sandbox_max_output} 字符）"
        if stderr_cut:
            stderr += f"\n…（输出过长，已截断，仅保留前 {self._settings.sandbox_max_output} 字符）"
        if timed_out:
            stderr += f"\n…（执行超过 {limit:g} 秒，已被强制终止）"

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            timed_out=timed_out,
        )

    def _read_capped(self, path: Path) -> tuple[str, bool]:
        """读文件的前 N 个字符，返回 (文本, 是否被截断)。

        只读前 N 个**字节**再解码，而不是整个读进来再切 ——
        否则一个 GB 级的输出文件会先把内存吃掉，截断就白做了。
        多读一点（×4）是为了让一个多字节字符不至于被从中间劈开。
        """
        cap = self._settings.sandbox_max_output
        try:
            with open(path, "rb") as f:
                raw = f.read(cap * 4 + 64)
        except OSError:
            return "", False

        try:
            size = path.stat().st_size
        except OSError:
            size = len(raw)

        # errors="replace" 而不是让解码失败：被劈开的最后一个字符变成 �
        # 无所谓，它待在截断标记旁边，没人会当真。
        text = raw.decode("utf-8", errors="replace")
        truncated = len(text) > cap or size > len(raw)
        return text[:cap], truncated

def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """杀掉进程**及它的所有后代**。

    为什么不只杀直接子进程？因为模型写的代码完全可以
    `subprocess.Popen(...)` 再开一个 —— 那个孙子进程会活下来，
    继续占 CPU，并且在 Windows 上还攥着继承过去的文件句柄不放。

    Windows 没有进程组的概念，得借 `taskkill /T`（T = tree）；
    POSIX 上则有标准的进程组（前提是子进程自己开了新会话，
    见 `_run_blocking` 里 `start_new_session` 的说明）。
    """
    if proc.poll() is not None:  # 已经退出了
        return

    try:
        if sys.platform == "win32":
            # 注意这里用 subprocess.run 而不是 asyncio 版本 ——
            # 同一个理由：不依赖事件循环。
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError, subprocess.SubprocessError) as exc:
        # 进程可能在我们动手之前就自己退出了 —— 这不是错误。
        logger.debug("杀进程树时遇到问题（多半是它已经退出了）：%s", exc)

    # 无论杀没杀成，都等它被回收，免得留下僵尸进程。
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def _safe_rmtree(path: Path) -> None:
    """尽力删除临时目录，失败就算了。

    删不掉通常是因为 Windows 上还有句柄没释放（刚被杀掉的进程会有一小段
    延迟）。这种情况下让系统清理临时目录即可 —— 绝不能因为清理失败
    就让整个请求报错。
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        logger.debug("清理沙箱临时目录失败：%s", path, exc_info=True)
