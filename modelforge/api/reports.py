"""报告端点 —— 把一次会话变成一篇能交上去的论文草稿。

════════════════════════════════════════════════════════════════════════
为什么是「一个端点」而不是「一个工具」
════════════════════════════════════════════════════════════════════════

`ask_user` / `run_python` 都是工具，为什么「写报告」不是？

因为**它们读的东西不一样**：

    工具      读模型的上下文 —— 这一轮它看得见什么
    报告端点  读**整个会话的事件日志**

报告要的是「这次建模从头到尾发生了什么」，而模型的上下文里没有这个：
对话可能几十轮，早期的东西早就被挤出去了；更关键的是，模型看不见
用户每次拍板时的**结构化记录**（候选、选择、备注都在日志里，不在消息里）。

所以报告是从**落盘的真相**里投影出来的，不是模型回忆出来的。
这也是为什么它不需要 Agent 循环：没有工具调用、没有多轮、没有中断 ——
就是「读日志 → 逐节写 → 存下来」。

════════════════════════════════════════════════════════════════════════
⚠️ 这个流有自己的事件词汇，**不要复用聊天的
════════════════════════════════════════════════════════════════════════

`chat.format_sse` 直接拿 `event.type` 当 SSE 的 `event:` 字段，而聊天那边
已经有 `error` / `finish` 这些名字。报告流如果也叫 `error`，
任何共用一个读取方的代码都会把两个流的错误混到一起。

所以这里的六个事件统一带 `report_` 前缀，前端用另一套类型处理
（帧解析复用 `web/src/lib/sse.ts`，那是通用的）。
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

# 和 api/artifacts.py / api/sessions.py 同一条规矩：取存储要**运行时查
# 模块属性**，否则 tests/conftest.py 的 monkeypatch 罩不住这里，
# 测试会往真实的用户目录里写文件、读真实数据库。
#
# ⚠️ 这条对 `sessions_api.get_store` **同样适用**，而且 M6a 真的踩了一次：
# 写成 `from modelforge.api.sessions import get_store` 之后，路由全部返回 404
# —— 因为 conftest 换掉的是 `sessions.get_store` 这个**模块属性**，
# 而上面那种写法在导入时就把原来的函数对象绑进了本模块，换不掉。
# 测试于是拿到了真实数据库（空），表现为「刚建的会话，报告端点说没有」。
from modelforge import artifacts as artifact_store_pkg
from modelforge.api import sessions as sessions_api
from modelforge.api.chat import SSE_HEADERS, acquire_provider
from modelforge.artifacts.base import ArtifactRef, SandboxArtifact, guess_mime
from modelforge.providers.base import ChatProvider, Message
from modelforge.providers.events import ErrorEvent, Finish, TextDelta
from modelforge.reports import (
    REPORT_SYSTEM_PROMPT,
    ReportDocument,
    ReportOutline,
    build_outline,
    name_key,
    render_document,
    render_markdown,
    section_message,
)
from modelforge.sessions.base import LogReport, SessionStore
from modelforge.sessions.project import list_artifacts

logger = logging.getLogger(__name__)

__all__ = ["router"]

# 前缀和 sessions 那边一样 —— 路由本来就是这个会话的一个动作，
# 单独成模块只是因为那边已经五百多行了（和 api/artifacts.py 同一条理由）。
router = APIRouter(prefix="/sessions", tags=["reports"])

REPORT_TEMPERATURE = 0.3
"""报告正文的采样温度。

比对话（0.2）略高一点：报告里需要一点措辞上的变化，不像求解那样要稳。
但也不能高 —— 数模论文的正文最怕「发挥」。
"""

_CHART_SUFFIX = ".chart.json"


def _sse(name: str, payload: Any) -> str:
    """报告流的 SSE 帧。

    和 `chat.format_sse` 是同一个帧格式，只是载荷不是 Pydantic 事件
    （这里的事件是本地的小结构，不值得为它们各建一个模型）。

    `ensure_ascii=False` 是**故意的**：报告正文全是中文，转义成 `\\uXXXX`
    之后每一帧会涨到六倍大。聊天那边用的是 `model_dump_json()`，
    Pydantic 默认也是不转义，两边行为一致。
    """
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ══════════════════════════════════════════════════════ 图说


async def _collect_captions(
    session_id: str, items: Sequence[Any]
) -> dict[str, str]:
    """把 `.chart.json` 里的 `caption` 读出来，按**分组键**建一张表。

    为什么要读盘：caption 不在事件日志里，它在产物文件里（`mp.save` 写
    `.chart.json` 时顺手存进去的那句话）。这是整条报告链路里**唯一**一处
    需要碰产物文件的地方 —— 所以它被特意挤到这个函数里，让上面的投影层
    保持纯函数。

    ⚠️ 失败一律吞掉（记一条 debug 日志）。少一句图说是小事，
    因为读不到一个文件就让整份报告生成不出来才是大事。
    """
    store = artifact_store_pkg.get_store()
    captions: dict[str, str] = {}

    for ref in list_artifacts(items):
        if not ref.name.lower().endswith(_CHART_SUFFIX):
            continue
        try:
            path = await store.path_for(session_id, ref.id)
            if path is None:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            caption = payload.get("caption") if isinstance(payload, dict) else None
        except (OSError, ValueError) as exc:
            logger.debug("读不到 %s 的 caption：%s", ref.name, exc)
            continue
        if isinstance(caption, str) and caption.strip():
            captions[name_key(ref.name)] = caption.strip()

    return captions


# ══════════════════════════════════════════════════════ 模型调用


async def _write_one_section(
    provider: ChatProvider, plan_title: str, brief: str
) -> AsyncIterator[tuple[str, str]]:
    """写一节正文，逐段吐出来。

    yield 的是 `(类型, 内容)`：

        ("delta", 片段)   正文的一个碎片，直接转给前端
        ("error", 消息)   这一节失败了 —— 调用方**不要**用已经收到的碎片

    ⚠️ **不能复用 `agents/loop.py` 的 `_stream_round`**。它要一个私有的
    `_RoundState`，而且**故意吞掉 `Finish`**（那是为「整条流恰好一个 Finish」
    设计的），调用方拿不到结束原因 —— 而报告正需要它来分辨
    「正常结束」和「被 max_tokens 截断」。十行代码，自己写更清楚。
    """
    messages: list[Message] = [
        {"role": "system", "content": REPORT_SYSTEM_PROMPT},
        {"role": "user", "content": section_message(brief, title=plan_title)},
    ]

    async for event in provider.stream(
        messages, tools=None, temperature=REPORT_TEMPERATURE
    ):
        if isinstance(event, TextDelta):
            yield ("delta", event.text)
        elif isinstance(event, ErrorEvent):
            # Provider 的契约是「出错不抛异常，yield 一个 ErrorEvent」，
            # 紧接着会有一个 Finish(reason="error")。这里如实转达，
            # 由调用方决定这一节作废。
            yield ("error", event.message)
        elif isinstance(event, Finish) and event.reason == "length":
            yield ("error", "正文被输出上限截断了")


# ══════════════════════════════════════════════════════ 落地


async def _persist(
    scope: str, title: str, document: ReportDocument, markdown: str
) -> tuple[ArtifactRef, ArtifactRef] | None:
    """把报告的两种视图存进产物库。**成功时返回两个引用，失败返回 None。**

    ⚠️ 三处刻意写死的地方，每一处都对应一个会静默出错的坑：

    **① 临时文件名是字面量，不是标题。**
    `report.md` / `report.report.json` 而不是 `<标题>.md` —— 因为标题是
    用户可控的（第一条消息的前 30 个字），拿它拼路径就是路径穿越。
    这和 `artifacts/base.py` 里那句「不去过滤这些名字，而是**根本不用它们
    当文件名**」是同一条规矩。标题只作为**显示名**交给 `ingest`，
    它从头到尾没有碰过文件系统。

    **② 临时目录在项目之外。**
    `tempfile.mkdtemp` 落系统临时目录。写进项目里的话，uvicorn 的
    `--reload` 会在报告写到一半时热重载，而重载信号会打断正在跑的请求
    （M3/M5 都踩过这个，形态一模一样）。

    **③ 必须确认两个文件都搬进去了。**
    `LocalArtifactStore._move_one` 遇到 `OSError` 会**返回 None 然后跳过**
    —— 而产物目录默认在 `%LOCALAPPDATA%`，和系统临时目录**很可能不在
    同一个盘**，跨盘 `shutil.move` 失败是真实存在的。不检查的话，
    我们会往日志里写一条指向不存在的产物的 `LogReport`，
    而日志是 append-only，那条错误记录**永远修不掉**。
    """
    work = Path(tempfile.mkdtemp(prefix="modelforge-report-"))
    doc_path = work / "report.md"
    json_path = work / "report.report.json"
    doc_name = f"{title or '报告'}.md"
    json_name = f"{title or '报告'}.report.json"

    try:
        doc_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(
            document.model_dump_json(indent=2), encoding="utf-8"
        )
    except OSError:
        logger.exception("报告临时文件写不出来")
        return None

    refs = await artifact_store_pkg.get_store().ingest(
        scope,
        [
            SandboxArtifact(
                name=doc_name,
                path=doc_path,
                size=doc_path.stat().st_size,
                mime=guess_mime(doc_name),
            ),
            SandboxArtifact(
                name=json_name,
                path=json_path,
                size=json_path.stat().st_size,
                mime=guess_mime(json_name),
            ),
        ],
    )

    if len(refs) != 2:
        # 不抛异常、不写日志 —— 调用方会把它转成一条带内错误帧。
        # 这里只留一条服务端日志，因为原因是环境问题（跨盘、磁盘满），
        # 用户看错误帧就够了。
        logger.error("报告产物只搬进去 %d/2 个，放弃写日志记录", len(refs))
        return None
    return refs[0], refs[1]


def _provider_model(provider: ChatProvider) -> str:
    """尽力取一下模型名。取不到就是空串。

    `ChatProvider` 协议里**没有** `model` 这个属性（协议只承诺「能流式生成」），
    所以这里用 `getattr` 兜底而不是把它加进协议：加进去意味着每个实现都要
    有它，而 Ollama 那边可能压根不关心自己叫什么模型。
    「这份报告是哪个模型写的」是可追溯性的一部分，但不值得为它扩大接口。
    """
    return str(getattr(provider, "model", "") or "")


# ══════════════════════════════════════════════════════ 流式执行


def _report_response(
    *,
    store: SessionStore,
    session_id: str,
    outline: ReportOutline,
    provider: ChatProvider,
    lease_token: str,
) -> StreamingResponse:
    """把报告生成包成 SSE 响应，并负责释放租约。

    ⚠️ **调用方必须先抢到租约**（和 `sessions._stream_response` 同一个约定）。

    ⛔ **不要复用 `sessions._stream_response`。** 它在 `CancelledError` 里
    会 append 一条 `LogAborted(reason="客户端断开连接")` —— 那对**对话流**
    是对的（那一轮确实被打断了），对报告流是**错的**：用户取消的是一次
    报告生成，而那条记录进的是**对话历史**，前端会把它渲染成
    「这轮对话被中断了」，而对话上什么事都没发生。
    """

    async def event_stream() -> AsyncIterator[str]:
        prose: dict[str, str] = {}
        notes: list[str] = []
        try:
            skeleton = [
                {"title": plan.title, "blocks": [b.model_dump() for b in plan.blocks]}
                for plan in outline.sections
            ]
            yield _sse("report_start", {"title": outline.title, "sections": skeleton})

            for index, plan in enumerate(outline.sections):
                parts: list[str] = []
                failure: str | None = None

                async for kind, content in _write_one_section(
                    provider, plan.title, plan.brief
                ):
                    if kind == "delta":
                        parts.append(content)
                        yield _sse("report_text", {"index": index, "delta": content})
                    else:
                        failure = content

                if failure is not None:
                    # ⚠️ 已经流出去的那些字**不能当正文用**。半截的段落
                    # 比没有段落更容易误导人 —— 它读起来是完整的。
                    logger.warning("报告第 %d 节生成失败：%s", index, failure)
                    notes.append(f"「{plan.title}」的正文没有生成出来：{failure}")
                    yield _sse(
                        "report_section_done",
                        {"index": index, "ok": False, "message": failure},
                    )
                else:
                    prose[plan.title] = "".join(parts)
                    yield _sse("report_section_done", {"index": index, "ok": True})

            document = render_document(
                outline,
                prose,
                notes=[
                    _provenance_note(provider),
                    *notes,
                ],
            )
            markdown = render_markdown(document)

            refs = await _persist(
                session_id, outline.title, document, markdown
            )
            if refs is None:
                yield _sse(
                    "report_error",
                    {
                        "message": (
                            "报告写好了，但没能存进产物库。"
                            "多半是磁盘空间或者临时目录的问题，"
                            "看一下后端日志里的那一行。"
                        )
                    },
                )
                return

            # 内容是完整的，才把记录写进日志。写一半的 `.md` 不配有一条
            # 「这份报告存在」的记录 —— 日志是 append-only，写错了修不掉。
            report = LogReport(
                title=outline.title,
                document=refs[0],
                sidecar=refs[1],
                provider=provider.name,
                model=_provider_model(provider),
            )
            await store.append(session_id, report)

            yield _sse(
                "report_done",
                {"document": document.model_dump(), "report": report.model_dump()},
            )

        except Exception as exc:
            # 最后一道保险。和 `sessions._stream_response` 的区别是这里
            # **不写日志** —— 报告失败不该在对话历史上留下任何痕迹。
            logger.exception("会话 %s 的报告生成异常终止", session_id)
            yield _sse(
                "report_error", {"message": f"{type(exc).__name__}: {exc}"}
            )

        finally:
            # 租约无论如何都要还，而且必须带令牌 —— 见 sessions/base.py。
            await sessions_api.shielded(store.release_lease(session_id, token=lease_token))

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=SSE_HEADERS
    )


def _provenance_note(provider: ChatProvider) -> str:
    """文档开头那句「谁写的」。**这份报告可不可信，读者有权知道来源。**"""
    model = _provider_model(provider)
    who = f"{provider.name} / {model}" if model else provider.name
    return f"正文由 {who} 撰写；决策记录、代码和插图直接取自过程记录，未经模型改写。"


# ══════════════════════════════════════════════════════ 端点


@router.post("/{session_id}/report", summary="生成报告（SSE）")
async def generate_report(session_id: str) -> StreamingResponse:
    """把这次会话写成一篇论文草稿。

    这个端点**不收请求体** —— 报告的内容完全由会话的日志决定，
    没有任何参数可调。将来要加「只写某几节」之类再做。

    ⚠️ **失败都在流开始之前用 HTTP 状态码表达**，和别的流式端点一致 ——
    出了第一个字节之后就只能走带内 `report_error` 帧了。

    这里刻意**不检查 `status == "idle"`**：会话正等着用户拍板时，
    已答的那些决策照样可以成文（`build_outline` 只收已答复的决策）。
    报告不是「对话的下一步」，它是「对已有过程的整理」。
    """
    store = sessions_api.get_store()
    session = await sessions_api.load_or_404(store, session_id)

    # provider 的配置错误在抢租约**之前**翻译成 400。顺序有讲究：
    # 反过来的话，一个「API Key 没配」会被一句「这个会话正忙」盖住，
    # 而用户会去等租约过期。
    try:
        provider = acquire_provider(None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    lease_token = await sessions_api.acquire_or_409(store, session_id)
    try:
        events = await store.events(session_id)
        outline = build_outline(
            events,
            session_id=session_id,
            title=session.title,
            captions=await _collect_captions(session_id, events),
        )
        if not outline.sections:
            raise HTTPException(
                status_code=409,
                detail=(
                    "这个会话还没有能写成报告的内容。"
                    "先跟它聊一轮、让它跑点东西，再回来生成报告。"
                ),
            )
    except Exception:
        await store.release_lease(session_id, token=lease_token)
        raise

    return _report_response(
        store=store,
        session_id=session_id,
        outline=outline,
        provider=provider,
        lease_token=lease_token,
    )
