"""SSE 流式端点的端到端验证。

用法（先在项目根目录把后端跑起来）：

    .venv/Scripts/python scripts/smoke_stream.py
    .venv/Scripts/python scripts/smoke_stream.py "用三句话解释什么是熵权法"

────────────────────────────────────────────────────────────────────────
这个脚本在测一件**单元测试测不到**的事
────────────────────────────────────────────────────────────────────────
`tests/test_stream.py` 用的是 FastAPI 的 TestClient，它会把整个响应体收完
再交给你 —— 用它验证「帧的内容对不对」没问题，但**永远验证不了「有没有真的
在流」**。哪怕后端把整段回答攒完才一次性吐出来，那些测试也照样全绿。

要验证「流」，就必须真的用网络、真的按块读、真的给每一帧记时间。
这正是本脚本存在的理由，也是它不可被单元测试替代的原因。

判据只有一条：
    **首帧到达时间应该明显早于总耗时。**
    两者接近（比如都 3 秒）说明中间某处被缓冲了 —— 那前端就会看到
    「等 3 秒然后整段蹦出来」，而不是逐字出现。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx

# Windows 控制台默认 GBK，不修的话中文输出会抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
DEFAULT_PROMPT = "用三句话解释什么是熵权法"

# 首帧耗时超过总耗时的这个比例，就值得怀疑被缓冲了
BUFFERING_SUSPECT_RATIO = 0.9


def _die(message: str, code: int = 2) -> int:
    print(f"\n✗ {message}")
    return code


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or DEFAULT_PROMPT

    # ── 先确认后端活着，把它没起来的报错和「后端返回了 500」区分开
    try:
        health = httpx.get(f"{BASE}/api/health", timeout=5.0).json()
    except Exception as exc:
        return _die(
            f"连不上后端 {BASE}（{type(exc).__name__}: {exc}）\n"
            "  先把后端跑起来：\n"
            "    .venv/Scripts/python -m uvicorn modelforge.main:app --reload --port 8000"
        )

    print(
        f"后端 :{BASE}  {health['service']} {health['version']}"
        f"  默认模型 {health['default_provider']}"
    )
    print(f"提问：{prompt}")
    print("─" * 72)

    started = time.perf_counter()
    previous = started
    first_frame_ms: float | None = None
    frames = 0
    text_chars = 0
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    error_message: str | None = None

    try:
        with httpx.stream(
            "POST",
            f"{BASE}/api/chat/stream",
            json={"messages": [{"role": "user", "content": prompt}]},
            timeout=120.0,
        ) as response:
            if response.status_code != 200:
                response.read()
                return _die(f"后端返回 {response.status_code}：{response.text[:300]}")

            ct = response.headers.get("content-type", "")
            if not ct.startswith("text/event-stream"):
                return _die(f"content-type 不对：{ct!r}，期望 text/event-stream")

            # 逐行读。我们的帧格式固定是「event 一行 + data 一行 + 空行」，
            # 所以每个 data 行恰好对应一帧 —— 这里依赖这个简化，
            # 通用解析逻辑在 tests/test_stream.py 和 web/src/lib/sse.ts 里。
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue

                now = time.perf_counter()
                elapsed_ms = (now - started) * 1000
                gap_ms = (now - previous) * 1000
                previous = now
                frames += 1
                if first_frame_ms is None:
                    first_frame_ms = elapsed_ms

                event = json.loads(line[len("data: ") :])
                kind = event["type"]

                if kind == "text_delta":
                    text = event["text"]
                    text_chars += len(text)
                    # 把换行显示成 ⏎，免得把表格输出搞乱
                    shown = text.replace("\n", "⏎")
                    print(f"[{elapsed_ms:8.0f}ms +{gap_ms:6.0f}ms] 文字  {shown}")
                elif kind == "usage":
                    usage = event
                    print(f"[{elapsed_ms:8.0f}ms +{gap_ms:6.0f}ms] 用量  {event}")
                elif kind == "finish":
                    finish_reason = event["reason"]
                    print(f"[{elapsed_ms:8.0f}ms +{gap_ms:6.0f}ms] 结束  {event['reason']}")
                elif kind == "error":
                    error_message = event["message"]
                    print(f"[{elapsed_ms:8.0f}ms +{gap_ms:6.0f}ms] 错误  {event}")
                else:
                    print(f"[{elapsed_ms:8.0f}ms +{gap_ms:6.0f}ms] {kind}  {event}")
    except Exception as exc:
        return _die(f"读流时出错（{type(exc).__name__}: {exc}）")

    total_ms = (time.perf_counter() - started) * 1000

    # ── 汇总结论
    print("─" * 72)
    if first_frame_ms is None:
        return _die("一帧都没收到 —— 后端可能挂了，或者路径写错了")

    ratio = first_frame_ms / total_ms if total_ms else 1.0
    print(f"共 {frames} 帧 / {text_chars} 字，首帧 {first_frame_ms:.0f}ms，总耗时 {total_ms:.0f}ms")
    print(f"首帧占总裁  {ratio:.0%}")

    ok = True
    if finish_reason is None:
        print("✗ 没收到 finish —— 契约要求它必须是最后一个事件")
        ok = False
    if error_message:
        print(f"✗ 流里有错误事件：{error_message}")
        ok = False
    if ratio > BUFFERING_SUSPECT_RATIO and frames > 1:
        print(
            "✗ 首帧和总耗时几乎相等 —— 帧是被攒在一起发的，不是真流式。\n"
            "  查一下：是否有反向代理在缓冲？响应头里带 X-Accel-Buffering: no 了吗？"
        )
        ok = False
    if usage:
        print(f"用量  prompt={usage['prompt_tokens']}  completion={usage['completion_tokens']}")

    print("\n✓ 流式验证通过" if ok else "\n✗ 流式验证失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
