"""沙箱执行器的测试 —— 真的起子进程，真的跑代码。

════════════════════════════════════════════════════════════════════════
这些测试用哪个解释器
════════════════════════════════════════════════════════════════════════
不是 `.venv-sandbox`，而是**当前测试进程自己**（`sys.executable`）。

为什么？因为这里要测的是**执行器**的行为 —— 超时、退出码、环境变量白名单、
输出截断、工作目录隔离。这些和你用哪个解释器无关。
指向 `sys.executable` 的好处是不需要先装那 300MB 的数值计算三件套，
`pytest` 一跑就过，贡献者 clone 下来就能验证。

代价：**这些测试跑的时候，子进程是有权访问项目目录的**（因为没有真隔离）。
所以测试里写的代码必须是善意的 —— 不测「能不能读到 C:/」，因为读得到，
而那恰恰是文档里写明「挡不住」的部分（见 sandbox/base.py 的安全边界说明）。

真正的隔离验证要靠端到端：跑 `scripts/setup_sandbox.py` 之后，
在沙箱里 `import numpy` 能成功、`os.environ.get("DEEPSEEK_API_KEY")` 是 None。
后者在下面有测试覆盖，而且用的是真的环境变量注入 —— 测的是契约，不是实现。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

from modelforge.config import Settings

# PROJECT_ROOT / is_inside 在 M5 被收拢进了 modelforge/paths.py ——
# 以前它们住在 subprocess_exec 里，而 PROJECT_ROOT 在两个模块里各定义了一份。
from modelforge.paths import PROJECT_ROOT, is_inside
from modelforge.sandbox.base import ExecutionResult
from modelforge.sandbox.subprocess_exec import SubprocessExecutor


def _executor(tmp_path: Path, *, max_output: int, name: str) -> SubprocessExecutor:
    """一个指向当前解释器的执行器，工作目录落在 pytest 的临时目录里。

    刻意把 `sandbox_work_dir` 指到 tmp_path 而不是默认的 `sandbox_tmp/`：
    否则跑一次测试就会在项目根目录留下一个 `sandbox_tmp/` 目录，
    而且并发跑测试时会互相打架。
    """
    return SubprocessExecutor(
        settings=Settings(
            sandbox_python=Path(sys.executable),
            sandbox_timeout=10.0,
            sandbox_max_output=max_output,
            sandbox_work_dir=tmp_path / name,
        )
    )


@pytest.fixture
def executor(tmp_path: Path) -> SubprocessExecutor:
    """常规执行器。

    输出上限刻意给得**足够大**（4000 字符）。一开始这里写的是 200，
    结果 `test_traceback_lands_in_stderr` 直接红了 —— 不是因为实现有问题，
    而是因为一个 Python traceback 带上临时目录的绝对路径就超过 200 字符，
    `ValueError` 那行还没轮到就被截掉了。

    教训：**测试夹具的参数也是一种断言。** 把上限设得太紧，
    测的就不是「错误有没有被正确归类」，而是「这台机器的临时目录有多长」。
    """
    return _executor(tmp_path, max_output=4000, name="work")


@pytest.fixture
def tiny_executor(tmp_path: Path) -> SubprocessExecutor:
    """输出上限只有 200 字符的执行器 —— 专门用来测截断。

    截断行为需要一个小上限才测得到，但那个上限只该影响这一个测试文件里的
    几条测试。所以单独一个夹具，而不是把主夹具的上限压低。
    """
    return _executor(tmp_path, max_output=200, name="work-tiny")


# ══════════════════════════════════════════════════════ 基本执行


async def test_prints_stdout(executor: SubprocessExecutor):
    result = await executor.run("print('你好，世界')")

    assert result.launched
    assert result.exit_code == 0
    assert "你好，世界" in result.stdout
    assert result.duration_ms >= 0


async def test_traceback_lands_in_stderr_with_nonzero_exit(executor: SubprocessExecutor):
    """报错必须走 stderr 且退出码非零。

    这两条是模型自我修复的**全部依据** —— 它看不到别的东西。
    """
    result = await executor.run("raise ValueError('炸了')")

    assert result.launched
    assert result.exit_code != 0
    assert "ValueError" in result.stderr
    assert "炸了" in result.stderr
    # traceback 是给模型的，不该混进 stdout（否则界面没法区分「输出」和「报错」）
    assert "ValueError" not in result.stdout


async def test_syntax_error_is_reported_not_crashed(executor: SubprocessExecutor):
    """语法错误的代码也要能安全地跑一遍 —— 这是最常见的情况，模型经常写错。"""
    result = await executor.run("def f(:\n    pass")

    assert result.launched
    assert result.exit_code != 0
    assert "SyntaxError" in result.stderr


# ══════════════════════════════════════════════════════ 超时


async def test_runaway_loop_is_killed(executor: SubprocessExecutor):
    """死循环必须被掐掉，而不是把后端拖死。

    注意断言的是「用时远小于一个正常死循环会占用的时间」——
    如果实现里忘了杀进程，这个测试会挂住直到 pytest 超时，
    那也是一个明确的失败信号。
    """
    result = await executor.run("while True: pass", timeout=1.0)

    assert result.timed_out
    assert result.duration_ms < 10_000
    assert "强制终止" in result.stderr


async def test_timeout_kills_grandchildren_too(executor: SubprocessExecutor):
    """挂着孙子进程的情况也要能杀掉。

    只杀直接子进程是不够的：模型完全可能 `subprocess.Popen` 一个后台进程，
    然后自己立刻退出 —— 那时父进程早就没了，但孙子还在占 CPU。
    这就是 `_kill_tree` 里 Windows 要用 `taskkill /T`、POSIX 要用进程组的原因。
    """
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "print('spawned', flush=True)\n"
        "while True: pass\n"
    )
    result = await executor.run(code, timeout=1.5)

    assert result.timed_out
    assert "spawned" in result.stdout  # 确认孙子真的被创建出来了，否则这测试没意义


# ══════════════════════════════════════════════════════ 隔离性


async def test_api_key_is_invisible_inside_sandbox(
    executor: SubprocessExecutor, monkeypatch: pytest.MonkeyPatch
):
    """**ADR-005 的核心断言。**

    故意在**测试进程**里设一个真密钥，然后进沙箱里读它。
    断言的是行为（沙箱里读不到），不是实现（白名单里没有这一项）——
    这样以后换成 Docker 执行器，这条测试依然有意义。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-never-leak")

    result = await executor.run(
        "import os\nprint(repr(os.environ.get('DEEPSEEK_API_KEY')))"
    )

    assert "sk-should-never-leak" not in result.stdout
    assert result.stdout.strip() == "None"


async def test_arbitrary_env_vars_are_not_inherited(
    executor: SubprocessExecutor, monkeypatch: pytest.MonkeyPatch
):
    """白名单是白名单，不是黑名单。

    这条测试用的是一个**没人想过要拉黑的**变量名。如果哪天有人把实现改成
    「从 os.environ 复制再删掉几个」，这条会红 —— 而那种实现是真危险的，
    因为它依赖「我记得所有敏感变量名」这个不成立的假设。
    """
    monkeypatch.setenv("SOME_RANDOM_SECRET_XYZ", "leak-me")

    result = await executor.run("import os\nprint(repr(os.environ.get('SOME_RANDOM_SECRET_XYZ')))")

    assert result.stdout.strip() == "None"


async def test_working_directory_is_a_fresh_temp_dir(executor: SubprocessExecutor):
    """工作目录不能是项目目录 —— 否则模型一句 `open('pyproject.toml','w')`
    就能把项目文件覆盖掉。"""
    result = await executor.run("import os\nprint(os.getcwd())")

    cwd = Path(result.stdout.strip()).resolve()
    assert cwd != PROJECT_ROOT
    assert PROJECT_ROOT not in cwd.parents


async def test_state_does_not_leak_between_runs(executor: SubprocessExecutor):
    """每次执行都是全新的空目录。

    这是 `run_python` 的工具描述里**向模型承诺过**的行为
    （见 sandbox/tools.py）。承诺和实现对不上，模型就会被坑：
    它会以为上次写的文件还在，然后拿到一个 FileNotFoundError 却不知道为什么。
    """
    await executor.run("open('marker.txt', 'w').write('x')")

    result = await executor.run("import os\nprint(os.path.exists('marker.txt'))")

    assert result.stdout.strip() == "False"


async def test_home_directory_is_redirected(executor: SubprocessExecutor):
    """`~` 要指向沙箱自己的临时目录，不能是用户真实的家目录。"""
    result = await executor.run("import os\nprint(os.path.expanduser('~'))")

    sandbox_home = Path(result.stdout.strip()).resolve()
    assert sandbox_home != Path.home().resolve()


async def test_matplotlib_backend_is_forced_to_agg(executor: SubprocessExecutor):
    """必须是无头后端。默认后端会尝试开窗口，在服务器上直接崩。"""
    result = await executor.run("import os\nprint(os.environ.get('MPLBACKEND'))")

    assert result.stdout.strip() == "Agg"


async def test_utf8_encoding_is_forced(executor: SubprocessExecutor):
    """不强制 UTF-8 的话，Windows 上子进程打印中文会 UnicodeEncodeError。

    而模型看到那个报错会一脸茫然 —— 它写的中文 `print` 明明没错。
    """
    result = await executor.run("import sys\nprint(sys.stdout.encoding)")

    assert "utf-8" in result.stdout.lower()


# ══════════════════════════════════════════════════════ 输出处理


async def test_long_output_is_truncated(tiny_executor: SubprocessExecutor):
    """输出必须截断 —— 不截的话一次 print 循环就能把上下文塞爆，
    而 prompt token 是**按量付费**的。"""
    result = await tiny_executor.run("print('x' * 5000)")

    assert "已截断" in result.stdout
    # 上限设的是 200 字符，加上提示语也该远小于原始的 5000
    assert len(result.stdout) < 400


async def test_truncation_happens_on_stderr_too(tiny_executor: SubprocessExecutor):
    result = await tiny_executor.run("import sys\nsys.stderr.write('y' * 5000)")

    assert "已截断" in result.stderr


async def test_multibyte_output_is_not_mangled(tiny_executor: SubprocessExecutor):
    """中文不能被截断逻辑劈成乱码。

    实现里只读前 N*4 个**字节**再解码，就是为了让一个汉字（3 字节）
    不至于被从中间切开。

    断言写成「最多一个替换字符」而不是「一个都没有」，是因为截断边界
    理论上仍可能落在字符中间 —— 真发生了也就是最后一个字符变 `�`，
    而它紧挨着「已截断」提示，没人会当真。要求更严反而会让测试对
    读取块大小的调整过度敏感。
    """
    result = await tiny_executor.run("print('熵' * 500)")

    assert result.stdout.count("熵") >= 190  # 200 的上限里绝大多数是完整的汉字
    assert result.stdout.count("�") <= 1  # � 就是那个 �


# ══════════════════════════════════════════════════════ 失败路径


async def test_missing_interpreter_is_an_error_not_a_crash(tmp_path: Path):
    """沙箱环境没装好，是**我们这边**的问题，不是模型代码的问题。

    所以它必须变成一个 `ExecutionResult.error`，而不是异常 ——
    否则整条对话流会断掉，而用户完全不知道发生了什么。
    错误信息里还要给出下一步动作（去跑 setup_sandbox.py）。
    """
    executor = SubprocessExecutor(
        settings=Settings(
            sandbox_python=tmp_path / "nope" / "python.exe",
            sandbox_work_dir=tmp_path / "work",
        )
    )

    result = await executor.run("print(1)")

    assert result.error is not None
    assert not result.launched
    assert "setup_sandbox.py" in result.error


def test_default_work_dir_is_outside_the_project():
    """**回归测试**：默认工作目录必须在项目之外。

    这不是审美偏好，是硬性要求 —— 详见 sandbox/subprocess_exec.py 的「⑤」：
    沙箱往项目里写 `solution.py` 会让 uvicorn 的 `--reload` 反复触发热重载，
    而重载的终止信号会打断正在执行的沙箱代码，表现为随机的 KeyboardInterrupt。

    有人「顺手」把默认值改回 `sandbox_tmp/` 时，这条会红。
    """
    executor = SubprocessExecutor(settings=Settings(sandbox_python=Path(sys.executable)))

    assert not is_inside(executor.work_root, PROJECT_ROOT), (
        f"默认工作目录 {executor.work_root} 落在项目里，"
        "会让 uvicorn --reload 反复触发重载"
    )


def test_work_dir_inside_project_emits_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """配错了要**自己开口说**，而不是等着别人去猜为什么时灵时不灵。

    配置成项目内是允许的（也许你用了 --reload-exclude），
    所以这里是警告而不是报错 —— 但必须有，否则这个问题查起来能花掉一整天。
    """
    with caplog.at_level("WARNING", logger="modelforge.sandbox.subprocess_exec"):
        SubprocessExecutor(
            settings=Settings(
                sandbox_python=Path(sys.executable),
                sandbox_work_dir=PROJECT_ROOT / "sandbox_tmp",
            )
        )

    # 用 record.getMessage() 而不是 `record.message % record.args`：
    # caplog 在把它交给 test 之前已经调用过 getMessage()，message 里是**插值完**的
    # 字符串；再拿它去 % args 会抛「not all arguments converted」。
    messages = [record.getMessage() for record in caplog.records]
    assert any("uvicorn --reload" in text for text in messages), (
        f"没有发出预期的警告。捕获到的日志：{messages}"
    )


def test_works_under_a_selector_event_loop(tmp_path: Path):
    """**回归测试**：执行器不能依赖「服务器碰巧选了哪个事件循环」。

    复现的是 uvicorn `--reload` 的真实处境 —— 它为了做热重载，会在 Windows 上
    **刻意选择 SelectorEventLoop**（见 uvicorn/loops/asyncio.py 的
    `asyncio_loop_factory(use_subprocess=True)`），而那个循环
    **根本不支持 asyncio 的子进程 API**：调用会抛一个**消息为空**的
    NotImplementedError，极难定位。

    最初的实现就是这么写的 —— 24 个离线测试全绿，单独跑执行器也正常，
    一接到 uvicorn 上就炸。原因是 pytest 用默认（Proactor）策略，
    **测试环境比真实环境宽松**。这是「测试通过」最危险的一种形式：
    它给你的是信心，不是证据。

    修法不是去改事件循环策略（那个选择发生在我们代码被 import 之前，
    应用层根本管不着），而是让执行器**根本不依赖事件循环** ——
    用 `asyncio.to_thread` 包一个阻塞式 Popen。

    这个测试自己起一个 SelectorEventLoop，所以在任何平台上都有意义：
    Linux 上它本来就是默认循环，等于顺带又验证一遍。
    """
    executor = _executor(tmp_path, max_output=4000, name="work-selector")

    async def run_once() -> ExecutionResult:
        return await executor.run("print('在 Selector 循环里跑通了')")

    loop = asyncio.SelectorEventLoop()
    try:
        result = loop.run_until_complete(run_once())
    finally:
        loop.close()

    assert result.error is None, f"Selector 循环下执行失败：{result.error}"
    assert result.exit_code == 0
    assert "跑通了" in result.stdout


async def test_execution_does_not_block_the_event_loop(executor: SubprocessExecutor):
    """执行期间事件循环必须还能干别的活。

    守的是「用线程」这个决定的**收益**。如果哪天有人把它改回在协程里
    直接调阻塞版 `subprocess.run`，功能测试照样全过 —— 但后端会在
    每次代码执行的这几百毫秒里完全卡死，连 `/api/health` 都响应不了。
    那种退化只有这条测试能抓到。
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await executor.run("import time; time.sleep(0.5)", timeout=10)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # 0.5 秒里每 10ms 一跳，理想情况约 50 次。给个宽松的下限即可 ——
    # 这条测试要抓的是「完全卡死」（ticks 会是 0 或个位数），不是精确频率。
    assert ticks > 10, f"执行期间事件循环只跳了 {ticks} 次，说明被阻塞了"


async def test_concurrent_runs_do_not_share_a_directory(executor: SubprocessExecutor):
    """并发执行的两次运行不能互相看见对方的文件。

    这是 `tempfile.mkdtemp()` 而不是固定目录名的理由 ——
    模型一次返回多个工具调用时，后端是**顺序**执行的，
    但将来一旦改成并发，固定目录名就会立刻出问题。
    """
    import asyncio

    results = await asyncio.gather(
        executor.run("open('a.txt','w').write('x')\nprint('ok')"),
        executor.run("import os\nprint(os.path.exists('a.txt'))"),
    )

    assert results[0].stdout.strip() == "ok"
    # 第二个不能看到第一个写的文件
    assert results[1].stdout.strip() == "False"
