"""端到端验证：让蒟蒻真的画一张论文级的图，然后把产物取回来。

用法（**先确保后端在跑**，它会用你 `.env` 里的真实模型）：

    .venv/Scripts/python scripts/smoke_artifact.py
    .venv/Scripts/python scripts/smoke_artifact.py "帮我算权重并画出来"

════════════════════════════════════════════════════════════════════════
它和单元测试的区别
════════════════════════════════════════════════════════════════════════
`tests/test_matplotlib_style.py` 已经覆盖了「沙箱能不能出图」——
在那个层面再写一个脚本是重复的。这个脚本要测的是**它盖不到的那一段**：

    · 真实的 uvicorn 进程（不是 TestClient —— 它会把响应体收完再给你）
    · 真实的 DeepSeek（提示词和工具描述到底有没有让模型用对）
    · 真实的事件日志落盘 + 真实的产物目录（**在生产路径上**，不是 tmp_path）
    · 真实的 HTTP 下载（Content-Disposition 有没有编码对中文文件名）

这正是 M3 那条 `smoke_stream.py` 存在的理由，换了个层面：
**单元测试全绿但真实运行会炸，是这个项目里反复出现的配方。**

════════════════════════════════════════════════════════════════════════
它会往你的用户目录里写东西，而且**不删**
════════════════════════════════════════════════════════════════════════
产物落在 `%LOCALAPPDATA%/modelforge/artifacts/<会话 id>/`（或者你配的
`ARTIFACTS_DIR`）。脚本会在每次启动**建一个新会话**（避免和你在用的
会话打架），但那个会话和它的产物都会留着 —— 因为要你亲眼看图对不对。

看完想清掉：界面上删掉那个会话，或者直接删那个目录。
（删会话会连带删产物；直接删目录留下的空引用会在下次启动被
`sweep_orphans` 收走。）

════════════════════════════════════════════════════════════════════════
它最后会打印什么
════════════════════════════════════════════════════════════════════════
    · 每个产物的名字 / 类型 / 大小 / MIME
    · 下载回来的字节数和文件头（PNG 的魔数 / PDF 的 `%PDF-`）
    · 一个本地目录，里面是**真的从 HTTP 下载下来**的那份文件

**最后一步要人看**：打开那个目录里的 PNG，确认中文没变成方框、
负号正常、图例没有框、多条曲线灰度下能分开。脚本检查不了「好不好看」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import httpx

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / ".smoke"  # 已 gitignore（见 .gitignore 的 artifacts/ 那条旁）

API_BASE = "http://127.0.0.1:8000"

# ⚠️ 这个提示词是**挑过的**，不是随手写的。
#
# 第一版用的是「3 个指标、3 个评价对象」—— 结果模型算到一半停下了，
# 问「n=3 时熵权法退化，两种口径算出的权重完全相反，走哪条？」
#
# **它是对的**：极差归一化之后每一列都有一个 0，取对数会炸，而两个口径
# 给出的权重一个接近均权、一个反差极大。方法没定就先画图才是错的 ——
# 这与「人在环中」的设计完全一致。
#
# 但那让冒烟脚本测不到产物链路。所以这里的样本量给足（8 个对象 × 4 个指标），
# 熵权法在这个规模上是良定义的，没有真正需要拍板的分岔口。
#
# 如果它**还是**停下来问，脚本会明确告诉你（而不是装作没产物就完事了）——
# 那种情况下换一个更具体的提示词重跑，别去改 M5 的代码。
DEFAULT_PROMPT = (
    "我有一组评价数据，8 个评价对象、4 个指标（都是效益型，越大越好）：\n"
    "[[12,30,45,20],[8,25,50,18],[15,28,40,25],[20,35,38,22],"
    "[9,22,44,30],[17,31,47,19],[13,27,42,28],[11,33,36,24]]\n"
    "请用熵权法算出权重，并画一张论文里能直接用的插图。"
)

# 一轮对话最多等多久。模型要跑好几轮工具，慢一点正常 ——
# 但卡死的时候不能一直等下去。
TIMEOUT_SECONDS = 240.0


def fail(message: str) -> None:
    print(f"\n✗ {message}")
    raise SystemExit(1)


def check_backend(client: httpx.Client) -> None:
    """后端没起来的话，后面所有的报错都会长得像别的问题。先拦住。"""
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


def read_sse(response: httpx.Response) -> list[tuple[str, dict]]:
    """把 SSE 流读完，返回 `(事件名, 载荷)` 列表。"""
    frames: list[tuple[str, dict]] = []
    name = "message"
    data_lines: list[str] = []

    for line in response.iter_lines():
        # httpx 的 iter_lines 已经把换行去掉了
        if line == "":
            if data_lines:
                frames.append((name, json.loads("\n".join(data_lines))))
            name, data_lines = "message", []
            continue
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: ") :])

    if data_lines:  # 服务端忘了发结尾空行的情况
        frames.append((name, json.loads("\n".join(data_lines))))
    return frames


def summarize(frames: list[tuple[str, dict]]) -> str:
    """把上千帧压成一行：`text_delta×12 → tool_call_delta×200 → usage → …`

    逐帧打印是没用的 —— 一次真跑能到一千多帧，`text_delta` 和
    `tool_call_delta` 各占几百条，刷屏之后什么也看不出来。
    而**游程压缩之后恰好把「有几轮、每轮干了什么」显示出来了**，
    那才是要看的东西。

    （这个函数是踩出来的：第一版就是逐帧打印的。）
    """
    runs: list[tuple[str, int]] = []
    for name, _ in frames:
        if runs and runs[-1][0] == name:
            runs[-1] = (name, runs[-1][1] + 1)
        else:
            runs.append((name, 1))
    return " → ".join(name if count == 1 else f"{name}×{count}" for name, count in runs)


def main() -> int:
    parser = argparse.ArgumentParser(description="产物链路的端到端验证")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)

    with httpx.Client(timeout=httpx.Timeout(30.0, read=TIMEOUT_SECONDS)) as client:
        check_backend(client)

        # ── 建一个**新**会话 ──
        # 不复用已有的：开发时手边可能正好有一个停在决策点上的会话，
        # 往它里面发消息会拿到 409，而那个 409 和产物链路毫无关系。
        session_id = client.post(f"{API_BASE}/api/sessions").json()["id"]
        print(f"✓ 建了会话 {session_id[:8]}…")

        print(f"\n▶ 问它：{args.prompt}\n")
        started = time.monotonic()

        with client.stream(
            "POST",
            f"{API_BASE}/api/sessions/{session_id}/messages",
            json={"content": args.prompt},
        ) as response:
            if response.status_code != 200:
                response.read()
                fail(f"发消息失败 {response.status_code}：{response.text[:300]}")
            frames = read_sse(response)

        elapsed = time.monotonic() - started
        print(f"✓ 收到 {len(frames)} 帧，用时 {elapsed:.1f}s")
        print(f"  事件序列：{summarize(frames)}")

        # ── 先看它是不是停下来问了 ──
        #
        # 这个检查必须在「找产物」**之前**，否则会报一条完全误导的错：
        # 「这一轮没有任何产物」听起来像链路坏了，而真实情况是
        # **模型做对了**（方法没定就先画图才是错的）。
        decisions = [payload for name, payload in frames if name == "decision_request"]
        if decisions:
            print(
                "\n⚠️ 蒟蒻在这一轮停下来问你了 —— 这本身是**对的**行为，"
                "但冒烟脚本没法替你拍板。\n"
            )
            for item in decisions:
                print(f"  问题：{item['question']}")
                for option in item["options"]:
                    print(f"    · {option}")
            print(
                "\n  两条路：\n"
                "    ① 去 http://localhost:3000/chat 打开那个会话，点一个选项，"
                "再看它接着画出来的图 —— 那是更真实的验收\n"
                "    ② 换一个**没有方法分岔口**的提示词重跑这个脚本，"
                "比如：\n"
                '       .venv/Scripts/python scripts/smoke_artifact.py '
                '"用 numpy 生成 200 个点的正弦波，画一张论文级插图"\n'
                "\n  ⚠️ 别去改 M5 的代码 —— 没产物不是 bug。"
            )
            return 0

        # ── 找出产物 ──
        #
        # ⚠️ 注意读的是 **tool_result 事件**而不是「扫磁盘」。这是一次端到端的
        #    检查：如果 refs 里没有产物，那么即使磁盘上真有文件，
        #    用户也是看不到的（界面上画的是 refs）。
        artifacts = [
            artifact
            for name, payload in frames
            if name == "tool_result"
            for artifact in payload.get("artifacts", [])
        ]

        if not artifacts:
            fail(
                "这一轮没有任何产物。\n"
                "可能的原因（按可能性排序）：\n"
                "  1. 模型没画图 —— 换个更明确要求出图的提示词再试\n"
                "  2. 工具描述里那段「出图用 mp.save()」没被模型采纳，"
                "它可能自己 plt.savefig() 到了工作目录根下（那样会被删掉）\n"
                "  3. 沙箱解释器不存在（后端日志里会有 setup_sandbox.py 的提示）"
            )

        print(f"\n✓ 这一轮产出了 {len(artifacts)} 个产物：")
        for item in artifacts:
            print(
                f"    {item['name']:<28} {item['kind']:<9} "
                f"{item['size']:>8} B  {item['mime']}"
            )

        # ── 从接口取回来 ──
        print("\n▶ 逐个下载：")
        for item in artifacts:
            url = f"{API_BASE}/api/artifacts/{session_id}/{item['id']}"
            got = client.get(url, params={"download": "1"})
            if got.status_code != 200:
                fail(f"下载 {item['name']} 失败：{got.status_code} {got.text[:200]}")

            disposition = got.headers.get("content-disposition", "")
            if "attachment" not in disposition:
                fail(f"{item['name']} 的响应里没有 attachment —— 浏览器会跳转而不是下载")

            # 文件名必须是**模型起的原名**，不是磁盘上那个 uuid
            if "filename*=utf-8''" in disposition:
                sent = unquote(disposition.split("utf-8''", 1)[1])
                if sent != item["name"]:
                    fail(f"下载文件名对不上：期望 {item['name']!r}，实际 {sent!r}")
            else:
                fail(f"中文文件名没有走 RFC 5987 编码：{disposition!r}")

            target = OUT_DIR / item["name"]
            target.write_bytes(got.content)
            print(f"    {item['name']:<28} {len(got.content):>8} B → {target}")

        # ── 文件头自检 ──
        #
        # 「下载到了 N 个字节」什么都说明不了：它完全可能是一个 JSON 错误体、
        # 一个空文件、或者一个 HTML 报错页。看一眼魔数才知道是不是真的图。
        print("\n▶ 文件头：")
        for item in artifacts:
            head = (OUT_DIR / item["name"]).read_bytes()[:8]
            if item["name"].lower().endswith(".png"):
                ok = head.startswith(b"\x89PNG\r\n\x1a\n")
                label = "PNG"
            elif item["name"].lower().endswith(".pdf"):
                ok = head.startswith(b"%PDF-")
                label = "PDF"
            elif item["name"].lower().endswith(".json"):
                ok = head.lstrip()[:1] in (b"{", b"[")
                label = "JSON"
            else:
                continue
            print(f"    {'✓' if ok else '✗'} {item['name']:<28} 看起来是 {label}")
            if not ok:
                fail(f"{item['name']} 的文件头不对 —— 它不是一个真的 {label}")

    print(
        f"\n✓ 产物链路通了。下载下来的文件在：\n    {OUT_DIR}\n"
        "\n"
        "⚠️ **最后一步要你自己看**：打开里面的 PNG，确认——\n"
        "    · 中文正常显示，没有方框（豆腐块）\n"
        "    · 坐标轴上的负号正常（不是方框）\n"
        "    · 没有顶部和右侧边框，网格是淡的，图例没有外框\n"
        "    · 多条曲线时，**灰度下**也能分开（线型和标记不同）\n"
        "      —— 把图导成灰度看一眼，数模论文经常黑白打印\n"
        "\n"
        f"清理：在 /chat 里删掉会话 {session_id[:8]}…，或者直接删那个目录。\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
