"""模型 Provider 抽象层。

目标：让上层 Agent 代码完全不关心底层是哪家模型。

    base.py       —— ``ChatProvider`` 协议：``async def stream(messages, **kw)``
                     抽象的关键在于「流式」，所有实现都必须吐出统一的事件序列。
    openai_compat.py —— OpenAI 兼容协议的实现。DeepSeek / 通义 / Kimi / Ollama
                     全都走这条路，只是 base_url 和 model 不同，所以这是主力实现。
    anthropic.py  —— Claude 官方 SDK 实现（协议不同，需要单独适配）。
    registry.py   —— 按名字取 Provider 实例。

M1 你要写的就是这个包——从 ``base.py`` 的协议开始。
"""
