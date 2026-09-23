"""报告端点的端到端测试。

    POST /api/sessions/{id}/report    生成报告（SSE）
    GET  /api/sessions/{id}           **刷新页面**：报告还在不在
    GET  /api/artifacts/{sid}/{aid}   点开能不能下载

中间那一步和 M4 的验收标准是同一条：**刷新不丢**。报告不是工具调用产出的，
所以它进日志走的是第八种事件（`LogReport`）—— 而「前端能不能看见它」
要跨两个文件（后端的 `list_artifacts` 和前端的 `allArtifacts`），
任何一处漏了都**不会让测试变红**。所以这里显式地走一遍刷新路径。

模型换成假的，所以测的是**编排**：SSE 事件顺序、租约、产物落地、
以及「生成报告不会污染对话历史」。
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from modelforge.api import chat, sessions
from modelforge.artifacts.base import ArtifactRef
from modelforge.main import create_app
from modelforge.providers.events import (
    ErrorEvent,
    Finish,
    ProviderEvent,
    TextDelta,
    ToolResult,
)
from modelforge.sessions.base import (
    LogAssistant,
    LogDecisionAnswer,
    LogDecisionRequest,
    LogTool,
    LogUser,
)
from modelforge.sessions.project import list_artifacts, list_reports
from tests import helpers


def _run(awaitable: Any) -> Any:
    """在一个一次性事件循环里跑一个协程。

    存储接口是 async 的（实现内部要用 `asyncio.to_thread` 甩开阻塞的
    sqlite3），而测试是同步的。`anyio` 是 FastAPI 的依赖，用它起一个临时
    循环最省事 —— `test_sessions_api.py` 里也是这么做的，不用引入
    pytest-asyncio，也不用把整个文件改成 async。
    """

    async def go() -> Any:
        return await awaitable

    return anyio.run(go)


# ══════════════════════════════════════════════════════ 测试替身


class _ScriptedProvider:
    """按调用次数吐剧本的假 Provider —— **一次调用 = 报告的一节**。

    和 `test_sessions_api.py` 里那个同形，但这里的「轮」是**报告章节**，
    不是对话轮次。所以剧本的长度决定了报告能写几节；
    不够长的话会抛 IndexError，而那个异常会被端点兜成一条 `report_error`。
    """

    name = "scripted"
    model = "scripted-v1"

    def __init__(self, sections: list[list[ProviderEvent]]) -> None:
        self._sections = sections
        self._index = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def stream(self, messages: list[dict[str, Any]], **kwargs: Any):
        self.seen_messages.append([dict(m) for m in messages])
        events = (
            self._sections[self._index]
            if self._index < len(self._sections)
            else [_text("（没有更多剧本了）"), Finish(reason="stop")]
        )
        self._index += 1
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


def _text(chunk: str) -> TextDelta:
    return TextDelta(text=chunk)


def _round(text: str) -> list[ProviderEvent]:
    return [_text(text), Finish(reason="stop")]


@pytest.fixture
def client() -> Any:
    with TestClient(create_app()) as c:
        yield c


def install(
    monkeypatch: pytest.MonkeyPatch, rounds: list[list[ProviderEvent]]
) -> _ScriptedProvider:
    provider = _ScriptedProvider(rounds)
    monkeypatch.setattr(chat, "get_provider", lambda name=None, **_: provider)
    return provider


# ══════════════════════════════════════════════════════ 造一个会话


async def _seed(store: Any, *, with_decision: bool = False) -> str:
    """直接往事件日志里写一段「已经发生过的事」。

    不跑真实的对话流程 —— 报告端点只读日志，把日志直接铺好更短也更清楚
    （走一遍对话反而会把「报告能不能读日志」这件事和「对话跑不跑得通」搅在一起）。
    """
    session = await store.create()
    await store.append(session.id, LogUser(content="用熵权法算这组数据的权重"))

    if with_decision:
        await store.append(
            session.id,
            LogDecisionRequest(
                call_id="c1", question="用哪种赋权方法？", options=["熵权法", "AHP"]
            ),
        )
        await store.append(
            session.id,
            LogDecisionAnswer(call_id="c1", choice="熵权法", note="数据都是全的"),
        )

    await store.append(
        session.id,
        LogAssistant(
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": "run_python",
                            "arguments": json.dumps({"code": "print('权重 0.42')"}),
                        },
                    }
                ],
            }
        ),
    )
    await store.append(
        session.id,
        LogTool(
            message={"role": "tool", "tool_call_id": "c2", "content": "权重 0.42"},
            result=ToolResult(
                call_id="c2",
                name="run_python",
                ok=True,
                stdout="权重 0.42",
                artifacts=[FIGURE],
            ),
        ),
    )
    return session.id


FIGURE = ArtifactRef(
    id="f" * 32, name="权重对比.png", kind="image", mime="image/png", size=10
)


# ══════════════════════════════════════════════════════ 主流程


def test_generating_a_report_streams_section_by_section(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """**本文件的主流程。** 报告应该一节一节流出来，而不是转圈等半天。

    断言的顺序本身就是产品要求：`report_start` 先把骨架（含程序块）发下去，
    然后每一节 `report_text` 流式吐正文 —— 用户能看到报告在长。
    骨架先发是为了让**图在正文还没写完时就显示出来**。
    """
    # 合成数据四样俱全（说过话、拍过板、跑过代码、出过图），所以是四节。
    provider = install(
        monkeypatch,
        [
            _round("第一节的正文。"),
            _round("第二节的正文。"),
            _round("第三节的正文。"),
            _round("第四节的正文。"),
        ],
    )
    store = sessions.get_store()
    session_id = _run(_seed(store, with_decision=True))

    response = client.post(f"/api/sessions/{session_id}/report")
    assert response.status_code == 200
    frames = helpers.frames_of(response.text)
    names = [name for name, _ in frames]

    assert names[0] == "report_start"
    assert names[-1] == "report_done"
    assert names.count("report_section_done") == 4
    assert set(names) <= {
        "report_start",
        "report_text",
        "report_section_done",
        "report_done",
        "report_error",
    }, "报告流出现了不该有的事件名（是不是和聊天那套混了？）"

    start = frames[0][1]
    assert [s["title"] for s in start["sections"]] == [
        "一、问题重述",
        "二、模型假设与方案选择",
        "三、模型的建立与求解",
        "四、结论",
    ], "合成数据里有决策记录，第二章不该被丢掉"

    # 骨架里就应该带着程序块 —— 图不必等正文写完才出现
    kinds = {b["kind"] for s in start["sections"] for b in s["blocks"]}
    assert {"decision", "code", "figure"} <= kinds

    assert provider.seen_messages, "模型一次都没被调用？"
    assert "蒟蒻" not in provider.seen_messages[0][0]["content"], (
        "报告用了对话的人设提示词 —— 交上去的论文里不能出现「蒟蒻」（ADR-017）"
    )


def test_the_report_survives_a_page_refresh(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """**这一条是本轮的「刷新不丢」验收。**

    报告不是工具调用产出的，所以它的产物引用进日志走的是第八种事件。
    前端重建界面的两条路都要能看见它：

        GET /api/sessions/{id}      → `reports` 字段（后端投影好的）
        list_artifacts(events)      → 产物清单（面板按它渲染）

    第二条容易被漏 —— `list_artifacts` 原本只收 `LogTool.result.artifacts`。
    漏了的话，报告在磁盘上、`reports` 里也有，但**产物面板里看不到**，
    而 pytest 全绿（前端那条路径是 TS 的）。
    """
    install(monkeypatch, [_round("正文一。"), _round("正文二。"), _round("正文三。")])
    store = sessions.get_store()
    session_id = _run(_seed(store))

    client.post(f"/api/sessions/{session_id}/report")

    # ① 会话详情里的 reports
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert len(detail["reports"]) == 1, "刷新之后报告不在 reports 里"
    report = detail["reports"][0]
    assert report["document"]["name"].endswith(".md")
    assert report["sidecar"]["name"].endswith(".report.json")
    assert report["provider"] == "scripted"
    assert report["model"] == "scripted-v1"

    # ② 产物清单（前端 allArtifacts 的 Python 对应物）
    events = _run(store.events(session_id))
    names = {ref.name for ref in list_artifacts(events)}
    assert any(name.endswith(".md") for name in names), (
        "报告没有出现在产物清单里 —— list_artifacts 忘了收 LogReport"
    )

    # ③ 两个文件都真的能下载
    for ref in (report["document"], report["sidecar"]):
        got = client.get(f"/api/artifacts/{session_id}/{ref['id']}")
        assert got.status_code == 200, f"产物取不到：{ref['name']}"
        assert len(got.content) == ref["size"], (
            "日志里记的大小和实际文件对不上 —— 多半是跨盘搬迁失败了一半"
        )

    # ④ sidecar 是完整的 JSON 文档（不是半截）
    sidecar_id = report["sidecar"]["id"]
    doc = json.loads(client.get(f"/api/artifacts/{session_id}/{sidecar_id}").content)
    assert doc["session_id"] == session_id
    assert [s["title"] for s in doc["sections"]]
    assert doc["version"] >= 1


def test_the_markdown_artifact_is_actually_markdown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """下载下来的 `.md` 要是能直接打开的中文 Markdown。

    这条守的是编码：写文件时忘了 `encoding="utf-8"` 的话，
    Windows 上会用 GBK —— 而 JSON 那边（`model_dump_json` 返回 str）
    不受影响。于是 sidecar 好好的、`.md` 打开是乱码，
    而**两边用的是同一次渲染的结果**，看不出任何异常。
    """
    install(monkeypatch, [_round("正文一。"), _round("正文二。"), _round("正文三。")])
    store = sessions.get_store()
    session_id = _run(_seed(store))
    client.post(f"/api/sessions/{session_id}/report")

    report = client.get(f"/api/sessions/{session_id}").json()["reports"][0]
    body = client.get(f"/api/artifacts/{session_id}/{report['document']['id']}").content
    text = body.decode("utf-8")

    assert "## 一、问题重述" in text
    assert "正文一。" in text


def test_generating_a_report_does_not_touch_the_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """**报告生成不该在对话历史上留任何痕迹。**

    这条盯的是一个真实的坑：`sessions._stream_response`（对话用的那个）
    在 `CancelledError` 里会 append 一条 `LogAborted(reason="客户端断开连接")`。
    报告端点如果图省事复用它，用户取消一次报告生成，
    **对话日志里就会多一条「这轮对话被中断了」的假记录**，而前端会渲染它。

    这里测的是正常路径（没有异常、没有取消），断言的是更基本的那条性质：
    报告只读日志、只追加自己的那一条事件。
    """
    install(monkeypatch, [_round("正文一。"), _round("正文二。"), _round("正文三。")])
    store = sessions.get_store()
    session_id = _run(_seed(store))
    before = [item.event.kind for item in _run(store.events(session_id))]

    client.post(f"/api/sessions/{session_id}/report")

    after = [item.event.kind for item in _run(store.events(session_id))]
    assert after == [*before, "report"], (
        "生成报告改变了对话历史 —— 除了那条 report，不该有任何别的记录"
    )


def test_a_section_that_fails_does_not_kill_the_whole_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """一节的模型调用失败，其余几节照旧成稿，**而且这件事会被说出来**。

    静默地少一节是最坏的处理方式：报告读起来完整，只是内容少了一块，
    而用户没有任何线索。所以断言两件事 —— 报告仍然生成成功，
    以及那一条说明确实出现在文档的 notes 里。
    """
    # 这个会话没有决策记录，所以是三节：问题重述 / 建立与求解 / 结论。
    # 让**第二节**（建立与求解）失败。
    install(
        monkeypatch,
        [
            _round("正文一。"),
            [ErrorEvent(message="模型服务返回了 503"), Finish(reason="error")],
            _round("正文三。"),
        ],
    )
    store = sessions.get_store()
    session_id = _run(_seed(store))
    response = client.post(f"/api/sessions/{session_id}/report")
    frames = helpers.frames_of(response.text)

    done = [data for name, data in frames if name == "report_done"]
    assert done, "一节失败不该让整份报告生成不出来"

    notes = done[0]["document"]["notes"]
    assert any("模型的建立与求解" in note and "503" in note for note in notes), notes

    # 失败的那一节：正文块**不能**是那半截文字
    failed = next(
        s for s in done[0]["document"]["sections"] if "建立与求解" in s["title"]
    )
    assert not any(b["kind"] == "prose" for b in failed["blocks"]), (
        "把失败那节的半截文字当正文用了 —— 半截段落读起来是完整的，最误导人"
    )
    # 但它的程序块要留着：代码和图本来就是好的，跟着一节正文一起丢掉才是浪费
    assert any(b["kind"] == "code" for b in failed["blocks"])


def test_a_session_with_nothing_to_report_is_a_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """空会话返回 409，**并且解释为什么**。

    409 而不是 400：这不是请求有问题，是当前状态做不了这件事。
    而且这里必须**在流开始之前**把话说清楚 —— 出了第一个字节就只能走带内错误帧，
    前端要额外写一条分支去处理一个本该是 HTTP 状态码的事情。
    """
    install(monkeypatch, [])
    created = client.post("/api/sessions").json()

    response = client.post(f"/api/sessions/{created['id']}/report")
    assert response.status_code == 409
    assert "还没有" in response.json()["detail"]


def test_an_unknown_session_is_a_404(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    install(monkeypatch, [])
    assert client.post("/api/sessions/nope/report").status_code == 404


def test_the_lease_is_released_even_when_the_model_explodes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """跑完之后租约必须还回去，否则这个会话要卡到租约过期。

    和别的流式端点同一条性质，但报告这边尤其要紧：它是**多次模型调用**，
    比一次对话慢得多，卡住的时间也就更长。
    """
    install(
        monkeypatch,
        [
            [ErrorEvent(message="炸了"), Finish(reason="error")],
        ],
    )
    store = sessions.get_store()
    session_id = _run(_seed(store))

    client.post(f"/api/sessions/{session_id}/report")

    again = _run(store.acquire_lease(session_id, seconds=60))
    assert again is not None, "报告跑完之后租约没还 —— 这个会话被卡住了"
    _run(store.release_lease(session_id, token=again))


def test_a_busy_session_is_a_409(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """正忙的会话拒绝生成报告。

    报告是「对整份日志的长时间独占读」—— 一边生成一边有对话在追加事件，
    写出来的正文会基于一个**从未存在过的历史**。理由和 `/resume` 一样。
    """
    install(monkeypatch, [_round("正文。")])
    store = sessions.get_store()
    session_id = _run(_seed(store))
    held = _run(store.acquire_lease(session_id, seconds=60))
    assert held is not None

    assert client.post(f"/api/sessions/{session_id}/report").status_code == 409


def test_reports_are_projected_into_the_session_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """`SessionDetail.reports` 是从日志投影出来的，不是存的第二份。

    和 `decisions` 同一个道理：真相只有事件日志一份，这个字段是给前端用的**视图**。
    所以它必须和 `list_reports` 的结果一致。
    """
    install(monkeypatch, [_round("正文一。"), _round("正文二。"), _round("正文三。")])
    store = sessions.get_store()
    session_id = _run(_seed(store))
    assert client.get(f"/api/sessions/{session_id}").json()["reports"] == []

    client.post(f"/api/sessions/{session_id}/report")

    detail = client.get(f"/api/sessions/{session_id}").json()
    events = _run(store.events(session_id))
    assert [r["title"] for r in detail["reports"]] == [
        r.title for r in list_reports(events)
    ]
