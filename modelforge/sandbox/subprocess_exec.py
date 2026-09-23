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

from modelforge.artifacts.base import ArtifactRef, ArtifactStore, SandboxArtifact, guess_mime
from modelforge.artifacts.local_store import LocalArtifactStore
from modelforge.config import Settings, get_settings
from modelforge.paths import PROJECT_ROOT, is_inside, resolve_project_path
from modelforge.sandbox.base import ExecutionResult

logger = logging.getLogger(__name__)

__all__ = ["SubprocessExecutor", "find_sandbox_python"]

# 沙箱运行时目录 —— 每次执行前会被复制进工作目录。
#
# 里面现在有两个文件，服务两个不同的目的：
#   matplotlibrc        科研风格配置（复制进 MPLCONFIGDIR，不是工作目录）
#   modelforge_plot.py  给模型的绘图辅助模块（复制进工作目录，可 import）
_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"

# 产物目录的约定名。模型把文件写进工作目录下的这个子目录，就会被收走。
#
# 为什么用「约定目录 + 扫描」而不是「让模型显式声明产出了什么」：
# 后者要求模型每次都记得声明，而**它一定会忘**，忘了还不会报错，
# 只是图凭空消失。约定目录下**它是写文件**，而写文件是它本来就在做的事，
# 忘了的可能性小得多。这和「风格靠环境不靠模型自觉」是同一条原则。
_ARTIFACT_DIRNAME = "artifacts"

# 单次执行最多收走多少个产物。超出的部分**会被明确告知**（见 `_scan_artifacts`），
# 不静默丢弃 —— 项目里所有「截断」都遵守这条，见 `_read_capped` 的措辞。
_MAX_ARTIFACTS = 20

# 单次执行的产物总大小上限。300 dpi 的数模插图大约 100~500 KB，
# 50 MB 意味着「正常用法碰不到，但一段写疯了的代码撑不爆磁盘」。
_MAX_TOTAL_BYTES = 50 * 1024 * 1024


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


class SubprocessExecutor:
    """满足 `CodeExecutor` 协议的子进程执行器。"""

    name = "subprocess"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.python = find_sandbox_python(self._settings)
        self.work_root = resolve_project_path(self._settings.sandbox_work_dir)
        # 产物存哪由注入的存储决定；不传就自己造一个本地实现。
        #
        # 这就是 `CodeExecutor` / `ChatProvider` / `TurnRecorder` 那一套：
        # 沙箱只认 `ArtifactStore` 这个协议，将来换成对象存储或远端服务
        # 不需要改这里一行。
        self._artifacts: ArtifactStore = artifacts or LocalArtifactStore(settings=self._settings)

        # 工作目录落进项目里是个**看起来无害、实际会间歇性炸**的配置。
        # 与其等着别人踩，不如在构造的时候就喊一声（原因见模块注释「⑤」）。
        if is_inside(self.work_root, PROJECT_ROOT):
            logger.warning(
                "沙箱工作目录 %s 位于项目目录内。如果后端用 uvicorn --reload 启动，"
                "沙箱每次执行生成的 solution.py 都会触发一次热重载，"
                "重载信号可能打断正在执行的代码，表现为随机的 KeyboardInterrupt。"
                "建议改用项目外的目录（默认值就是系统临时目录）。",
                self.work_root,
            )

        self.mpl_config_dir = self._prepare_mpl_config()

    def _prepare_mpl_config(self) -> Path:
        """准备 matplotlib 的配置目录：确保存在，并把我们的 matplotlibrc 放进去。

        ⚠️ 这个目录**必须是持久的**，不能是每次执行都删的工作目录。

        M3 把 `MPLCONFIGDIR` 指向了 `<工作目录>/.mplconfig`，而工作目录用完即删，
        于是**每次执行**都要重建一次字体缓存。

        实测（本机 273 个字体，matplotlib 3.11.2）：

            冷启动（无缓存）  0.870 s
            热启动（有缓存）  0.453 s
            ─────────────────────────
            白交的部分        0.42 s   ← 每次执行

        说清楚它的量级：0.42 秒**不致命**。每次执行是独立进程，这个代价
        不累积；`sandbox_timeout` 默认 10 秒，它占 4%。所以这不是一个
        「会炸」的问题，而是一个「每张图都白交 0.42 秒」的问题 ——
        一场画十几张图的对话要多等好几秒，而且没有任何理由。

        之所以值得在注释里写这么长，是因为它**没有任何症状**：
        图照出、结果照对，只是慢一点点。这种问题不会有人来报 bug，
        只会让人觉得「这软件有点钝」。修它只要一行配置。

        （字体缓存本身仍然是有用的 —— 我们只是让它建一次就留下，
        而不是每次重新建。）

        「每次执行都重新复制一遍 matplotlibrc」是刻意的：这样样式表永远和
        仓库版本一致。装在 `.venv-sandbox` 里的话，改了样式不重跑安装脚本
        就还是旧的，而且不会有任何提示。
        """
        target = resolve_project_path(self._settings.sandbox_mpl_config_dir)
        source = _RUNTIME_DIR / "matplotlibrc"

        if is_inside(target, PROJECT_ROOT):
            logger.warning(
                "matplotlib 配置目录 %s 位于项目目录内。它的字体缓存文件会被"
                "不定期重写，同样可能触发 uvicorn --reload 的热重载。"
                "建议改用项目外的目录（默认值就是系统应用数据目录）。",
                target,
            )

        try:
            target.mkdir(parents=True, exist_ok=True)
            destination = target / "matplotlibrc"
            # 只在内容真的不一样时才写。目录本身可能是共享的，而这里
            # 每次请求都会被构造一次 —— 无脑覆盖既是多余的写入，
            # 也会白白刷新文件的修改时间。
            if not destination.exists() or destination.read_bytes() != source.read_bytes():
                shutil.copy(source, destination)
                logger.debug("已同步 matplotlibrc 到 %s", destination)
        except OSError as exc:
            # 不抛异常：配置目录建不出来不该让整个服务起不来。
            # 后果只是图失去科研风格（退回 matplotlib 默认），
            # 而不是什么都跑不了 —— 用一个警告换一个降级，比值。
            logger.warning("准备 matplotlib 配置目录 %s 失败：%s", target, exc)
        return target

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

        # matplotlib 的配置目录 —— **持久目录，不是工作目录**。
        #
        # ⚠️ 这一行 M5 改过。M3 时它是 `work_dir / ".mplconfig"`，
        #    而工作目录用完即删，于是每次执行都重建一遍字体缓存（实测 0.42 秒）。
        #    完整的原因写在 `_prepare_mpl_config` 的 docstring 里。
        #
        # 它同时承担第二个职责：里面放着我们的 `matplotlibrc`。
        # matplotlib 查找配置时 `$MPLCONFIGDIR/matplotlibrc` 排第一位，
        # 所以**沙箱里任何 matplotlib 代码都会自动套上科研风格**，
        # 不需要模型配合 —— 这是 M5 图表质量的主要保证。
        env["MPLCONFIGDIR"] = str(self.mpl_config_dir)

        return env

    # ────────────────────────────────────────────── 执行

    async def run(
        self, code: str, *, timeout: float | None = None, scope: str | None = None
    ) -> ExecutionResult:
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

        # 把给模型用的绘图辅助模块放进工作目录。
        #
        # 为什么是「每次拷一份」而不是「装进 .venv-sandbox」：
        # 装了之后它就和仓库版本脱钩了 —— 改了辅助模块，不重跑安装脚本
        # 就还是旧的，而且**不会有任何提示**。每次拷一个十几 KB 的文件
        # 可以忽略，换来的是「运行时永远和仓库版本一致」这条不需要维护的不变式。
        #
        # `python solution.py` 会把脚本所在目录放进 `sys.path`，
        # 所以模型直接 `import modelforge_plot` 就能用。
        _stage_runtime(work_dir)

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
            # ★ 产物必须在工作目录被删掉之前搬走，所以它在这个 try 里面，
            #   而不是 `finally` 之后 —— `finally` 会在本行之前执行，
            #   那时候文件早没了。
            result.artifacts = await self._collect_artifacts(work_dir, scope, result)
        except asyncio.CancelledError:
            # 用户关掉页面 / 点了停止。Starlette 会取消我们这个协程
            # （见 api/chat.py 的说明），但**线程是取消不掉的** ——
            # 它会一直等到子进程结束。所以这里主动把它杀掉，
            # 否则那段代码会继续占着 CPU 跑满整个超时时间。
            proc = box.get("proc")
            if proc is not None:
                logger.info("请求已取消，终止沙箱进程 pid=%s", proc.pid)
                _kill_tree(proc)
            # ⚠️ 取消路径**故意不收集产物**，直接连同工作目录一起丢掉。
            #
            # 技术上可以在 CancelledError 里 `await` 一次收集，但那个 await
            # 会立刻再次抛出 CancelledError（任务已经被取消了），得用
            # `asyncio.shield` 包起来才行 —— M4 在释放租约那里踩过同一个坑。
            #
            # 值不值得多这一层？不值得。用户主动点了停止，意味着他不想等这一轮；
            # 而已经跑完的那几轮产物早就各自收走了，这里丢的最多是**当前这一轮**
            # 的画到一半的图。这和 ADR-009 已经接受的「用户中途刷新时，
            # 被中断的那一轮已流出的文字会消失」是同一件事，口径一致。
            raise
        finally:
            # 产物已经被搬走了（或者这次执行本来就没有产物），
            # 剩下的都是中间文件：solution.py、stdout/stderr 的原始文件、
            # 模型自己写坏的那些。整个删掉。
            #
            # M3 时的注释写着「M5 要做产物面板时会改成按需保留」——
            # 实际做法比那个设想更简单：不是「按需保留工作目录」，
            # 而是「把要留的东西搬出去，然后照常删」。
            # 这样清理逻辑完全没变，多出来的只有一次搬运。
            _safe_rmtree(work_dir)

        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    # ────────────────────────────────────────────── 产物

    async def _collect_artifacts(
        self, work_dir: Path, scope: str | None, result: ExecutionResult
    ) -> list[ArtifactRef]:
        """把工作目录 `artifacts/` 下的文件搬进产物存储。

        `scope` 是 None 时直接返回空 —— 那表示调用方（无状态端点）没有地方
        保存产物。**这是刻意的**：与其给它们找一个临时归宿然后再想怎么清理，
        不如让行为与 M3 完全一致（跑完即删），少一条需要理解的分支。

        这个函数**永远不抛异常**。产物搬运失败不该让整次执行变成失败 ——
        代码跑成功了、stdout 拿到了，这才是主要结果；图丢了是次要的损失。
        """
        if scope is None:
            return []

        try:
            found, notes = await asyncio.to_thread(_scan_artifacts, work_dir)
            # 截断/丢弃的说明写进 stderr，会经由 `format_for_model` 原样
            # 呈现给模型。这和同文件里「输出过长已截断」「执行超过 N 秒」
            # 那两条提示是同一个做法：**有取舍就要说出来，不能静默截断。**
            for note in notes:
                result.stderr += f"\n…（{note}）"
            if not found:
                return []
            return await self._artifacts.ingest(scope, found)
        except Exception:
            logger.warning("收集产物时发生未预期的异常", exc_info=True)
            return []

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

def _stage_runtime(work_dir: Path) -> None:
    """把沙箱运行时里**可 import 的**文件复制进工作目录。

    目前只有一个 `modelforge_plot.py`。`matplotlibrc` 在同一个目录里，
    但它**不往工作目录拷** —— 它走 `MPLCONFIGDIR`（见 `_build_env`）。
    两边都放就成了两个真相源，而 matplotlib 查配置的顺序里 CWD 排在
    `$MPLCONFIGDIR` 之后，出问题时你会去改一个没生效的文件。
    """
    for name in ("modelforge_plot.py",):
        try:
            shutil.copy(_RUNTIME_DIR / name, work_dir / name)
        except OSError as exc:
            # 拷不进去的后果是模型 `import modelforge_plot` 报 ImportError，
            # 那条报错会原样回到模型手上，它能自己改成不用这个模块的写法。
            # 所以这里只记一笔，不让整次执行失败。
            logger.warning("把 %s 放进沙箱工作目录失败：%s", name, exc)


def _scan_artifacts(work_dir: Path) -> tuple[list[SandboxArtifact], list[str]]:
    """找出工作目录 `artifacts/` 下模型产出的文件。

    返回 `(找到的文件, 给人看的取舍说明)`。第二条是**必须**的 ——
    这个函数会截断，而项目里所有截断都要说出来（对比 `_read_capped`
    的措辞）。静默丢弃会让用户看到「方案里说有三张图，界面上只有两张」
    而完全不知道为什么。

    这里的顺序是**文件名排序**，所以「留下哪 20 个」是可复现的，
    不取决于文件系统返回目录项的顺序。不是因为字母序更合理，
    而是因为**确定的坏行为比随机的好行为好排查**。
    """
    root = work_dir / _ARTIFACT_DIRNAME
    if not root.is_dir():
        # 绝大多数执行都不会产出文件（只是算个数）。这条路径要快。
        return [], []

    found: list[SandboxArtifact] = []
    total_bytes = 0
    over_count = 0
    over_size = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        relative = path.relative_to(root)
        # 跳过 Python 自己造的东西和隐藏文件：`__pycache__`、编辑器备份、
        # matplotlib 可能临时落下的 `.#xxx`。它们不是用户要的产物。
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue

        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0:
            # 空文件几乎总是「代码跑到一半崩了」留下的，不是有意的产物。
            continue

        if len(found) >= _MAX_ARTIFACTS:
            over_count += 1
            continue
        if total_bytes + size > _MAX_TOTAL_BYTES:
            over_size += 1
            continue

        total_bytes += size
        found.append(
            SandboxArtifact(
                # 用相对路径而不是纯文件名：万一模型建了子目录
                # （`artifacts/图/权重.png`），把路径显示出来比
                # 让两张同名文件在界面上长得一模一样要好。
                # 统一成正斜杠，免得同样的东西在 Windows 和 Linux 上
                # 显示成两种样子。
                #
                # 顺手把控制字符去掉。POSIX 上文件名可以含 `\n`/`\r`，
                # 而这个名字最终会出现在 HTTP 响应头（下载时的
                # `Content-Disposition`）里 —— 头注入是真实存在的一类漏洞。
                # 这里清掉，是在这个名字**第一次变成结构化数据**的地方清，
                # 而不是指望后面每个消费者自己记得。
                name=_sanitize_name(str(relative).replace("\\", "/")),
                path=path,
                size=size,
                mime=guess_mime(relative.name),
            )
        )

    notes: list[str] = []
    if over_count:
        notes.append(f"产物超过 {_MAX_ARTIFACTS} 个，有 {over_count} 个没有被保存")
    if over_size:
        limit_mb = _MAX_TOTAL_BYTES // (1024 * 1024)
        notes.append(f"产物总大小超过 {limit_mb} MB，有 {over_size} 个没有被保存")

    return found, notes


def _sanitize_name(name: str) -> str:
    """去掉文件名里的控制字符。

    这不是路径安全措施（防穿越的是 uuid 落盘，见 `artifacts/base.py`），
    而是**协议安全措施**：这个名字会进 HTTP 响应头。POSIX 的文件名可以含
    `\\r\\n`，而把未清洗的字符串拼进响应头就是经典的 header injection。

    ⚠️ **先 strip 再替换，顺序不能反。** 反过来的话，行尾的 `\\r\\n` 会先被
    换成 `?`，而 `?` 是可打印字符，`strip()` 就再也去不掉它了 ——
    一个本该干干净净的名字会带着两个问号尾巴。

    剩下的位置只保留可打印字符，其余换成 `?`。用 `?` 而不是删掉，是为了让
    「这里原来有个怪字符」这件事在界面上看得见，而不是让两个不同的文件名
    变成同一个。
    """
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in name.strip())
    return cleaned or "未命名产物"


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
