"""产物端点的端到端测试 —— M5 的验收落在这一条主流程上。

    跑一轮带工具调用的对话 → 模型产出一张图
    → 刷新（GET 会话详情）看产物引用还在不在
    → 从产物端点把图取回来
    → 删会话 → 图和它的引用一起消失

这里的假执行器**不产出假的产物** —— 它真的往（隔离出来的）产物存储里
写文件。因为这条链路上最容易出问题的部分恰恰是「真的落盘了没有」：
M3 的教训是 TestClient 会把响应体收完再给你，而单元测试永远测不出
「帧是不是随时间陆续到达的」。这里是同理 —— **只断言 refs 里有东西，
证明不了那张图真的存在、真的能取回来。**

════════════════════════════════════════════════════════════════════════
这条测试顺带守住的一件要紧事
════════════════════════════════════════════════════════════════════════
`client` 夹具会**真的跑一遍 lifespan**，而 lifespan 启动时会调
`sweep_orphans()` —— 它删除「不属于任何现存会话」的产物目录。

在测试里会话存储是空的，所以**没有 conftest 那个产物隔离夹具的话，
一次测试运行会把用户真实的图全部当成孤儿删掉**。
这个文件是那条路径最可能被走到的入口（因为它真的建会话、真的产图），
所以它也是那个夹具的活体验收。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from modelforge.api import chat
from modelforge.artifacts.base import ArtifactStore, SandboxArtifact
from modelforge.main import create_app
from modelforge.providers.events import (
    Finish,
    ProviderEvent,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from modelforge.sandbox.base import ExecutionResult
from tests import helpers

# 一张 1x1 的真 PNG。用真的而不是随便几个字节，是因为下载端点会
# 按扩展名给 MIME、按文件头给浏览器判断 —— 拿假字节测不出这些。
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ══════════════════════════════════════════════════════ 测试替身


class _ScriptedProvider:
    """按轮次吐事件的假 Provider。"""

    name = "scripted"

    def __init__(self, rounds: list[list[ProviderEvent]]) -> None:
        self._rounds = rounds
        self._index = 0

    async def stream(self, messages: list[dict[str, Any]], **kwargs: Any):
        events = self._rounds[min(self._index, len(self._rounds) - 1)]
        self._index += 1
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


class _PlottingExecutor:
    """假执行器，但它产出**真的**产物。

    真的写文件、真的走 `ArtifactStore.ingest()`、真的落盘 ——
    这样后面那个「下载回来」的断言才有意义。
    """

    name = "fake-plot"

    def __init__(self, store: ArtifactStore, staging: Path) -> None:
        self._store = store
        self._staging = staging
        self.seen_scopes: list[str | None] = []

    async def run(
        self, code: str, *, timeout: float | None = None, scope: str | None = None
    ) -> ExecutionResult:
        # 记下 scope —— 「会话有没有把归属传下来」只能从这里看出来。
        # 不传 scope 的实现跑起来和正确的**一模一样**，
        # 区别只在于产物最后没有归宿（图会随着工作目录被删掉）。
        self.seen_scopes.append(scope)

        source = self._staging / "权重对比.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(TINY_PNG)

        artifacts = []
        if scope is not None:
            artifacts = await self._store.ingest(
                scope,
                [
                    SandboxArtifact(
                        name="权重对比.png",
                        path=source,
                        size=len(TINY_PNG),
                        mime="image/png",
                    )
                ],
            )

        return ExecutionResult(
            stdout="[已保存产物] 权重对比\n",
            exit_code=0,
            duration_ms=7,
            artifacts=artifacts,
        )


def plot_round(call_id: str = "c_plot") -> list[ProviderEvent]:
    arguments = json.dumps({"code": "mp.save(fig, '权重对比')"}, ensure_ascii=False)
    return [
        TextDelta(text="蒟蒻把图存下来了。"),
        ToolCallDelta(index=0, id=call_id, name="run_python", arguments_delta=arguments),
        Usage(prompt_tokens=120, completion_tokens=25),
        Finish(reason="tool_calls"),
    ]


def wrap_up_round(text: str = "画好了，你可以点开看。") -> list[ProviderEvent]:
    return [
        TextDelta(text=text),
        Usage(prompt_tokens=200, completion_tokens=15),
        Finish(reason="stop"),
    ]


@pytest.fixture
def client() -> Any:
    # 用 with 而不是直接构造：这样会真正跑一遍 lifespan，
    # 包括启动时那两段清理（清租约 + 清孤儿产物）。
    with TestClient(create_app()) as c:
        yield c


def install(
    monkeypatch: pytest.MonkeyPatch,
    rounds: list[list[ProviderEvent]],
    store: ArtifactStore,
    tmp_path: Path,
) -> _PlottingExecutor:
    provider = _ScriptedProvider(rounds)
    executor = _PlottingExecutor(store, staging=tmp_path / "staging")
    monkeypatch.setattr(chat, "get_provider", lambda name=None, **_: provider)

    # ⚠️ **两个模块都要 patch**，因为它们各自 import 了一份 `get_executor`：
    #
    #     有状态会话端点  api/sessions.py  → sessions.get_executor()
    #     无状态调试端点  api/chat.py      → chat.get_executor()
    #
    # 两个模块里写的都是「运行时查模块属性」（`get_executor()` 而不是
    # 把函数对象绑死），所以换掉模块属性就能生效。但只换一个的话，
    # 另一个端点会去起一个**真的沙箱**——在这条测试里表现为
    # 「执行器一次都没被调用」，而不是报错。
    monkeypatch.setattr("modelforge.api.sessions.get_executor", lambda: executor)
    monkeypatch.setattr(chat, "get_executor", lambda: executor)
    return executor


def produce_one_artifact(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    store: ArtifactStore,
    tmp_path: Path,
) -> tuple[str, str]:
    """跑一轮完整对话，返回 `(会话 id, 产物 id)`。"""
    install(monkeypatch, [plot_round(), wrap_up_round()], store, tmp_path)

    session_id = client.post("/api/sessions").json()["id"]
    response = client.post(
        f"/api/sessions/{session_id}/messages", json={"content": "帮我画个权重对比图"}
    )
    assert response.status_code == 200, response.text

    results = [
        payload
        for name, payload in helpers.frames_of(response.text)
        if name == "tool_result"
    ]
    assert results, f"流里没有 tool_result：{helpers.names_of(response.text)}"
    artifacts = results[0]["artifacts"]
    assert len(artifacts) == 1

    return session_id, artifacts[0]["id"]


# ══════════════════════════════════════════════════════ 主流程


def test_the_full_artifact_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, isolated_artifact_store, tmp_path: Path
):
    """**M5 的验收测试。**

    产出 → 引用进了事件流 → 刷新后还在 → 能从接口取回来 → 删会话一起消失。
    """
    session_id, artifact_id = produce_one_artifact(
        client, monkeypatch, isolated_artifact_store, tmp_path
    )
    url = f"/api/artifacts/{session_id}/{artifact_id}"

    # ── ① 流里带着产物引用 ──
    # （`produce_one_artifact` 已经断言过，这里只说明它验过了）

    # ── ② **刷新页面**：产物引用从事件日志里读回来 ──
    #
    # 这一步是 M4 那条「刷新不丢」在 M5 的延续。产物**没有单独存一份表**，
    # 它的元数据跟着 `tool` 事件的 `result` 一起进了日志 ——
    # 所以这里断言「详情里带着它」就等价于断言「刷新之后图还在」。
    detail = client.get(f"/api/sessions/{session_id}").json()
    tool_events = [e for e in detail["events"] if e["event"]["kind"] == "tool"]
    replayed = tool_events[0]["event"]["result"]["artifacts"]

    assert [a["id"] for a in replayed] == [artifact_id]
    assert replayed[0]["name"] == "权重对比.png"
    assert replayed[0]["kind"] == "image"

    # ── ③ 取回来 ──
    response = client.get(url)

    assert response.status_code == 200
    assert response.content == TINY_PNG
    assert response.headers["content-type"] == "image/png"
    # 内联时**不能**给 Content-Disposition: attachment，否则 <img> 显示不出来
    assert "attachment" not in response.headers.get("content-disposition", "")
    assert response.headers["x-content-type-options"] == "nosniff"

    # ── ④ 删会话 → 产物一起没 ──
    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get(url).status_code == 404
    assert not (
        isolated_artifact_store.root / session_id  # type: ignore[attr-defined]
    ).exists()


def test_download_uses_the_original_chinese_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, isolated_artifact_store, tmp_path: Path
):
    """下载时的文件名必须是**模型起的那个名字**，不是磁盘上的 uuid。

    ════════════════════════════════════════════════════════════════
    两个容易踩的点，这条测试都盯着
    ════════════════════════════════════════════════════════════════

    ① **`<a download>` 属性在跨域时会被浏览器忽略。**
       前端跑在 :3000、后端在 :8000，所以「点了链接要下载而不是跳转」
       这件事**只能靠服务端的 `Content-Disposition`**。
       这条测试直接断言那个头在不在 —— 因为前端那边的属性是个摆设，
       删了它测试也不会红，而删了这个头用户就会看到图片在新标签页里打开。

    ② **中文文件名要走 RFC 5987 的 `filename*=utf-8''…` 形式。**
       不编码的话，响应头里出现非 ASCII 字节，有些浏览器会拿到乱码文件名，
       更糟的情况是头解析出错。好在 `FileResponse` 会自动处理，
       但「它会自动处理」这件事本身需要一条测试来固定住。

    ③（顺带）**文件名是从日志里查出来的**，不是从磁盘文件名推的。
       磁盘上那个叫 `3f2a…c1.png`。查日志这一步失败时会降级用磁盘名 ——
       那是个可接受的降级，但绝不能是默认行为。
    """
    session_id, artifact_id = produce_one_artifact(
        client, monkeypatch, isolated_artifact_store, tmp_path
    )

    response = client.get(f"/api/artifacts/{session_id}/{artifact_id}?download=1")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=utf-8''" in disposition, disposition
    # 把百分号编码解回去，确认拿到的是中文原名而不是 uuid
    assert unquote(disposition.split("utf-8''", 1)[1]) == "权重对比.png"


def test_the_session_id_is_passed_down_as_the_scope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, isolated_artifact_store, tmp_path: Path
):
    """**回归测试：会话有没有把归属传给执行器。**

    这个 bug 的症状很隐蔽：不传 scope 的实现跑起来和正确的**一模一样** ——
    代码照跑、stdout 照回、对话照继续。唯一的差别是产物落在工作目录里，
    而那个目录在执行结束时就删了，所以用户永远看不到图，
    也没有任何报错。

    真正会走到这条路径的只有 `api/sessions.py` 里那一行 `scope=session_id`。
    有人重构 `_stream_response` 时把它漏掉，这条会红。
    """
    executor = install(
        monkeypatch,
        [plot_round(), wrap_up_round()],
        isolated_artifact_store,
        tmp_path,
    )

    session_id = client.post("/api/sessions").json()["id"]
    client.post(f"/api/sessions/{session_id}/messages", json={"content": "画图"})

    assert executor.seen_scopes == [session_id]


def test_the_stateless_endpoint_leaves_no_artifacts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, isolated_artifact_store, tmp_path: Path
):
    """无状态端点不保留产物 —— 行为与 M3 完全一致。

    `/api/chat/stream` 没有会话、没有事件日志、界面上也没有地方展示产物，
    所以给它保留文件只会留下一堆没人认领的东西（然后每次启动被
    `sweep_orphans` 收走）。让它照旧跑完即删，少一条需要理解的分支。
    """
    executor = install(
        monkeypatch,
        [plot_round(), wrap_up_round()],
        isolated_artifact_store,
        tmp_path,
    )

    response = client.post(
        "/api/chat/stream", json={"messages": [{"role": "user", "content": "画图"}]}
    )

    assert response.status_code == 200
    assert executor.seen_scopes == [None]
    # 产物一个都没留下 —— 连存储根目录都不该被建出来
    assert list(isolated_artifact_store.root.glob("**/*")) == []
    assert list((tmp_path / "staging").glob("*.png")) == [
        tmp_path / "staging" / "权重对比.png"
    ], "执行器确实造了文件，只是没有被收走"


@pytest.mark.parametrize(
    "session_id,artifact_id",
    [
        ("不存在", "a" * 32),
        ("s" * 32, "不存在"),
        ("../../etc", "a" * 32),
        ("s" * 32, "../../../windows/win.ini"),
    ],
)
def test_bad_ids_are_404_not_500(
    client: TestClient, session_id: str, artifact_id: str
) -> None:
    """不合法的 id 是 404，不是 500，更不是「读到了别的文件」。

    这一层不做「这个会话存不存在」的校验（那是一次多余的数据库读）——
    能不能取到完全由 `ArtifactStore.path_for()` 的形态校验决定，
    而它在任何不合法的输入上都返回 None。
    """
    assert client.get(f"/api/artifacts/{session_id}/{artifact_id}").status_code == 404


def test_a_missing_artifact_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, isolated_artifact_store, tmp_path: Path
):
    """文件被手动删掉时也要是 404 而不是崩掉。

    这在实际中会发生：用户去 `%LOCALAPPDATA%\\modelforge\\artifacts` 里
    手动清了一次空间。那时候日志里还记着那个产物，界面上也还画着缩略图 ——
    点下去必须是「找不到」，而不是一个 500 堆栈。
    """
    session_id, artifact_id = produce_one_artifact(
        client, monkeypatch, isolated_artifact_store, tmp_path
    )
    scope_dir = isolated_artifact_store.root / session_id  # type: ignore[attr-defined]
    for path in scope_dir.iterdir():
        path.unlink()

    assert client.get(f"/api/artifacts/{session_id}/{artifact_id}").status_code == 404
