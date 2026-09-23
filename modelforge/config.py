"""全局配置。

所有配置项都可以被同名（大写）的环境变量覆盖，也可以写在项目根目录的 ``.env`` 里。
字段名 ``deepseek_api_key`` 对应环境变量 ``DEEPSEEK_API_KEY``。
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Agent 产物（图表、生成的报告、沙箱输出）落盘的根目录
    artifacts_dir: Path = Path("artifacts")

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

    agent_max_tool_rounds: int = 5
    """一轮对话里最多允许多少次「模型要工具 → 执行 → 回填」循环。

    必须有上限：模型可能陷入「反复调用同一个工具」的死循环，
    没有这个闸门就会一直烧 token。"""



@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例。

    测试中修改环境变量后需要调用 ``get_settings.cache_clear()`` 才能生效。
    """
    return Settings()
