"""端到端验证：从一道题走到一篇能下载的论文草稿。

用法（**先确保后端在跑**，它会用你 `.env` 里的真实模型）：

    .venv/Scripts/python scripts/smoke_report.py
    .venv/Scripts/python scripts/smoke_report.py "帮我算权重并画出来"

════════════════════════════════════════════════════════════════════════
它比单元测试多看到了什么
════════════════════════════════════════════════════════════════════════
`tests/test_reports_api.py` 已经把编排测完了 —— 但它跑在一个**假模型**上，
而报告这件事有一半的价值在「模型写出来的那段文字到底像不像论文」。
那部分测不了，只能看。

它还会验一件单元测试永远看不见的事：**流是不是真的在流**。
四节报告如果被攒起来一次性发下来，所有断言照样全绿 ——
而用户面对的是一个转半分钟的圈。这个脚本会打印每一节到达的时刻。

════════════════════════════════════════════════════════════════════════
它会往你的用户目录里写东西，而且**不删**
════════════════════════════════════════════════════════════════════════
和 `smoke_artifact.py` 一样：建一个新会话（不碰你在用的那些），
报告和它的产物都留着 —— 因为要你亲眼读一遍。

清理：在 /chat 里删掉那个会话，或者直接删那个目录。

════════════════════════════════════════════════════════════════════════
最后要人看的东西
════════════════════════════════════════════════════════════════════════
脚本能验结构（几节、有没有决策记录、`.md` 是不是合法 UTF-8），
验不了「读起来像不像论文」。所以它会把正文的头几段打出来给你看。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / ".smoke"

API_BASE = "http://127.0.0.1:8000"

# 和 smoke_artifact.py 用的是同一道题 —— 样本量给足（8×4），
# 熵权法在这个规模上良定义，没有真正需要拍板的分岔口。
# 但它**可能还是**会停下来问，那样正好能顺带验一下决策记录那一块。
DEFAULT_PROMPT = (
    "我有一组评价数据，8 个评价对象、4 个指标（都是效益型，越大越好）：\n"
    "[[12,30,45,20],[8,25,50,18],[15,28,40,25],[20,35,38,22],"
    "[9,22,44,30],[17,31,47,19],[13,27,42,28],[11,33,36,24]]\n"
    "请用熵权法算出权重，并画一张论文里能直接用的插图。"
)

# 读超时给得很宽：报告是**四节、每节一次模型调用**，比一轮对话慢得多，
# 而对话那一轮本身也要跑好几轮工具。卡死的时候还是会有个头。
REPORT_TIMEOUT = 420.0

KNOWN_KINDS = {"prose", "decision", "figure", "code", "note"}


def fail(message: str) -> None:
    print(f"\n✗ {message}")
    raise SystemExit(1)


def read_sse(response: httpx.Response) -> list[tuple[str, dict]]:
    """把 SSE 流读完，返回 `(事件名, 载荷)` 列表。"""
    frames: list[tuple[str, dict]] = []
    name = "message"
    data_lines: list[str] = []

    for line in response.iter_lines():
        if line == "":
            if data_lines:
                frames.append((name, json.loads("\n".join(data_lines))))
            name, data_lines = "message", []
            continue
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: ") :])

    if data_lines:
        frames.append((name, json.loads("\n".join(data_lines))))
    return frames


def check_backend(client: httpx.Client) -> None:
    try:
        response = client.get(f"{API_BASE}/api/health", timeout=5.0)
    except httpx.ConnectError:
        fail(
            f"连不上后端 {API_BASE}。先在项目根目录起一个：\n"
            "  .venv/Scripts/python -m uvicorn modelforge.main:app --reload --port 8000"
        )
    if response.status_code != 200:
        fail(f"健康检查返回 {response.status_code}：{response.text[:200]}")
    print(f"✓ 后端在跑：{response.json()}")


# ══════════════════════════════════════════════════════ 第一步：聊出材料


def talk(client: httpx.Client, session_id: str, prompt: str) -> None:
    """发一条消息，必要时替用户拍板，直到模型不再要东西为止。

    ⚠️ 为什么要替它拍板：模型很可能在这道题上停下来问一句（那是对的，
    见 smoke_artifact.py 里那段说明）。而**报告需要至少一个已答复的决策**
    才能验到「决策记录块」那一条 —— 那道题里最值钱的一块。

    所以这里自动选第一个选项，并在输出里**明说**「这是脚本替你选的」，
    免得有人以为报告里的那些决策是自己做的。
    """
    print(f"\n▶ 问它：{prompt}\n")

    with client.stream(
        "POST",
        f"{API_BASE}/api/sessions/{session_id}/messages",
        json={"content": prompt},
    ) as response:
        if response.status_code != 200:
            response.read()
            fail(f"发消息失败 {response.status_code}：{response.text[:300]}")
        frames = read_sse(response)

    print(f"✓ 收到 {len(frames)} 帧")

    for rounds in range(1, 5):  # 最多接四轮，防死循环
        decisions = [p for n, p in frames if n == "decision_request"]
        if not decisions:
            break

        decision = decisions[-1]
        choice = decision["options"][0]
        print(
            f"\n⚠️ 蒟蒻停下来问了（第 {rounds} 次）：{decision['question']}\n"
            f"   脚本替你选了第一个：{choice!r}\n"
            f"   —— 报告里那条决策记录会写明这是选出来的，不是你做的。"
        )

        with client.stream(
            "POST",
            f"{API_BASE}/api/sessions/{session_id}/decisions",
            json={"call_id": decision["call_id"], "choice": choice, "note": ""},
        ) as response:
            if response.status_code != 200:
                response.read()
                fail(f"提交决策失败 {response.status_code}：{response.text[:300]}")
            frames = read_sse(response)
        print(f"✓ 接上之后又收到 {len(frames)} 帧")

    artifacts = [
        a
        for name, payload in frames
        if name == "tool_result"
        for a in payload.get("artifacts", [])
    ]
    if not artifacts:
        print(
            "\n⚠️ 这一轮没有产出任何图。报告还是能生成（代码块照旧），"
            "但验不到「图表块」那一条。换个更明确要求出图的提示词重跑。"
        )
    else:
        print(f"✓ 产出了 {len(artifacts)} 个产物")


# ══════════════════════════════════════════════════════ 第二步：写报告


def write_report(client: httpx.Client, session_id: str) -> dict:
    print("\n▶ 生成报告：\n")
    started = time.monotonic()
    marks: list[tuple[str, float, int]] = []

    with client.stream(
        "POST", f"{API_BASE}/api/sessions/{session_id}/report"
    ) as response:
        if response.status_code != 200:
            response.read()
            fail(f"生成报告失败 {response.status_code}：{response.text[:300]}")

        frames: list[tuple[str, dict]] = []
        name = "message"
        data_lines: list[str] = []
        for line in response.iter_lines():
            if line == "":
                if data_lines:
                    frame = (name, json.loads("\n".join(data_lines)))
                    frames.append(frame)
                    marks.append((name, time.monotonic() - started, len(data_lines)))
                name, data_lines = "message", []
                continue
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        if data_lines:
            frames.append((name, json.loads("\n".join(data_lines))))

    total = time.monotonic() - started
    names = [n for n, _ in frames]

    for required in ("report_start", "report_done"):
        if required not in names:
            errors = [p["message"] for n, p in frames if n == "report_error"]
            fail(f"报告流里没有 {required}。" + (f"错误：{errors}" if errors else ""))

    # ── 逐节到达的时刻：**这条是单元测试看不见的** ──
    #
    # 四节如果被攒起来一次性发下来，所有断言照样全绿 —— 而用户面对的是
    # 一个转半分钟的圈。所以把每一节开始的时刻打出来，
    # 人一眼就能看出「是不是在流」。
    print(f"✓ 用时 {total:.1f}s，{len(frames)} 帧。逐节到达时刻：")
    section_starts = [(i, t) for i, (n, t, _) in enumerate(marks) if n == "report_start"]
    if section_starts:
        first_section_at = section_starts[0][1]
        print(f"    骨架到达        {first_section_at:6.1f}s")
        for index, (_, when, _) in enumerate(
            (m for m in marks if m[0] == "report_section_done"), start=1
        ):
            print(f"    第 {index} 节写完    {when:6.1f}s")
        if len(section_starts) == 1 and first_section_at < 1.0:
            print("    （骨架不到 1 秒就下来了 —— 图不必等正文写完）")

    document = next(p["document"] for n, p in frames if n == "report_done")
    handle = next(p["report"] for n, p in frames if n == "report_done")
    return {"document": document, "handle": handle}


def inspect(document: dict, session_id: str) -> None:
    """把报告的结构摊开 —— 顺便验几条**必须成立**的性质。"""
    sections = document["sections"]
    print(f"\n✓ 报告《{document['title']}》，{len(sections)} 节：")

    for section in sections:
        kinds = [b["kind"] for b in section["blocks"]]
        unknown = set(kinds) - KNOWN_KINDS
        if unknown:
            fail(f"「{section['title']}」里有工作台不认得的块：{sorted(unknown)}")
        counts: dict[str, int] = {}
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
        summary = "、".join(f"{k}×{c}" for k, c in counts.items()) or "（只有正文）"
        print(f"    {section['title']:<16} {summary}")

    # ── 报告里最要紧的那条性质 ──
    #
    # 「图和数字不经过模型的手」是这份报告可不可信的全部依据（ADR-015）。
    # 落到这个层面上能验的是：**决策记录里带着用户的选择和备注**、
    # **图表块引用的产物真的取得回来**。引用取不到的话，
    # 报告上会是一个裂图，而正文还说得头头是道。
    decisions = [b for s in sections for b in s["blocks"] if b["kind"] == "decision"]
    for item in decisions:
        print(
            f"\n    决策记录 · 第 {item['seq']} 步\n"
            f"      问：{item['question']}\n"
            f"      选：{item['choice']}"
            + (f"\n      备注：{item['note']}" if item["note"] else "")
        )
        if not item["choice"]:
            fail("决策记录里没有『用户选了什么』—— 报告里最值钱的那一块是空的")

    figures = [b for s in sections for b in s["blocks"] if b["kind"] == "figure"]
    if not figures:
        print("\n    ⚠️ 这份报告里没有图（这一轮模型没出图）。")
    else:
        print(f"\n    图表块 {len(figures)} 个：")
        for item in figures:
            print(f"      {item['name']}  —  {item['caption'] or '（没有说明）'}")

    for note in document.get("notes", []):
        print(f"\n    报告级说明：{note}")


# ══════════════════════════════════════════════════════ 第三步：取回来


def fetch_and_check(client: httpx.Client, session_id: str, payload: dict) -> None:
    """把两个产物下载下来，验内容和**刷新之后还在不在**。"""
    handle = payload["handle"]
    print("\n▶ 下载两个产物：")

    for label, ref in (("Markdown", handle["document"]), ("JSON", handle["sidecar"])):
        got = client.get(f"{API_BASE}/api/artifacts/{session_id}/{ref['id']}")
        if got.status_code != 200:
            fail(f"{label} 取不到：{got.status_code} {got.text[:200]}")
        if len(got.content) != ref["size"]:
            fail(
                f"{label} 的字节数和日志里记的对不上"
                f"（{len(got.content)} vs {ref['size']}）—— 多半是搬迁失败了一半"
            )
        target = OUT_DIR / ref["name"]
        target.write_bytes(got.content)
        print(f"    {ref['name']:<32} {len(got.content):>8} B → {target}")

    # Markdown 必须是**能直接打开的中文文本**。
    # 写文件时忘了 encoding="utf-8" 的话，Windows 上会用 GBK ——
    # 而 JSON 那边不受影响，于是 sidecar 好好的、.md 打开是乱码。
    markdown = (OUT_DIR / handle["document"]["name"]).read_text(encoding="utf-8")
    for heading in ("#", "##"):
        if heading not in markdown:
            fail(f"Markdown 里没有 {heading} 标题 —— 它看起来不像一篇文档")
    print(f"\n✓ Markdown 是合法 UTF-8，{len(markdown)} 字，有结构")

    # 相对路径那条说明必须在 —— 否则用户下载完打开是一堆裂图，
    # 而且会以为是生成坏了。
    if "相对路径" not in markdown:
        fail("Markdown 开头没有说明图片用相对路径 —— 用户会看到一堆裂图而不知道原因")

    # JSON 必须是完整的一份文档，不是半截
    sidecar = json.loads((OUT_DIR / handle["sidecar"]["name"]).read_bytes())
    if sidecar["session_id"] != session_id:
        fail("sidecar 里的 session_id 对不上")
    if not sidecar["sections"]:
        fail("sidecar 里一节都没有")
    print(f"✓ sidecar 是完整的 JSON 文档（version={sidecar['version']}，"
          f"{len(sidecar['sections'])} 节）")

    # ── 刷新不丢 ──
    #
    # 这是 M4 那条验收标准在报告上的对应物。报告不是工具调用产出的，
    # 它进日志走的是第八种事件 —— 任何一环漏了，刷新之后它就没了。
    detail = client.get(f"{API_BASE}/api/sessions/{session_id}").json()
    if not detail.get("reports"):
        fail(
            "刷新之后会话详情里没有这份报告 —— 后端的 list_reports 没接上？\n"
            "（这是 M4 那条『刷新不丢』在报告上的对应物）"
        )
    print(f"✓ 刷新之后报告还在：reports 里有 {len(detail['reports'])} 份")


def main() -> int:
    parser = argparse.ArgumentParser(description="报告链路的端到端验证")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    session_id = ""

    with httpx.Client(timeout=httpx.Timeout(30.0, read=REPORT_TIMEOUT)) as client:
        check_backend(client)

        session_id = client.post(f"{API_BASE}/api/sessions").json()["id"]
        print(f"✓ 建了会话 {session_id[:8]}…")

        talk(client, session_id, args.prompt)
        payload = write_report(client, session_id)
        inspect(payload["document"], session_id)
        fetch_and_check(client, session_id, payload)

    print(
        "\n" + "─" * 68 + "\n"
        "✓ 报告链路通了。\n"
        f"    下载下来的两份在：{OUT_DIR}\n"
        f"    想读排版好的那版：http://localhost:3000/chat?s={session_id}\n"
        "    点右栏产物清单里的那一条（标着「报告」），然后「读全文」\n"
        "\n"
        "⚠️ **剩下的是文字的事，脚本验不了，只能你读：**\n"
        "    · 正文像不像论文（而不是像聊天记录被抄了一遍）\n"
        "    · 「模型假设」那一节有没有把决策记录串成有逻辑的假设\n"
        "    · 正文里那几个数字**有没有出处**（找不到出处的话，\n"
        "      工作台会在旁边给一条核对提示 —— 那不是指控，是让你扫一眼）\n"
        "\n"
        f"清理：在 /chat 里删掉会话 {session_id[:8]}…，或者直接删那个目录。\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
