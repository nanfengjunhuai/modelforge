"""用真实的模型跑一次流式请求，肉眼验证 Provider 层是否正常。

单元测试（tests/test_providers.py）验证的是「给定 chunk 能否正确映射成事件」，
但验证不了「真实厂商吐出的 chunk 长什么样、SDK 版本对不对得上」。
这个脚本补上后半截。

用法：

    # 用配置里的默认 provider（.env 中的 DEFAULT_PROVIDER）
    .venv/Scripts/python scripts/smoke_provider.py "用一句话解释什么是熵权法"

    # 指定 provider
    .venv/Scripts/python scripts/smoke_provider.py --provider ollama "你好"

    # 额外验证「工具调用碎片拼装」这条最容易出错的链路
    .venv/Scripts/python scripts/smoke_provider.py --demo-tools "帮我算一下 1 到 100 的和"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

# Windows 控制台默认 GBK，中文输出会直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from modelforge.providers import (  # noqa: E402 —— 必须在编码修正之后导入
    Finish,
    TextDelta,
    ToolCallAccumulator,
    ToolCallDelta,
    Usage,
    available_providers,
    get_provider,
)

# 一个假的工具声明，只用于验证「模型是否会要求调用工具」以及
# 「碎片化的 arguments 能否拼成合法 JSON」。真正的沙箱工具在 M3 实现。
DEMO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "在沙箱中执行一段 Python 代码并返回标准输出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码"},
                },
                "required": ["code"],
            },
        },
    }
]

DEMO_SYSTEM = (
    "你可以调用 run_python 工具来执行代码。"
    "当用户提出需要计算的问题时，必须调用工具，不要自己心算。"
)


async def run(provider_name: str | None, prompt: str, demo_tools: bool) -> int:
    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        # 注册表已经把这个错误写成人话了，直接打印即可
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    print(f"provider = {provider.name}    model = {provider.model}")
    print(f"{'─' * 60}")

    messages: list[dict] = []
    if demo_tools:
        messages.append({"role": "system", "content": DEMO_SYSTEM})
    messages.append({"role": "user", "content": prompt})

    acc = ToolCallAccumulator()
    usage: Usage | None = None
    finish: Finish | None = None

    started = time.perf_counter()
    first_token_at: float | None = None

    async for event in provider.stream(
        messages, tools=DEMO_TOOLS if demo_tools else None, temperature=0.2
    ):
        if isinstance(event, TextDelta):
            if first_token_at is None:
                first_token_at = time.perf_counter()
            print(event.text, end="", flush=True)

        elif isinstance(event, ToolCallDelta):
            if first_token_at is None:
                first_token_at = time.perf_counter()
            acc.feed(event)

        elif isinstance(event, Usage):
            usage = event

        elif isinstance(event, Finish):
            finish = event

        else:  # ErrorEvent
            print(f"\n✗ [{event.type}] {event.message}", file=sys.stderr)
            print(f"  retryable = {event.retryable}", file=sys.stderr)

    elapsed = time.perf_counter() - started

    print()
    print(f"{'─' * 60}")

    # 验证「Finish 一定是最后一个事件」这条契约
    print(f"finish_reason : {finish.reason if finish else '（缺失！契约被破坏）'}")

    if usage:
        print(f"tokens        : prompt={usage.prompt_tokens} completion={usage.completion_tokens}")

    if first_token_at is not None:
        print(f"首字延迟      : {(first_token_at - started) * 1000:.0f} ms")
    print(f"总耗时        : {elapsed * 1000:.0f} ms")

    tool_calls = acc.result()
    if tool_calls:
        import json

        print(f"\n工具调用（{len(tool_calls)} 个）:")
        for tc in tool_calls:
            print(f"  · {tc.name}  id={tc.id}")
            try:
                parsed = json.loads(tc.arguments)
                print(f"    参数解析成功: {json.dumps(parsed, ensure_ascii=False)[:120]}")
            except json.JSONDecodeError as exc:
                # 拼装逻辑出错时这里会红 —— 这正是我们要在这个脚本里验证的
                print(f"    ✗ 参数不是合法 JSON: {exc}")
                print(f"      原始内容: {tc.arguments!r}")
                return 1

    if finish is not None and finish.reason == "error":
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("prompt", help="发给模型的提示词")
    parser.add_argument(
        "--provider",
        default=None,
        help=(
            "provider 名字，默认用 .env 里的 DEFAULT_PROVIDER。"
            f"可选：{', '.join(available_providers())}"
        ),
    )
    parser.add_argument(
        "--demo-tools",
        action="store_true",
        help="注册一个假的 run_python 工具，验证工具调用链路",
    )
    args = parser.parse_args()

    return asyncio.run(run(args.provider, args.prompt, args.demo_tools))


if __name__ == "__main__":
    raise SystemExit(main())
