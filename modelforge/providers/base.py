"""Provider 抽象层的接口契约。

上层代码（Agent、状态机）只依赖这个文件，不依赖任何具体厂商的 SDK。
换模型 = 换一个满足 `ChatProvider` 的实现，上层一行不用改。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from modelforge.providers.events import ProviderEvent

__all__ = ["ChatProvider", "Message", "ToolSpec"]


# ────────────────────────────────────────────────── 内部消息格式
#
# 决策：内部统一使用 OpenAI 的 chat 消息格式。
#
# 依据是实测：DeepSeek / 通义 / Kimi / Ollama / vLLM 全部兼容 OpenAI 协议，
# 用它的格式当内部标准意味着「只有一个 Provider 需要写真正的转换代码」
# （Anthropic，因为它协议不同）。
#
# 已知代价：OpenAI 的概念会渗透到上层状态机。这是被接受的选择，不是疏忽。
#
# 为什么不定义自己的 Message 类？
#   因为那样每个 Provider 都要写双向转换，而我们只需要单向（只有一家不兼容）。
#   抽象的价值在于收敛复杂度，不是在于「拥有自己的模型」本身。

Message = dict[str, Any]
"""形如 {"role": "user", "content": "..."}。

role 取值：system | user | assistant | tool
带工具结果的消息还要有 tool_call_id 字段，assistant 消息则可能带 tool_calls。
"""

ToolSpec = dict[str, Any]
"""形如 {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}。

参数用 JSON Schema 描述。M3 会给沙箱执行工具写第一份 ToolSpec。
"""


@runtime_checkable
class ChatProvider(Protocol):
    """所有模型接入方都必须满足的契约。

    刻意保持极小 —— 只有一个「干活」的方法。抽象层的接口越大，能塞进来的实现就越少。
    （下面还声明了 `aclose`，但那不是能力，是生命周期：有连接池的东西就得有
     关闭的方式，否则进程退出时会留下没关闭的连接。这和「接口要小」不冲突。）
    """

    name: str
    """Provider 的标识名，如 "deepseek" / "ollama"。用于日志和错误信息。"""

    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """流式生成，逐段吐出归一化事件。

        ⚠️ 返回的是 `ProviderEvent`（五种），不是 `StreamEvent`（六种）。
        Provider **没有能力**产出 `ToolResult` —— 那是沙箱跑完代码之后由
        Agent 循环造出来的。用类型把这条边界划清楚，比写注释可靠。

        实现者应该写成一个 `async def` + `yield` 的异步生成器（这里用 `def` 声明
        是因为「异步生成器函数」的类型本来就是 `Callable[..., AsyncIterator]`）。

        必须遵守的契约：
          1. **`Finish` 一定是最后一个事件。** 即使底层把结束标志和用量信息
             分散在不同 chunk 里，实现方也要负责排序。
          2. **出错时不抛异常，而是 yield 一个 `ErrorEvent`。** 详见 events.py。
          3. **不做重试。** 已定决策：一旦流开始就无法回滚，重试会让用户看到
             前半句被重复输出。上层状态机知道当前进度，由它决定策略。
             （连接建立「之前」的失败由 SDK 的 max_retries 处理，那是另一回事。）

        Args:
            messages: 对话历史，OpenAI 格式。
            tools: 可用的工具声明。None 表示本轮不使用工具。
            temperature: 采样温度。数模求解希望结果稳定，默认值偏保守。
            max_tokens: 输出上限。None 表示用服务端默认值。
        """
        ...

    async def aclose(self) -> None:
        """释放底层连接池。进程退出时调用。

        为什么是 async？因为关闭 TCP 连接本身要等对端回应，是 I/O。
        实现方即使在没事可做时也必须能安全地被调用（幂等）。
        """
        ...
