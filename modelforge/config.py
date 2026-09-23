"""全局配置。

所有配置项都可以被同名（大写）的环境变量覆盖，也可以写在项目根目录的 ``.env`` 里。
字段名 ``deepseek_api_key`` 对应环境变量 ``DEEPSEEK_API_KEY``。
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from modelforge.paths import user_data_dir


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 服务 ----------
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    # 前端 dev server 的地址。Next.js 默认跑在 3000。
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # ---------- 存储 ----------
    database_path: Path = Path("data/modelforge.db")
    """会话 / 事件日志的 SQLite 文件。

    相对路径会**锚在项目根目录上**，不是当前工作目录 —— 锚 CWD 的话，
    在 `web/` 里启动一次后端就会得到一个空数据库，然后开始怀疑
    「为什么我的会话全不见了」。解析逻辑在 `sessions/sqlite_store.py`
    的 `resolve_db_path()`，和沙箱工作目录用的是同一条规矩。

    这个目录已经在 .gitignore 里（`data/`、`*.db`），不会进仓库。
    """

    session_lease_seconds: float = 180.0
    """一个会话被某条流独占多久（秒）。

    这个 TTL 是**「租约泄漏时的最大代价」**，所以要卡在两头之间：

      · 太短 → 一轮对话还没跑完租约就到期，另一条流能挤进来，
              两条流交错写同一个日志（投影出来是非法历史）。
      · 太长 → 后端起停、进程被杀之后，那个会话会卡住这么久才自愈。

    最坏耗时 ≈ `agent_max_tool_rounds × (sandbox_timeout + 模型延迟)`
    ≈ 5 × (10 + 5) = 75 秒。180 秒是它的两倍多，留了余量。
    **改了 `agent_max_tool_rounds` 或 `sandbox_timeout` 要回头看这个值。**
    """

    artifacts_dir: Path = user_data_dir() / "artifacts"
    """模型生成的产物（图表、CSV、报告）落盘的根目录。

    ⚠️ **默认值刻意在项目目录之外，不要随手改回 `artifacts/`。**

    这里踩过一个 M3 已经踩过一次的坑，形态一模一样但更难发现：
    uvicorn 的 `--reload` 盯着整个项目目录。产物一落盘，热重载就被触发，
    而重载发出的终止信号会顺着控制台传到**正在跑代码的沙箱子进程**上 ——
    模型会时不时收到一个莫名其妙的 `KeyboardInterrupt`（退出码 3221225786）。

    为什么说「更难发现」：这个坑在 M3 是每次执行写一个 `solution.py`
    触发的（每次都中），而产物是**只有画图时**才落盘 —— 于是它看起来
    像「模型偶尔抽风」，尤其是画图那几轮。竞态 + 低频 = 最难查的组合。

    详见 `sandbox/subprocess_exec.py` 模块注释的「⑤」，以及
    `modelforge/artifacts/local_store.py` 里那条启动警告。
    """

    # ---------- 模型 Provider ----------
    # M1 会基于下面这些字段构建 Provider 抽象层。
    # 约定：所有 Provider 都暴露 OpenAI 兼容接口，因此共用一套 base_url + api_key + model 结构。
    default_provider: str = "deepseek"

    # DeepSeek：https://platform.deepseek.com
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # Ollama：本地模型，无需 API Key
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen2.5:7b"

    # Claude（走官方 SDK，非 OpenAI 兼容协议）
    anthropic_api_key: str | None = None

    # ---------- 沙箱 ----------
    # ADR-005：跑模型生成的代码必须用**独立**的 Python 环境，不能是后端自己的
    # .venv —— 否则一段 `os.environ["DEEPSEEK_API_KEY"]` 就能把密钥读走。
    # 因此 pyproject.toml 里刻意没有 numpy/scipy/matplotlib，它们装在沙箱环境里。
    sandbox_python: Path | None = None
    """沙箱解释器的绝对路径。None 表示自动探测项目根目录下的 .venv-sandbox。"""

    sandbox_timeout: float = 10.0
    """单次执行的墙钟上限（秒）。超时即杀掉整个进程树。"""

    sandbox_max_output: int = 8000
    """stdout/stderr 各自回传给模型的字符上限。超出部分截断并注明。"""

    sandbox_work_dir: Path = Path(tempfile.gettempdir()) / "modelforge-sandbox"
    """每次执行都会在它下面开一个全新的临时子目录当工作目录。

    ⚠️ **默认值刻意放在项目目录之外，不要随手改回 `sandbox_tmp/`。**
    原因是踩过一次很难查的坑，详见 `sandbox/subprocess_exec.py` 的「⑤」：

        uvicorn 的 `--reload` 会盯着项目目录。沙箱每次执行都会生成一个
        `solution.py`，于是热重载被反复触发，而重载时发给服务器的终止信号
        会顺着控制台传到**正在跑代码的沙箱子进程**上 —— 表现为模型时不时
        收到一个莫名其妙的 `KeyboardInterrupt`。

    放在系统临时目录就完全避开了这个问题。想改的话，
    执行器会在启动时检查并在目录落进项目里时打出警告。
    """

    sandbox_mpl_config_dir: Path = user_data_dir() / "mplconfig"
    """沙箱里 matplotlib 的配置目录（`MPLCONFIGDIR`）。M5 新增。

    ⚠️ **必须是持久目录，不能是每次执行都删的工作目录。**

    这里修的是一个真实的性能问题：M3 把 `MPLCONFIGDIR` 设成了
    `<工作目录>/.mplconfig`，而工作目录在每次执行结束时被整个删掉。
    后果是**每一次 `import matplotlib.pyplot` 都要重建字体缓存** ——
    在这台机器上（273 个字体）是秒级的固定开销。

    M3 看不出问题（那次执行总共才 155ms，多花的钱在噪声里），
    但 M5 是图表密集的：画一张图就交一次这个税，而 `sandbox_timeout`
    默认只有 10 秒 —— 画三张图很可能直接撞超时，报错还长得像
    「你的代码太慢」，完全指不到真正的原因。

    这个目录同时承担第二个职责：**放我们的 `matplotlibrc`**。
    matplotlib 查找配置的顺序里，`$MPLCONFIGDIR/matplotlibrc` 排在第一位，
    所以放在这里意味着沙箱里**任何** matplotlib 代码都会自动套上科研风格，
    不需要模型配合（见 `sandbox/runtime/matplotlibrc`）。
    """

    agent_max_tool_rounds: int = 5
    """一轮对话里最多允许多少次「模型要工具 → 执行 → 回填」循环。

    必须有上限：模型可能陷入「反复调用同一个工具」的死循环，
    没有这个闸门就会一直烧 token。

    ⚠️ **M4 之后这个值的语义变了**：它是「**单次** `run_agent_turn` 调用的
    保险丝」，不再是「一次会话的总预算」。因为 HITL 恢复是「用重建出来的
    历史重新跑一次 `run_agent_turn`」，每次都会重新计数 —— 一个问了 5 次
    问题的会话，实际允许的工具轮数会多得多。

    这是有意的取舍，不是疏漏：真要按会话累计，就得把轮数也记成事件、
    在恢复时读回来，而那会让「用户每回答一次问题就续一次命」这件事
    变得难以解释。目前接受它，详见 ADR-009。"""



@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例。

    测试中修改环境变量后需要调用 ``get_settings.cache_clear()`` 才能生效。
    """
    return Settings()
