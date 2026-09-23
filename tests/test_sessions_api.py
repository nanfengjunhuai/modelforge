"""会话端点的端到端测试 —— 整个 M4 的验收就落在这一条主流程上。

    POST /api/sessions                        建会话
    POST /api/sessions/{id}/messages          说一句话 → 跑出决策点
    GET  /api/sessions/{id}                   **刷新页面**：拿回全部状态
    POST /api/sessions/{id}/decisions         拍板 → 接着跑完

中间那一步（GET）才是重点。整个 M4 存在的理由是「服务重启 / 刷新页面不丢进度」，
而这一条测试就是那句话的可执行版本：**每一步之后都去看一眼服务端记了什么**，
而不是信任前端内存里的状态。

模型和沙箱都换成假的，所以这里测的是**编排**：状态流转、租约、校验、
以及「恢复时发出去的历史长什么样」。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from modelforge.api import chat
from modelforge.main import create_app
from modelforge.providers.events import (
    Finish,
    ProviderEvent,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from tests import helpers

# ══════════════════════════════════════════════════════ 测试替身


class _ScriptedProvider:
    """跨请求按轮次吐事件的假 Provider。

    和 `test_agent_loop.py` 里那个的区别是**它的剧本跨越多个 HTTP 请求**：
    用户发一条消息消耗一轮，点一次决策卡片又消耗一轮。
    这正是 HITL 的形态 —— 一条「对话」被中断切成了好几次请求。
    """

    name = "scripted"

    def __init__(self, rounds: list[list[ProviderEvent]]) -> None:
        self._rounds = rounds
        self._index = 0
        # 每次调用都记下当时的完整历史 —— 这是断言「恢复时上下文对不对」的唯一办法
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def stream(self, messages: list[dict[str, Any]], **kwargs: Any):
        self.seen_messages.append([dict(m) for m in messages])
        events = self._rounds[self._index]
        self._index += 1
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


class _FakeExecutor:
    name = "fake"

    async def run(self, code: str, *, timeout: float | None = None):
        from modelforge.sandbox.base import ExecutionResult

        return ExecutionResult(stdout="5050\n", exit_code=0, duration_ms=3)


@pytest.fixture
def client() -> Any:
    # 用 with 而不是直接构造：这样会真正跑一遍 lifespan，
    # 包括启动时的「清空遗留租约」。
    with TestClient(create_app()) as c:
        yield c


def install(
    monkeypatch: pytest.MonkeyPatch, rounds: list[list[ProviderEvent]]
) -> _ScriptedProvider:
    provider = _ScriptedProvider(rounds)
    # `acquire_provider` 在调用时才从 chat 模块全局里查 get_provider 这个名字。
    monkeypatch.setattr(chat, "get_provider", lambda name=None, **_: provider)
    monkeypatch.setattr("modelforge.api.sessions.get_executor", lambda: _FakeExecutor())
    return provider


# ══════════════════════════════════════════════════════ 剧本


def ask_round(
    *, call_id: str = "c_ask", options: list[str] | None = None, allow_free_text: bool = True
) -> list[ProviderEvent]:
    arguments = json.dumps(
        {
            "question": "这一步用哪种赋权方法？",
            "options": options or ["熵权法（客观，但要求数据完整）", "AHP（能纳入主观判断）"],
            "allow_free_text": allow_free_text,
        },
        ensure_ascii=False,
    )
    return [
        TextDelta(text="蒟蒻看这一步有两个方向。"),
        ToolCallDelta(index=0, id=call_id, name="ask_user", arguments_delta=arguments),
        Usage(prompt_tokens=100, completion_tokens=30),
        Finish(reason="tool_calls"),
    ]


def answer_round(text: str = "那就用熵权法。") -> list[ProviderEvent]:
    return [
        TextDelta(text=text),
        Usage(prompt_tokens=150, completion_tokens=12),
        Finish(reason="stop"),
    ]


def new_session(client: TestClient) -> str:
    response = client.post("/api/sessions")
    assert response.status_code == 200, response.text
    return response.json()["id"]


# ══════════════════════════════════════════════════════ 主流程


def test_the_full_hitl_loop(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """**M4 的验收测试。**

    建会话 → 问一句 → 它停下来问 → 刷新页面看到卡片还在 → 拍板 → 它接着跑完。
    """
    provider = install(monkeypatch, [ask_round(), answer_round()])
    session_id = new_session(client)

    # ── 第一步：用户说一句话，Agent 跑到决策点停下 ──
    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "帮我做这道评价类题目"},
    )
    assert response.status_code == 200

    names = helpers.names_of(response.text)
    assert names[-1] == "finish"
    frames = helpers.frames_of(response.text)
    assert frames[-1][1]["reason"] == "awaiting_user"

    decision = next(data for name, data in frames if name == "decision_request")
    assert decision["call_id"] == "c_ask"
    assert decision["question"] == "这一步用哪种赋权方法？"
    assert len(decision["options"]) == 2

    # ── 第二步：**刷新页面** —— 服务端必须记着一切 ──
    detail = client.get(f"/api/sessions/{session_id}").json()

    assert detail["session"]["status"] == "awaiting_user"
    assert detail["session"]["title"] == "帮我做这道评价类题目"
    assert detail["session"]["busy"] is False  # 流已经结束了，租约还了

    kinds = [e["event"]["kind"] for e in detail["events"]]
    # 顺序说明了中断的机制：这一轮生成**先完整地跑完并落盘**（assistant + usage），
    # 然后循环才在处理工具调用的阶段发现「这次要问用户」，于是写下
    # decision_request 并结束。
    #
    # 换句话说，中断不是「把生成截断」，而是「生成完了，只是它要的不是工具结果
    # 而是人的答案」。这个区别决定了恢复的正确性 —— 被中断的那一轮在日志里
    # 是完整的。
    assert kinds == ["user", "assistant", "usage", "decision_request"]

    # 决策卡片还在，而且是「待答」状态
    assert len(detail["decisions"]) == 1
    assert detail["decisions"][0]["choice"] is None

    # ── 第三步：用户拍板 ──
    response = client.post(
        f"/api/sessions/{session_id}/decisions",
        json={
            "call_id": "c_ask",
            "choice": "熵权法（客观，但要求数据完整）",
            "note": "我们数据挺全的",
        },
    )
    assert response.status_code == 200

    frames = helpers.frames_of(response.text)
    assert frames[-1][1]["reason"] == "stop"
    assert "".join(d["text"] for n, d in frames if n == "text_delta") == "那就用熵权法。"

    # ── 第四步：**恢复时发出去的历史长什么样** ──
    #
    # 这一段是整条链路里最容易悄悄坏掉的地方：
    # 恢复走的是一次**全新的** run_agent_turn 调用，上下文完全靠投影重建。
    # 少一样东西都会出问题，而症状各不相同：
    #   · 少 system  → 人设消失（不再问问题、不用工具），界面上毫无异常
    #   · 少决策答案 → 模型不知道自己问的问题被回答了，会再问一遍
    #   · 坏 JSON   → 服务端直接 400
    resumed = provider.seen_messages[1]
    assert resumed[0]["role"] == "system"
    assert "蒟蒻" in resumed[0]["content"]
    assert resumed[1] == {"role": "user", "content": "帮我做这道评价类题目"}
    assert resumed[2]["role"] == "assistant"
    assert resumed[3]["role"] == "tool"
    assert resumed[3]["tool_call_id"] == "c_ask"
    assert "熵权法" in resumed[3]["content"]
    assert "我们数据挺全的" in resumed[3]["content"]  # 备注也要带上

    # ── 第五步：再刷新一次，已答的决策仍然在，状态回到 idle ──
    detail = client.get(f"/api/sessions/{session_id}").json()

    assert detail["session"]["status"] == "idle"
    assert len(detail["decisions"]) == 1
    assert detail["decisions"][0]["choice"] == "熵权法（客观，但要求数据完整）"
    # 最后那条回答**必须在日志里** —— 这是 M4 修掉的那个 bug
    kinds = [e["event"]["kind"] for e in detail["events"]]
    assert kinds.count("assistant") == 2
    assert "那就用熵权法。" in detail["events"][-2]["event"]["message"]["content"]


# ══════════════════════════════════════════════════════ 状态机


def test_sending_a_message_while_awaiting_a_decision_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """**有未答的决策时发新消息要被挡住 —— 否则下一次请求必然 400。**

    投影出来的历史里会有一个「有 tool_calls 却没有对应结果」的 assistant
    消息，多数服务直接拒绝。把门放在服务端，而不是靠前端禁用输入框：
    用户刷新页面、开第二个标签页、或者直接 curl 都能绕过去。
    """
    install(monkeypatch, [ask_round()])
    session_id = new_session(client)
    client.post(f"/api/sessions/{session_id}/messages", json={"content": "开始"})

    response = client.post(
        f"/api/sessions/{session_id}/messages", json={"content": "我再补充一句"}
    )

    assert response.status_code == 409
    assert "等你拍板" in response.json()["detail"]


def test_answering_a_decision_that_is_not_pending_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """call_id 对不上时给一句**能指导行动**的错。

    最常见的成因是页面上的卡片过期了（用户在另一个标签页已经答过），
    所以错误信息里要说「刷新一下」。
    """
    install(monkeypatch, [ask_round(call_id="c_real")])
    session_id = new_session(client)
    client.post(f"/api/sessions/{session_id}/messages", json={"content": "开始"})

    response = client.post(
        f"/api/sessions/{session_id}/decisions",
        json={"call_id": "c_stale", "choice": "A"},
    )

    assert response.status_code == 409
    assert "c_real" in response.json()["detail"]


def test_answering_with_nothing_pending_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    install(monkeypatch, [answer_round()])
    session_id = new_session(client)
    client.post(f"/api/sessions/{session_id}/messages", json={"content": "随便聊聊"})

    response = client.post(
        f"/api/sessions/{session_id}/decisions", json={"call_id": "c1", "choice": "A"}
    )

    assert response.status_code == 409
    assert "没有待回答的问题" in response.json()["detail"]


def test_free_text_can_be_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """`allow_free_text=False` 时，答案必须在选项里。

    这是**用户可控字符串进入模型上下文的唯一入口**，所以要有一道校验。
    允许自由作答时就不校验（用户完全可能知道模型没枚举到的信息）；
    不允许时必须命中，否则模型会收到一个它从没提供过的方案。
    """
    install(monkeypatch, [ask_round(options=["甲", "乙"], allow_free_text=False)])
    session_id = new_session(client)
    client.post(f"/api/sessions/{session_id}/messages", json={"content": "开始"})

    response = client.post(
        f"/api/sessions/{session_id}/decisions",
        json={"call_id": "c_ask", "choice": "我自己想的第三个方案"},
    )

    assert response.status_code == 422
    assert "不在里面" in response.json()["detail"]


def test_unknown_session_is_404(client: TestClient):
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.post("/api/sessions/nope/messages", json={"content": "在吗"}).status_code == 404
    assert client.delete("/api/sessions/nope").status_code == 404


def test_deleting_a_session_removes_it(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    install(monkeypatch, [])
    session_id = new_session(client)

    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_empty_message_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """空消息不该消耗一次模型调用。"""
    install(monkeypatch, [])
    session_id = new_session(client)

    response = client.post(f"/api/sessions/{session_id}/messages", json={"content": ""})

    assert response.status_code == 422


# ══════════════════════════════════════════════════════ 租约


def test_resume_does_not_append_anything(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """`/resume` 是 ADR-004 承诺的那条重试路径：**不追加事件**，只重开一条流。

    场景：用户点了决策卡片，答案已经落盘，但续流的请求在网络层抖了一下。
    此时决策已经答过了（再点会被「一个 call_id 只能回答一次」挡回来），
    没有这个端点的话，用户只能另发一条消息，而模型的历史里那条 tool 结果
    就永远没有后续了。

    它也是「事件日志 + 重建」相对「进程内挂起 Future」的核心优势之一 ——
    这种端点在任何时候都是免费的，因为发起一轮所需的一切都能从盘上重算。
    """
    install(monkeypatch, [answer_round("重试之后跑通了。"), answer_round("重试之后跑通了。")])
    session_id = new_session(client)
    client.post(f"/api/sessions/{session_id}/messages", json={"content": "算个和吧"})

    before = client.get(f"/api/sessions/{session_id}").json()["events"]

    response = client.post(f"/api/sessions/{session_id}/resume")
    assert response.status_code == 200
    assert helpers.names_of(response.text)[-1] == "finish"

    after = client.get(f"/api/sessions/{session_id}").json()["events"]

    # 事件数变多了（多了新一轮的 assistant + usage），但**没有多出 user 或决策**
    kinds_before = [e["event"]["kind"] for e in before]
    kinds_after = [e["event"]["kind"] for e in after]
    assert kinds_after[: len(kinds_before)] == kinds_before  # 旧事件原样保留
    assert kinds_after.count("user") == 1  # 没有凭空多出用户消息
    assert kinds_after.count("decision_answer") == 0


def test_a_second_stream_cannot_start_while_one_is_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """**并发保护的落点。**

    两条流同时写一个会话的日志，投影出来是**非法历史**（assistant 和它的
    tool 结果之间插进了别的东西），而且这种损坏是持久的 —— 那个会话
    再也发不出请求了。

    这里用「手工占住租约」来模拟「第一条流还在跑」。真实的并发路径
    不需要真的去跑两条流：这个端点看到的只是「租约抢不到」这一个事实。
    """
    install(monkeypatch, [answer_round()])
    session_id = new_session(client)

    hold_the_lease(session_id)

    response = client.post(f"/api/sessions/{session_id}/messages", json={"content": "在吗"})

    assert response.status_code == 409
    assert "正忙" in response.json()["detail"]


def hold_the_lease(session_id: str, *, seconds: float = 60) -> None:
    """在同步测试里抢一次租约。

    conftest 的 `isolated_session_store` 已经把 `sessions.get_store` 换成了
    一个指向 tmp_path 的实例，所以这里取到的就是服务端用的同一个存储。

    需要跑一次异步调用（存储接口是 async 的，因为实现内部要用
    `asyncio.to_thread` 把阻塞的 sqlite3 甩出去）。`anyio` 是 FastAPI 的
    依赖，用它起一个一次性事件循环最省事 —— 不用引入 pytest-asyncio，
    也不用把整个测试文件改成 async。
    """
    import anyio

    from modelforge.api import sessions as sessions_api

    async def go() -> None:
        taken = await sessions_api.get_store().acquire_lease(session_id, seconds=seconds)
        assert taken, "没能占住租约 —— 这个测试的前提就不成立了"

    anyio.run(go)
