"""Provider 注册表 —— 按名字拿到一个装配好的 Provider 实例。

上层只需要 `get_provider()`，不需要知道任何 base_url / API Key 的细节。
"""

from __future__ import annotations

from typing import NamedTuple

from modelforge.config import Settings, get_settings
from modelforge.providers.base import ChatProvider
from modelforge.providers.openai_compat import OpenAICompatProvider

__all__ = ["available_providers", "get_provider"]


class _Spec(NamedTuple):
    """描述一个 Provider 的配置字段在 Settings 里叫什么名字。

    用「字段名」而不是直接存值，是因为 Settings 是运行时才实例化的 ——
    注册表必须在拿到配置之前就能建好。
    """

    base_url_field: str
    api_key_field: str | None  # None 表示这个服务不需要 API Key
    model_field: str


# 所有走 OpenAI 兼容协议的 Provider。
# 加一个新的国产模型（通义、Kimi、智谱…）只需要在这里加一行，不用写任何新代码。
_OPENAI_COMPAT: dict[str, _Spec] = {
    "deepseek": _Spec("deepseek_base_url", "deepseek_api_key", "deepseek_model"),
    "ollama": _Spec("ollama_base_url", None, "ollama_model"),
}


def available_providers() -> list[str]:
    """返回所有已注册的 Provider 名字，供 CLI 和错误提示使用。"""
    return sorted(_OPENAI_COMPAT)


def get_provider(
    name: str | None = None,
    *,
    settings: Settings | None = None,
) -> ChatProvider:
    """按名字装配一个 Provider。

    Args:
        name: Provider 标识名，不传则用配置里的 `default_provider`。
        settings: 注入配置，主要给测试用；不传则读全局单例。

    Raises:
        ValueError: 名字未注册。
    """
    cfg = settings or get_settings()
    key = (name or cfg.default_provider).lower()

    spec = _OPENAI_COMPAT.get(key)
    if spec is None:
        raise ValueError(
            f"未知的 provider: {key!r}。已注册的有：{available_providers()}。"
            f"如需新增，请在 registry.py 的 _OPENAI_COMPAT 里加一行。"
        )

    model = getattr(cfg, spec.model_field)
    api_key = getattr(cfg, spec.api_key_field) if spec.api_key_field else None

    # 提前把「忘了配 Key」这个最常见的错误变成一句人话，
    # 而不是让它在真正发请求时变成一个 401 报错。
    if spec.api_key_field and not api_key:
        raise ValueError(
            f"provider {key!r} 需要 API Key，但配置为空。"
            f"请复制 .env.example 为 .env 并填写 {spec.api_key_field.upper()}。"
        )

    return OpenAICompatProvider(
        name=key,
        base_url=getattr(cfg, spec.base_url_field),
        model=model,
        api_key=api_key,
    )
