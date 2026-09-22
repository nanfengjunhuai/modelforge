"""全局配置。

所有配置项都可以被同名（大写）的环境变量覆盖，也可以写在项目根目录的 ``.env`` 里。
字段名 ``deepseek_api_key`` 对应环境变量 ``DEEPSEEK_API_KEY``。
"""

from __future__ import annotations

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


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例。

    测试中修改环境变量后需要调用 ``get_settings.cache_clear()`` 才能生效。
    """
    return Settings()
