"""会话端点 —— HITL 的中断与恢复。

════════════════════════════════════════════════════════════════════════
这个模块在整条链路上的位置
════════════════════════════════════════════════════════════════════════

    浏览器  ──POST /messages──►  本模块  ──►  run_agent_turn  ──►  SSE
                                  │                │
                                  │                └─ recorder ──► 事件日志
                                  └─ 租约 / 状态校验 / 投影重建

它替换掉了 M2/M3 那个**无状态**的 `POST /api/chat/stream`（那个端点还在，
保留作调试用途）。差别只有一处，但那一处改变了一切：

    无状态：前端每次把**整个对话历史**发过来
    有状态：前端只发**这一次的新消息**，历史由后端从事件日志重建

ADR-002 说的「状态落盘 + 点击时重建」，落点就在这一层。

════════════════════════════════════════════════════════════════════════
三个端点，对应会话的三种「下一步」
════════════════════════════════════════════════════════════════════════

    POST /{id}/messages    idle 状态：用户说了句话 → 开一轮
    POST /{id}/decisions   awaiting_user 状态：用户拍板了 → 把答案补上继续
    POST /{id}/resume      任意就绪状态：什么都不追加，按现有历史重开一条流

`/resume` 看起来多余（它不改变任何状态），但它是 ADR-004 承诺的那条重试路径：
「Provider 层不重试，重试决策权归上层状态机」。这里的场景是 ——
用户点了决策卡片，`decision_answer` 已经落盘，续流的请求在网络层抖了一下，
错误以带内 ErrorEvent 结束。此时**决策已经答过了**，再点一次会被
「同一个 call_id 只能回答一次」的数据库约束挡回来。没有 `/resume` 的话，
用户只能另发一条消息，而那条 tool 结果就永远没有后续了。

════════════════════════════════════════════════════════════════════════
并发：数据库租约，不是进程内的锁
════════════════════════════════════════════════════════════════════════
用 `asyncio.Lock` 会便宜很多，但它只在单进程内有效 —— 而 ADR-002 拒绝
「进程内挂起 Future」的理由正是「服务重启丢进度、无法水平扩展」。
用一个只保护单进程的锁去保护一个跨进程的状态，等于把那个理由原样搬了回来。

所以租约写在数据库里（`sessions.lease_until`），抢占用一条带条件的 UPDATE：

    UPDATE sessions SET lease_until = ?, lease_token = ?
    WHERE id = ? AND (lease_until IS NULL OR lease_until < ?)

`rowcount == 0` 就是「有人正在用」。多进程、多机都认这条。

⚠️ **`lease_token` 是 M6a 加的，它修的是「释放」那一半。** 光有抢占没有
所有权，一个跑得比 TTL 还慢的请求跑完时会**放掉别人的租约**
（详见 `sessions/base.py::acquire_lease`）。所以抢到租约的那个协程会拿到
一个令牌，还租约时必须交回来 —— 对不上就什么都不做。

⚠️ 配套的一条：`main.py` 的 lifespan 启动时必须 `clear_all_leases()`。
   不调用的话，上次进程被强杀（而不是优雅退出）留下的租约会让那个会话
   在 TTL 内一直返回 409 —— 开发期热重载反复重启时尤其明显。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# 和 api/artifacts.py 同样的理由：取产物存储要**运行时查模块属性**，
# 否则 tests/conftest.py 的 monkeypatch 罩不住这里，测试会写进真实目录。
from modelforge import artifacts as artifact_store_pkg
from modelforge.agents.loop import run_agent_turn
from modelforge.api.chat import acquire_provider, format_sse, get_executor
from modelforge.config import get_settings
from modelforge.providers.base import ChatProvider, Message, ToolSpec
from modelforge.providers.events import ErrorEvent, Finish
from modelforge.sandbox.tools import ASK_USER_SPEC, TOOL_SPECS
from modelforge.sessions.base import (
    LogAborted,
    LogDecisionAnswer,
    LogReport,
    LogUser,
    Session,
    SessionStore,
    StoredEvent,
)
from modelforge.sessions.project import (
    Decision,
    build_messages,
    derive_status,
    list_decisions,
    list_reports,
)
from modelforge.sessions.sqlite_store import SessionRecorder, SqliteSessionStore

logger = logging.getLogger(__name__)

__all__ = [
    "acquire_or_409",
    "get_store",
    "load_or_404",
    "resolve_provider",
    "router",
    "shielded",
]

router = APIRouter(prefix="/sessions", tags=["sessions"])

# 有会话的端点用这份工具集：跑代码 + 问用户。
# 无状态的 /api/chat/stream 用的是 TOOL_SPECS（只有 run_python）——
# 它没有地方能让用户回答问题，给了 ask_user 只会得到一个死胡同。
SESSION_TOOL_SPECS: list[ToolSpec] = [*TOOL_SPECS, ASK_USER_SPEC]


# ══════════════════════════════════════════════════════ 存储装配


def get_store() -> SessionStore:
    """构造会话存储。

    和 `chat.get_executor` 一样**刻意不缓存**：构造函数只解析了一个路径字符串，
    没有任何 I/O（连接是在每次操作内部开的）。没成本的东西不需要缓存，
    而缓存会带来「配置改了不生效」的麻烦。

    做成模块级函数是为了可测：测试 monkeypatch 掉这个名字，
    就能塞一个指向临时目录的存储进来，不需要污染真实的数据库文件。
    """
    return SqliteSessionStore()


# ══════════════════════════════════════════════════════ 请求 / 响应模型


class SessionDetail(BaseModel):
    """`GET /sessions/{id}` 的响应 —— 前端**刷新页面后重建整个界面**靠它。"""

    session: Session
    events: list[StoredEvent]
    decisions: list[Decision]
    """决策列表是**从 events 派生**的，看起来冗余。
    之所以显式带上，是因为前端渲染决策卡片时需要「待答 / Option 列表 / 已选的答案」
    这三样凑在一起的东西，而它们散落在两种日志记录里 ——
    让前端自己拼等于把投影逻辑在前端抄了第二份。
    """

    reports: list[LogReport] = []
    """生成过的报告（M6a）。理由同上。

    ⚠️ **它同时也是这一轮最重要的一道防线。** 产物的前端路径是
    「扫 `events` 里 `kind === 'tool'` 的结果」—— 而 `report` 是**第八种** kind，
    前端的 reducer 少一个 `case` **不会编译报错**（那个 switch 没有穷尽性断言，
    末尾还有 return），于是报告在刷新后会静默消失，而所有 pytest 都是绿的。

    把 reports 显式投影出来，就不必指望前端记得加那个 case。TS 那边
    仍然要加（契约测试会红），但「忘了加」的后果从「功能没了」降级成
    「多一份没用到的类型」。
    """


class NewMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class DecisionSubmission(BaseModel):
    """用户对一个决策点的回答。

    这是**用户可控字符串进入模型上下文的唯一入口**，所以三道校验都在这里：
    长度上限、`call_id` 必须是当前 pending 的那个、`choice` 必须在选项里
    （或者该决策允许自由作答）。
    """

    call_id: str = Field(min_length=1, max_length=200)
    choice: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=2000)


# ══════════════════════════════════════════════════════ 小工具


async def shielded(coro: object) -> None:
    """跑一个清理动作，**即使当前协程正在被取消**。

    为什么需要这个？因为 Starlette 在浏览器断开时会取消跑着我们生成器的那个
    task。取消是以 `CancelledError` 注入到**当前 await 点**的，
    于是 `finally` 里紧跟其后的那个 `await` 会立刻再抛一次 ——
    清理动作根本没机会跑完。

    `asyncio.shield` 把内部协程放进一个独立 task，外层的取消不会传到它身上；
    再 `suppress` 掉 await 这个 shield 时冒出来的 CancelledError
    （取消仍然会从我们自己的 `raise` 那里正常向上传播，不会被吞掉）。

    不做这件事的后果很具体：**租约永远不释放**，用户在 TTL（默认 180 秒）内
    对同一个会话发任何请求都会拿到 409，而他只是刷新了一下页面。
    """
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.shield(coro)  # type: ignore[arg-type]


async def load_or_404(store: SessionStore, session_id: str) -> Session:
    session = await store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{session_id}")
    return session


async def acquire_or_409(store: SessionStore, session_id: str) -> str:
    """抢占会话，抢不到就 409。**成功时返回所有权令牌**，调用方必须把它
    一路带到 `release_lease(session_id, token=...)`。

    **这是真正的并发闸门。** 上层那些状态校验（status 是不是 idle）只是
    为了让错误信息更好懂 —— 它们都有 TOCTOU 窗口，两个并发请求可以同时通过。
    只有这条带条件的 UPDATE 是原子的。

    令牌为什么要出现在签名里：释放时不带令牌 = 无条件释放，而一个跑得比
    TTL 还慢的请求（报告生成必然如此）会把**已经换手给别人**的租约放掉。
    详见 `sessions/base.py` 里 `acquire_lease` 的那段说明。
    """
    settings = get_settings()
    token = await store.acquire_lease(session_id, seconds=settings.session_lease_seconds)
    if token is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "这个会话正忙（有一条回复正在生成，或者上一次异常退出留下了租约）。"
                "等它跑完再试；如果确定没有在跑，稍等一会儿租约会自动过期。"
            ),
        )
    return token


def resolve_provider(name: str | None) -> ChatProvider:
    """取 Provider，把配置类错误翻译成 HTTP 400。

    复用 `chat.py` 的 `acquire_provider`（**必须**复用，不能自己造一个）——
    它内部维护着进程级的缓存，而 Provider 手里攥着 httpx 连接池。
    两个模块各缓存一份 = 两个连接池 = 每个请求多一轮 TLS 握手。

    配置类错误（provider 名字写错、API Key 没配）发生在流开始之前，
    所以能用 HTTP 状态码表达。这和「流开始之后的错误只能走带内 ErrorEvent」
    是同一件事的两面 —— 详见 chat.py 的模块注释。
    """
    try:
        return acquire_provider(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ══════════════════════════════════════════════════════ 流式执行


def _stream_response(
    *,
    store: SessionStore,
    session_id: str,
    messages: list[Message],
    provider_name: str | None,
    lease_token: str,
) -> StreamingResponse:
    """把一次 `run_agent_turn` 包成 SSE 响应，并负责租约的释放。

    ⚠️ **调用方必须先抢到租约**。这个函数只负责还 —— 因为「抢」可能失败
    并需要返回 409（一个 HTTP 状态码，必须在流开始之前发出去），
    而「还」永远发生在流结束之后。

    `lease_token` 是抢租约时拿到的凭证，原样交还给 `release_lease` ——
    这东西一旦中途丢了，释放就会变成无条件的，见 `acquire_or_409`。

    `temperature` / `max_tokens` 暂时写死在调用点。等 M5 做了「设置」
    面板再从这里透出去 —— 现在多传两个没人改的参数只是噪音。
    """
    provider = resolve_provider(provider_name)

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in run_agent_turn(
                provider,
                messages,
                executor=get_executor(),
                recorder=SessionRecorder(store, session_id),
                tools=SESSION_TOOL_SPECS,
                # 模型生成的图归到这个会话名下（M5）。会话这一层是唯一
                # 知道「这些产物该跟着谁走」的地方，所以 scope 从这里给出去，
                # 经 loop、dispatch 一路传到 executor。
                scope=session_id,
            ):
                yield format_sse(event)

        except asyncio.CancelledError:
            # 用户关了页面 / 点了停止。如实记一笔 —— 否则日志会停在
            # 「用户问了一句，之后什么都没有」，后人完全分不清这是
            # 「还没跑完」还是「跑砸了」。
            #
            # 注意这里**记不了**这一轮已经流出去的那半截文本：一轮生成
            # 只在跑完时才落盘（见 loop.py），所以被中断的那一轮不留痕迹。
            # 用户看到过的字刷新后会消失 —— 这是 M4 明确接受的代价，
            # 详见 ADR-009。
            logger.info("会话 %s 的流被客户端中断", session_id)
            await shielded(store.append(session_id, LogAborted(reason="客户端断开连接")))
            raise

        except Exception as exc:
            # 最后一道保险。Agent 循环内部的失败都走带内 ErrorEvent，
            # 能走到这里说明是上游代码本身的 bug。
            # 即便如此也必须补一个 Finish —— 契约（Finish 一定最后出现）
            # 是跨层承诺，每一层都有责任维护它。
            logger.exception("会话 %s 的流异常终止", session_id)
            await shielded(store.append(session_id, LogAborted(reason=f"{type(exc).__name__}")))
            yield format_sse(ErrorEvent(message=f"{type(exc).__name__}: {exc}"))
            yield format_sse(Finish(reason="error"))

        finally:
            # 无论怎么结束都要还租约，哪怕上面已经 raise 了。
            # 带上令牌 —— 如果这条租约已经过期并被别人接管，这个调用会
            # 什么也不做，而不是把别人的租约放掉。
            await shielded(store.release_lease(session_id, token=lease_token))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ══════════════════════════════════════════════════════ 端点


@router.post("", summary="建一个会话")
async def create_session() -> Session:
    return await get_store().create()


@router.get("", summary="最近的会话列表")
async def list_sessions(limit: int = 50) -> list[Session]:
    return await get_store().list_recent(limit=max(1, min(limit, 200)))


@router.get("/{session_id}", summary="会话详情（含完整事件日志）")
async def get_session(session_id: str) -> SessionDetail:
    """刷新页面时前端调这个，用返回的事件流把整个界面重建出来。

    返回的是**事件**而不是投影好的消息，因为界面要的不只是对话气泡 ——
    工具卡片、决策卡片、时间线都要。前端有一个 reducer 把这些事件
    折叠成视图模型，实时路径和回放路径喂给它的是同一种输入。
    """
    store = get_store()
    session = await load_or_404(store, session_id)
    events = await store.events(session_id)
    return SessionDetail(
        session=session,
        events=events,
        decisions=list_decisions(events),
        reports=list_reports(events),
    )


@router.delete("/{session_id}", summary="删除会话")
async def delete_session(session_id: str) -> dict[str, bool]:
    """删除会话，**连同它的产物**。

    两件事必须一起做，否则会留下一堆谁也不认识的图：日志没了，
    那些产物的引用就没了，界面上再也看不到它们，而磁盘上还在占着地方。
    这是「孤儿产物」两种来源里的第一种（另一种是进程被强杀，
    由 `main.py` 启动时清理）。

    ⚠️ 顺序是**先删会话再删产物**，而且删产物失败**不让整个请求失败**。

    为什么是这个顺序：`store.delete()` 可能返回 False（会话不存在），
    那时候我们已经把图删了 —— 但那种情况下本来也没人会来取它们。
    反过来先删产物再删会话的话，删会话失败会留下一堆「日志里记着有产物、
    盘上却没有」的引用，那是更糟的状态：用户点开图会拿到 404，
    而界面上明明画着一张缩略图。

    产物删不掉（Windows 上浏览器正开着那张图，句柄没释放）时只记一笔日志。
    用户的意图是「删掉这个会话」，会话确实删掉了；
    剩下的目录会在下次启动时被 `sweep_orphans()` 收走。
    """
    store = get_store()
    deleted = await store.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{session_id}")

    try:
        removed = await artifact_store_pkg.get_store().delete_scope(session_id)
        if removed:
            logger.info("已删除会话 %s 的 %d 个产物", session_id, removed)
    except Exception:
        logger.warning("删除会话产物失败（下次启动会清理）", exc_info=True)

    return {"deleted": True}


@router.post("/{session_id}/messages", summary="发一条消息（SSE）")
async def send_message(
    session_id: str, payload: NewMessage, request: Request
) -> StreamingResponse:
    """用户说了句话，开一轮新的生成。

    ⚠️ **必须要求 `status == "idle"`。** 有未答复的决策时直接发消息，
    会让下一次请求 100% 失败：投影出来的历史里会有一个「有 tool_calls
    却没有对应结果」的 assistant 消息，多数服务直接 400。

    只在前端禁用输入框是不够的 —— 用户刷新页面、开第二个标签页、
    或者直接 curl 都能绕过去。服务端这道门才是真的门。
    """
    store = get_store()
    await load_or_404(store, session_id)
    lease_token = await acquire_or_409(store, session_id)

    try:
        events = await store.events(session_id)
        if derive_status(events) != "idle":
            raise HTTPException(
                status_code=409,
                detail="还有一个问题在等你拍板，先回答它再发新消息。",
            )

        await store.append(session_id, LogUser(content=payload.content))
        events = await store.events(session_id)
        messages = build_messages(events)
    except Exception:
        # 抢到租约之后的任何失败都必须还回去，否则这个会话要卡到租约过期。
        await store.release_lease(session_id, token=lease_token)
        raise

    return _stream_response(
        store=store,
        session_id=session_id,
        messages=messages,
        provider_name=None,
        lease_token=lease_token,
    )


@router.post("/{session_id}/decisions", summary="提交决策（SSE）")
async def submit_decision(
    session_id: str, payload: DecisionSubmission, request: Request
) -> StreamingResponse:
    """用户拍板了，把答案补上继续跑。

    这里做的四件事，正好就是 ADR-002 那句话的展开：

        校验 → 把答案 append 成一条事件 → 从事件日志重新投影出消息历史
        → **重新跑一次 `run_agent_turn`**

    注意最后一步：没有任何「恢复」代码。我们不是唤醒一个挂起的生成器，
    而是用一个完整重建出来的历史从头跑一遍 —— 进程重启过、机器换过，
    都无所谓。
    """
    store = get_store()
    await load_or_404(store, session_id)
    lease_token = await acquire_or_409(store, session_id)

    try:
        events = await store.events(session_id)
        pending = next(
            (d for d in list_decisions(events) if d.choice is None), None
        )
        if pending is None:
            raise HTTPException(status_code=409, detail="这个会话现在没有待回答的问题。")

        if pending.call_id != payload.call_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"要回答的是 {pending.call_id!r}，不是 {payload.call_id!r}。"
                    "多半是页面上的卡片过期了，刷新一下。"
                ),
            )

        choice = payload.choice.strip()
        # 允许自由作答时就不校验选项（用户能写选项之外的答案）；
        # 不允许时必须命中选项之一，否则模型会收到一个它没提供过的方案。
        if not pending.allow_free_text and choice not in pending.options:
            raise HTTPException(
                status_code=422,
                detail=f"这个问题的选项是 {pending.options}，{choice!r} 不在里面。",
            )

        await store.append(
            session_id,
            LogDecisionAnswer(call_id=payload.call_id, choice=choice, note=payload.note),
        )
        events = await store.events(session_id)
        messages = build_messages(events)
    except Exception:
        await store.release_lease(session_id, token=lease_token)
        raise

    return _stream_response(
        store=store,
        session_id=session_id,
        messages=messages,
        provider_name=None,
        lease_token=lease_token,
    )


@router.post("/{session_id}/resume", summary="按当前历史重开一条流（SSE）")
async def resume_session(session_id: str, request: Request) -> StreamingResponse:
    """不追加任何事件，只用现有的投影跑一轮。

    这是 ADR-004 那条重试路径的落点。最容易遇到的场景：

        用户点了决策卡片 → decision_answer 落盘 → 续流的请求网络抖了一下
        → 带内 ErrorEvent，流结束

    此时**决策已经答过了**，再点一次会被数据库的唯一索引挡回来
    （一个 call_id 只能回答一次，这是对的）。但没有它，用户就只能另发一条
    消息，而模型的历史里那条 tool 结果永远没有后续 —— 它会以为自己问了
    一个没人搭理的问题。

    代价是它可能被滥用成「重复跑同一轮」，烧掉额外的 token。所以它受
    租约保护（同一时刻只能有一条流），但 M4 不做频率限制 ——
    真要滥用的话，前端的按钮比这个端点好管得多。
    """
    store = get_store()
    await load_or_404(store, session_id)
    lease_token = await acquire_or_409(store, session_id)

    try:
        events = await store.events(session_id)
        if derive_status(events) != "idle":
            raise HTTPException(
                status_code=409,
                detail="还有问题在等用户拍板，先回答它再重开。",
            )
        messages = build_messages(events)
    except Exception:
        await store.release_lease(session_id, token=lease_token)
        raise

    return _stream_response(
        store=store,
        session_id=session_id,
        messages=messages,
        provider_name=None,
        lease_token=lease_token,
    )
