"""测试之间共享的工具 —— 不是测试，所以文件名不叫 `test_*`。

目前只有一件事：解析 SSE 响应体。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["frames_of", "names_of"]


def frames_of(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 响应体拆成 `(事件名, data)` 列表。

    这是前端 `web/src/lib/sse.ts` 那个解析器的 Python 版。两边**独立实现**
    同一套解析，正好互相验证 —— 谁只改了单边的格式假设，这边的断开会先炸。

    （注意「互相验证」说的是 Python 和 TypeScript 之间。同一个语言里放两份
    实现没有这个价值，只会各自漂移，所以这个函数在 `tests/helpers.py`。）
    """
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name = "message"
        data: str | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        assert data is not None, f"帧里没有 data 行: {block!r}"
        frames.append((name, json.loads(data)))
    return frames


def names_of(body: str) -> list[str]:
    """只要事件名，不要载荷 —— 断言「事件顺序」时用。"""
    return [name for name, _ in frames_of(body)]
