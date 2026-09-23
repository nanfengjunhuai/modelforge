"""模型 Provider 抽象层。

让上层 Agent 代码完全不关心底层是哪家模型。

    base.py           —— `ChatProvider` 协议 + 内部消息格式（即 OpenAI 格式）
    events.py         —— 归一化的流式事件模型（M1 的核心设计）
    openai_compat.py  —— 主力实现，覆盖 DeepSeek / 通义 / Kimi / Ollama / vLLM
    registry.py       —— 按名字装配 Provider

典型用法：

    from modelforge.providers import get_provider, ToolCallAccumulator, ToolCallDelta, Finish

    provider = get_provider()                  # 用配置里的默认 provider
    acc = ToolCallAccumulator()

    async for event in provider.stream([{"role": "user", "content": "你好"}]):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolCallDelta):
            acc.feed(event)
        elif isinstance(event, Finish):
            print()                            # Finish 保证是最后一个事件
            tool_calls = acc.result()
"""

from modelforge.providers.base import ChatProvider, Message, ToolSpec
from modelforge.providers.events import (
    ErrorEvent,
    Finish,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallAccumulator,
    ToolCallDelta,
    Usage,
)
from modelforge.providers.openai_compat import OpenAICompatProvider
from modelforge.providers.registry import available_providers, get_provider

# 按字母序排列，由 ruff 的 RUF022 强制。
# 不按「接口/事件/实现」分组，是因为 lint 规则要求排序，
# 而分组注释一旦被排序打散就会变成误导（注释停在原处、条目已经飞走）。
# 模块级的说明请看本文件开头的文档字符串。
__all__ = [
    "ChatProvider",
    "ErrorEvent",
    "Finish",
    "FinishReason",
    "Message",
    "OpenAICompatProvider",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolCallAccumulator",
    "ToolCallDelta",
    "ToolSpec",
    "Usage",
    "available_providers",
    "get_provider",
]
