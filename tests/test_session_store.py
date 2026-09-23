"""SQLite 会话存储的测试。

这个文件里有两条测试值得单独说明，因为它们守的是**不报错的错**：

    test_deleting_a_session_also_removes_its_events
        SQLite 的 `ON DELETE CASCADE` 默认是空操作，要靠每条连接的
        `PRAGMA foreign_keys = ON` 打开。忘了它 → 删会话只删 sessions 行，
        events 全变孤儿数据 —— 不报错、不崩溃，只是数据库慢慢变脏。

    test_status_cache_matches_the_derivation
        `sessions.status` 那一列是 `derive_status(events)` 的**缓存**。
        缓存和真相分叉是这类设计最容易出的问题，而且症状是
        「界面上显示的状态和实际不一致」—— 一眼看不出是哪边的错。

两条都是「全绿但不对」那一类，所以要有测试专门盯着。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modelforge.config import Settings
from modelforge.providers.events import ToolResult
from modelforge.sessions.base import (
    LogAssistant,
    LogDecisionAnswer,
    LogDecisionRequest,
    LogEvent,
    LogTool,
    LogUser,
    SessionStore,
)
from modelforge.sessions.project import derive_status, derive_title
from modelforge.sessions.sqlite_store import (
    PROJECT_ROOT,
    SessionRecorder,
    SqliteSessionStore,
    resolve_db_path,
)


@pytest.fixture
def store(tmp_path: Path) -> SqliteSessionStore:
    # 刻意**不用** conftest 里那个共享夹具：这个文件要直接操作存储，
    # 包括一些别的测试不该看到的边界（比如制造约束冲突）。
    return SqliteSessionStore(settings=Settings(database_path=tmp_path / "store.db"))


def assistant(calls: list[str] | None = None, *, text: str = "") -> LogAssistant:
    message: dict[str, object] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {"id": c, "type": "function", "function": {"name": "run_python", "arguments": "{}"}}
            for c in calls
        ]
    return LogAssistant(message=message)


# ══════════════════════════════════════════════════════ 基本 CRUD


async def test_create_and_get(store: SqliteSessionStore):
    created = await store.create()

    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.status == "idle"
    assert fetched.busy is False
    assert fetched.title == "新会话"  # 还没有用户消息，先占位


async def test_get_returns_none_for_an_unknown_id(store: SqliteSessionStore):
    """「没有这个东西」是调用方要用 if 处理的分支，不是异常情况。"""
    assert await store.get("nope") is None


async def test_delete_reports_whether_anything_was_deleted(store: SqliteSessionStore):
    session = await store.create()

    assert await store.delete(session.id) is True
    assert await store.delete(session.id) is False


async def test_list_recent_puts_the_most_recently_touched_first(store: SqliteSessionStore):
    first = await store.create()
    second = await store.create()
    await store.append(first.id, LogUser(content="把第一个顶上去"))

    recent = await store.list_recent()

    assert [s.id for s in recent] == [first.id, second.id]


async def test_it_satisfies_the_protocol(store: SqliteSessionStore):
    """运行时的协议自检。

    `@runtime_checkable` 的 isinstance 只检查方法名存不存在（不看签名），
    所以它拦不住「参数写错了」—— 但能拦住「实现里漏了一个方法」，
    而那正是写新存储后端时最容易忘的事。
    """
    assert isinstance(store, SessionStore)


# ══════════════════════════════════════════════════════ 追加


async def test_seq_starts_at_zero_and_increments(store: SqliteSessionStore):
    session = await store.create()

    seqs = [
        (await store.append(session.id, LogUser(content=f"第 {i} 句"))).seq
        for i in range(4)
    ]

    assert seqs == [0, 1, 2, 3]


async def test_events_come_back_in_order(store: SqliteSessionStore):
    session = await store.create()
    await store.append(session.id, LogUser(content="一"))
    await store.append(session.id, assistant(text="二"))
    await store.append(session.id, LogUser(content="三"))

    events = await store.events(session.id)

    assert [e.seq for e in events] == [0, 1, 2]
    assert events[0].event.content == "一"  # type: ignore[union-attr]


async def test_events_round_trip_through_json(store: SqliteSessionStore):
    """七种事件存进去、读出来必须还是原来那个东西。

    判别联合靠 `kind` 字段反序列化 —— 如果哪天有人把 `kind` 改名成
    别的东西，读写会在这里炸，而不是在某个用户的会话上静默丢数据。
    """
    session = await store.create()
    original: list[LogEvent] = [
        LogUser(content="帮我选个模型"),
        assistant(calls=["c1"], text="我跑一下"),
        LogTool(
            message={"role": "tool", "tool_call_id": "c1", "content": "结果"},
            result=ToolResult(call_id="c1", name="run_python", ok=True, stdout="结果"),
        ),
        LogDecisionRequest(call_id="c2", question="选哪个？", options=["A", "B"]),
        LogDecisionAnswer(call_id="c2", choice="A", note="备注"),
    ]
    for event in original:
        await store.append(session.id, event)

    restored = [e.event for e in await store.events(session.id)]

    assert restored == original


# ══════════════════════════════════════════════════════ 缓存一致性（核心）


@pytest.mark.parametrize(
    "tail",
    [
        [],
        [LogDecisionRequest(call_id="c1", question="?", options=["A"])],
        [
            LogDecisionRequest(call_id="c1", question="?", options=["A"]),
            LogDecisionAnswer(call_id="c1", choice="A"),
        ],
    ],
)
async def test_status_cache_matches_the_derivation(
    store: SqliteSessionStore, tail: list[LogEvent]
):
    """**本文件最重要的一条。**

    `sessions.status` 那一列是 `derive_status(events)` 的缓存 ——
    存在的理由只是「列表页不想重放所有事件」。

    缓存和真相分叉是这种设计最容易出的问题：症状是「界面上显示的状态
    和实际不一致」，而且一眼看不出该怀疑哪边。所以这里直接断言
    「数据库里存的那个值 == 拿全部事件重算出来的值」，
    在三种典型状态下各验一遍。

    实现上之所以能做到这一点，是因为 `append` 会**在同一个事务里**
    重放全部事件再写回缓存，而不是「增量地判断该改成什么」——
    后者会是 `derive_status` 的第二份实现，两份迟早漂移。
    """
    session = await store.create()
    await store.append(session.id, LogUser(content="一句话"))
    await store.append(session.id, assistant(calls=["c1"]))
    for event in tail:
        await store.append(session.id, event)

    events = await store.events(session.id)
    fetched = await store.get(session.id)

    assert fetched is not None
    assert fetched.status == derive_status(events)


async def test_title_cache_matches_the_derivation(store: SqliteSessionStore):
    """标题和状态走的是同一套机制（append 时同事务刷新）。"""
    session = await store.create()
    await store.append(session.id, LogUser(content="帮我做 2024 国赛 A 题"))
    await store.append(session.id, LogUser(content="第二句不该覆盖标题"))

    events = await store.events(session.id)
    fetched = await store.get(session.id)

    assert fetched is not None
    assert fetched.title == derive_title(events)
    assert fetched.title == "帮我做 2024 国赛 A 题"


# ══════════════════════════════════════════════════════ 约束


async def test_deleting_a_session_also_removes_its_events(store: SqliteSessionStore):
    """**回归测试：`PRAGMA foreign_keys = ON` 有没有真的执行。**

    这条测试存在的唯一理由是：**忘了那个 PRAGMA 是完全静默的。**
    SQLite 不报错、不崩溃，只是把级联删除变成空操作 ——
    于是删了会话之后 events 表里留下一堆永远没人读的孤儿数据，
    而且越积越多，直到某天你发现数据库文件大得离谱。

    有人「顺手」把 `_connect()` 里那几行 PRAGMA 删掉时，这条会红。
    """
    session = await store.create()
    await store.append(session.id, LogUser(content="这些事件应该跟着被删掉"))
    assert len(await store.events(session.id)) == 1

    await store.delete(session.id)

    assert await store.events(session.id) == []
    # 直接查表确认，而不是只信 store 的 API —— 万一 events() 自己写错了呢
    with sqlite3.connect(store.path) as raw:
        remaining = raw.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert remaining == 0


async def test_a_decision_can_only_be_answered_once(store: SqliteSessionStore):
    """**由数据库保证**，不是靠端点里的 if。

    这是 M4 那条「同一个 call_id 只能回答一次」的硬约束。放在数据库上
    而不是 API 里，是因为它是一条**数据完整性规则**：
    重复的答案会让投影出两条内容打架的 tool 消息，
    而模型会看到「用户选了 A」和「用户选了 B」并存。

    放在数据库上还有一个好处：不管将来有几个端点、多少条并发路径，
    都绕不过去。
    """
    session = await store.create()
    await store.append(session.id, LogDecisionAnswer(call_id="c1", choice="A"))

    with pytest.raises(sqlite3.IntegrityError):
        await store.append(session.id, LogDecisionAnswer(call_id="c1", choice="B"))


async def test_the_same_call_id_can_be_answered_in_a_different_session(
    store: SqliteSessionStore,
):
    """唯一约束是 (session, call_id) 而不是全局的 call_id。

    看起来是废话，但写索引时漏掉 `session_id` 那一列是很容易犯的错 ——
    而症状要到「两个会话恰好用了同一个 call_id」时才出现，
    也就是几乎永远发现不了。
    """
    first = await store.create()
    second = await store.create()

    await store.append(first.id, LogDecisionAnswer(call_id="c1", choice="A"))
    await store.append(second.id, LogDecisionAnswer(call_id="c1", choice="B"))

    assert len(await store.events(second.id)) == 1


# ══════════════════════════════════════════════════════ 租约


async def test_a_session_can_only_be_leased_once(store: SqliteSessionStore):
    """**并发保护的核心理由。**

    两个并发的请求交错写同一个日志，投影出来是**非法历史**
    （assistant 和它的 tool 结果之间插进了另一条 assistant），
    不只是脏数据。而且这种损坏是持久的 —— 那个会话再也发不出请求了。
    """
    session = await store.create()

    assert await store.acquire_lease(session.id, seconds=60) is True
    assert await store.acquire_lease(session.id, seconds=60) is False

    fetched = await store.get(session.id)
    assert fetched is not None
    assert fetched.busy is True


async def test_releasing_a_lease_frees_it(store: SqliteSessionStore):
    session = await store.create()
    await store.acquire_lease(session.id, seconds=60)

    await store.release_lease(session.id)

    assert await store.acquire_lease(session.id, seconds=60) is True


async def test_an_expired_lease_can_be_taken_over(store: SqliteSessionStore):
    """租约是**有 TTL 的**，过期就自动失效。

    这条性质是整个并发方案能自愈的原因：进程被强杀时来不及还租约，
    但那条租约最多挂 TTL 那么久，之后下一个请求就能接手。
    没有 TTL 的话，一次崩溃会让那个会话永久卡死。

    （`clear_all_leases` 在启动时兜底，但那是优化，不是正确性的依赖。）
    """
    session = await store.create()
    # seconds=-1 → 租约立刻就是过期的
    await store.acquire_lease(session.id, seconds=-1)

    assert await store.acquire_lease(session.id, seconds=60) is True


async def test_clearing_all_leases_frees_every_session(store: SqliteSessionStore):
    """**进程启动时必须调用这个**（见 main.py 的 lifespan）。

    开发期热重载频繁重启，每次都可能在租约还没还的时候就掐死进程 ——
    不清一遍的话，用户会看到一个莫名其妙的 409，而且要等三分钟才自愈。
    """
    first = await store.create()
    second = await store.create()
    await store.acquire_lease(first.id, seconds=300)
    await store.acquire_lease(second.id, seconds=300)

    cleared = await store.clear_all_leases()

    assert cleared == 2
    assert await store.acquire_lease(first.id, seconds=60) is True


async def test_leasing_an_unknown_session_fails_instead_of_creating_one(
    store: SqliteSessionStore,
):
    """UPDATE 打不中任何行时 rowcount 是 0 —— 而这和「被人占着」是同一个返回值。

    这不是 bug：两种情况对调用方来说都是「你现在不能用它」。
    但如果哪天有人把实现改成「先 INSERT 再 UPDATE」，这条会红。
    """
    assert await store.acquire_lease("does-not-exist", seconds=60) is False


# ══════════════════════════════════════════════════════ 路径解析


def test_relative_db_path_is_anchored_to_the_project_root(tmp_path: Path):
    """相对路径锚在项目根目录上，**不是当前工作目录**。

    锚 CWD 的话，在 `web/` 里启动一次后端就会得到一个空数据库，
    然后开始怀疑「为什么我的会话全不见了」。
    `subprocess_exec.py` 早就为沙箱工作目录定下了这条规矩
    （那里的注释写着「CWD 是不可靠的」），这里是同一个问题。
    """
    resolved = resolve_db_path(Settings(database_path=Path("data/modelforge.db")))

    assert resolved == PROJECT_ROOT / "data" / "modelforge.db"
    assert resolved.is_absolute()


def test_absolute_db_path_is_left_alone(tmp_path: Path):
    absolute = tmp_path / "somewhere" / "custom.db"

    assert resolve_db_path(Settings(database_path=absolute)) == absolute


# ══════════════════════════════════════════════════════ 录制器


async def test_recorder_writes_the_three_things_the_loop_reports(
    store: SqliteSessionStore,
):
    """`SessionRecorder` 把循环报告的三件事落到正确的记录类型上。

    这一层薄得几乎什么都不做，但它存在的意义是让 `loop.py`
    完全不知道存储的存在 —— 循环只看到一个 assistant_round /
    tool_result / decision_requested 的接口。
    """
    session = await store.create()
    recorder = SessionRecorder(store, session.id)

    await recorder.assistant_round({"role": "assistant", "content": "我在想"})
    await recorder.tool_result(
        {"role": "tool", "tool_call_id": "c1", "content": "5050"},
        ToolResult(call_id="c1", name="run_python", ok=True, stdout="5050"),
    )
    await recorder.usage(prompt_tokens=10, completion_tokens=3, finish_reason="stop")

    kinds = [e.event.kind for e in await store.events(session.id)]
    assert kinds == ["assistant", "tool", "usage"]
