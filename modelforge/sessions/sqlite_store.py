"""SQLite 实现的会话存储 —— M4 的默认后端。

════════════════════════════════════════════════════════════════════════
为什么用 `asyncio.to_thread` 包一个阻塞的 sqlite3
════════════════════════════════════════════════════════════════════════
和 M3 的沙箱执行器是**同一个决定，同一个理由**。

sqlite3 是同步阻塞的；FastAPI 是 async 的。三条路：

  ① 直接阻塞调用        → 每次读写数据库都会卡住整个事件循环。
                          开发时看不出来，上线后表现为「偶尔整个服务停顿一下」。
  ② aiosqlite           → 多一个依赖。它的本质也是线程池，只是藏在库里。
  ③ asyncio.to_thread   → 和 ② 等价，但不加依赖，而且和 M3 已经踩过的
                          那条经验一致：**别把功能正确性押在事件循环上**。

选 ③。M3 那里踩的坑（Windows 的 SelectorEventLoop 不支持 asyncio 子进程）
教训是：任何依赖「服务器碰巧选了哪个事件循环」的写法都会在某个真实部署里炸。
`to_thread` 在哪种循环下都能跑。

════════════════════════════════════════════════════════════════════════
SQLite 的四个静默陷阱
════════════════════════════════════════════════════════════════════════
每一个的特征都一样：**不报错，只是行为不对**，而且单元测试常常测不到。

  ① `ON DELETE CASCADE` **默认是空操作**。SQLite 的外键约束要靠
     `PRAGMA foreign_keys = ON` 打开，而且**是每条连接各自设置的**
     （不是数据库级的持久设置）。忘了它 → 删会话只删掉 sessions 那一行，
     events 全变成孤儿数据，越积越多，还不会有任何报错。

  ② `busy_timeout` 默认是 **0**。只要有两个连接同时想写，后到的那个
     立刻抛 `database is locked`。这个异常会从 recorder 一路抛进
     `run_agent_turn`，被 SSE 端点最后那道兜底 except 转成一条带内 error ——
     现象是「随机某条回答被截断」，而日志里只有一句 SQLite 错误。
     给 5 秒缓冲，绝大多数瞬时竞争都会自己消失。

  ③ `check_same_thread` 默认是 **True**，而连接是**绑定线程**的。
     所以连接必须在 `to_thread` 的**工作函数内部**创建。
     图省事在协程里建好再传进来会抛 `ProgrammingError`，而且**时灵时不灵**
     —— 线程池复用线程时恰好能过。又是 M3 那个「单元测试全绿」的配方。

  ④ `journal_mode` 默认是 DELETE，读写互斥。WAL 让读不阻塞写。

  ⑤ **`CREATE TABLE IF NOT EXISTS` 不会给一张已存在的表加列。**
     在开发机上永远看不出来（库删了重建就好），在别人的机器上是启动就炸。
     和上面四条的区别是它**不静默** —— 报「no such column」，
     只是那句话完全不提「你忘了迁移」。处理见 `_migrate()`。

（④ 是持久设置，写一次就记在数据库文件里；①②③⑤ 都是每次建连接要过一遍的。
这个区别本身也是个坑，所以 `_connect()` 里把它们无条件执行一遍 ——
重复设置没有代价，漏掉有。）
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from modelforge.config import Settings, get_settings
from modelforge.paths import resolve_project_path
from modelforge.providers.base import Message
from modelforge.providers.events import DecisionRequest, ToolResult
from modelforge.sessions.base import (
    LogAssistant,
    LogDecisionRequest,
    LogEvent,
    LogTool,
    LogUsage,
    Session,
    SessionStore,
    StoredEvent,
    parse_log_event,
)
from modelforge.sessions.project import derive_status, derive_title

logger = logging.getLogger(__name__)

__all__ = ["SessionRecorder", "SqliteSessionStore", "resolve_db_path"]

T = TypeVar("T")


def resolve_db_path(settings: Settings) -> Path:
    """把配置里的数据库路径解析成绝对路径，**锚在项目根目录上**。

    为什么不按当前工作目录（CWD）？因为 CWD 是不可靠的 —— 用户可能从任何
    地方敲启动命令。同一套规矩在沙箱那边也定过一次，M5 把两者收进了
    `modelforge/paths.py`（那里把「项目内的东西锚项目根」和「运行时生成的
    东西落系统目录」这两条反向的规则写在了一起）。

    ⚠️ 两套约定并存一定会有人踩：在 `web/` 目录下启动后端，得到一个空数据库，
    然后开始怀疑为什么「会话列表是空的但刷新也没用」。统一按 PROJECT_ROOT。
    """
    return resolve_project_path(Path(settings.database_path))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,
    lease_until REAL,                 -- unix 时间戳；NULL = 空闲
    lease_token TEXT,                 -- 当前持有者的凭证；NULL = 空闲
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

-- 「同一个 call_id 只能回答一次」交给数据库保证，而不是靠端点里的 if。
-- 这是一个**部分唯一索引**（WHERE 子句），只约束 decision_answer 那些行。
CREATE UNIQUE INDEX IF NOT EXISTS one_answer_per_call
    ON events(session_id, json_extract(payload, '$.call_id'))
    WHERE kind = 'decision_answer';
"""


_COLUMNS_ADDED_LATER: list[tuple[str, str, str]] = [
    # (表, 列名, 建列语句)。M6a 加的：租约的所有权凭证，见 acquire_lease。
    ("sessions", "lease_token", "ALTER TABLE sessions ADD COLUMN lease_token TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """把 `_SCHEMA` 里新加的列补到**已经存在**的数据库上。

    ⚠️ **`CREATE TABLE IF NOT EXISTS` 不会修改一张已经存在的表。**

    这句话值得单独写一段，因为它是 SQLite（以及多数数据库）最容易被误读的
    行为之一：在一个有数据的库上启动新版本，`_SCHEMA` 里新加的那一列
    **一声不吭地不生效**，然后第一条用到它的 UPDATE 抛「no such column」——
    报错指向的是那行 SQL，完全不会让人想到「表没迁移」。
    而开发机上还复现不了（开发机的库早就删掉重建过了）。

    所以每加一列要对**两处**负责：
      · `_SCHEMA` 里写上 —— 给新建的库用
      · 这里登记一条 —— 给已经在用的库用
    两处都要改是这个方案的代价，换来的是不必引入 Alembic 那套迁移框架。

    判据用 `PRAGMA table_info` 而不是「先 ALTER 再 catch 重复列名」：
    后者的 except 会把「表根本不存在」这类真错误一起吞掉。
    """
    for table, column, ddl in _COLUMNS_ADDED_LATER:
        # 表名和 DDL 都是这个文件里的字面量，不接受任何外部输入。
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(ddl)


def _now_iso() -> str:
    """统一的 ISO8601 UTC 时间戳。

    `timespec="microseconds"` 不是可有可无的 —— 默认的 `isoformat()` 在
    微秒恰好是 0 时会**省略那一整段**，于是同一列里会出现两种宽度的字符串，
    按文本排序时出错。显式固定精度，宽度就永远一致。
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


class SqliteSessionStore:
    """满足 `SessionStore` 协议的 SQLite 实现。"""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.path = resolve_db_path(self._settings)

    # ────────────────────────────────────────────── 连接

    def _connect(self) -> sqlite3.Connection:
        """开一条新连接。

        ⚠️ **必须在 `to_thread` 的工作函数内部调用**，不能在协程里建好传进来。
        原因见模块注释的陷阱 ③。

        每次操作开一条新连接（而不是复用一个长连接）是刻意的：
        连接是线程绑定的，而线程池会把不同的操作调度到不同的线程上；
        想复用就得管一个 per-thread 的连接池，那是给「每秒几千次查询」
        准备的优化，而这里的负载是「一个用户在对话框里敲字」。
        SQLite 打开连接的成本在本地文件上约几十微秒，不值得为它引入状态。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row

        # 顺序有讲究：busy_timeout 要**最先**设，否则后面几条 PRAGMA
        # 自己就可能撞上瞬时锁而直接失败。
        conn.execute("PRAGMA busy_timeout = 5000")   # 陷阱 ②
        conn.execute("PRAGMA journal_mode = WAL")    # 陷阱 ④
        conn.execute("PRAGMA foreign_keys = ON")     # 陷阱 ①
        conn.executescript(_SCHEMA)
        _migrate(conn)   # 陷阱 ⑤：IF NOT EXISTS 不改已存在的表
        return conn

    async def _run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """把一段阻塞的数据库操作甩到工作线程上跑，并包在一个事务里。

        `with conn:` 在成功时提交、异常时回滚 —— 这是 sqlite3 连接对象的
        上下文管理器语义。**这一点对本文件特别重要**：`append()` 要同时写
        events 和 sessions 两处，必须一起成功或一起失败。
        """
        return await asyncio.to_thread(self._run_blocking, work)

    def _run_blocking(self, work: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._connect()
        try:
            with conn:
                return work(conn)
        finally:
            conn.close()

    # ────────────────────────────────────────────── 会话

    async def create(self) -> Session:
        session_id = uuid.uuid4().hex
        now = _now_iso()
        # 标题先占位，等第一条用户消息落盘时由 `_refresh_caches` 刷新成真的。
        placeholder = derive_title([])

        def work(conn: sqlite3.Connection) -> Session:
            conn.execute(
                "INSERT INTO sessions (id, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, placeholder, "idle", now, now),
            )
            return Session(
                id=session_id,
                title=placeholder,
                status="idle",
                busy=False,
                created_at=now,
                updated_at=now,
            )

        return await self._run(work)

    async def get(self, session_id: str) -> Session | None:
        def work(conn: sqlite3.Connection) -> Session | None:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return _row_to_session(row) if row else None

        return await self._run(work)

    async def list_recent(self, *, limit: int = 50) -> list[Session]:
        def work(conn: sqlite3.Connection) -> list[Session]:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_session(row) for row in rows]

        return await self._run(work)

    async def events(self, session_id: str) -> list[StoredEvent]:
        def work(conn: sqlite3.Connection) -> list[StoredEvent]:
            rows = conn.execute(
                "SELECT seq, created_at, payload FROM events "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
            return [
                StoredEvent(
                    seq=row["seq"],
                    created_at=row["created_at"],
                    event=parse_log_event(row["payload"]),
                )
                for row in rows
            ]

        return await self._run(work)

    async def delete(self, session_id: str) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            # events 靠 ON DELETE CASCADE 一起删掉 —— 前提是 foreign_keys
            # 已经打开（陷阱 ①）。test_session_store.py 里有一条测试专门守着
            # 这件事，因为忘了 PRAGMA 的后果是**完全静默**的。
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cursor.rowcount > 0

        return await self._run(work)

    # ────────────────────────────────────────────── 追加

    async def append(self, session_id: str, event: LogEvent) -> StoredEvent:
        """追加一条记录，并在**同一个事务里**刷新 title / status 两个缓存。"""
        now = _now_iso()
        payload = event.model_dump_json()

        def work(conn: sqlite3.Connection) -> StoredEvent:
            # seq 的分配和插入是**同一条语句**，所以是原子的。
            # 拆成「先 SELECT MAX(seq) 再 INSERT」的话，两条并发的流会读到
            # 同一个 max，然后一条撞主键失败 —— 而且失败的那条可能是先开始的。
            cursor = conn.execute(
                "INSERT INTO events (session_id, seq, kind, payload, created_at) "
                "VALUES (?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM events "
                "           WHERE session_id = ?), ?, ?, ?) "
                "RETURNING seq",
                (session_id, session_id, event.kind, payload, now),
            )
            seq = cursor.fetchone()[0]

            stored = StoredEvent(seq=seq, created_at=now, event=event)
            self._refresh_caches(conn, session_id, now)
            return stored

        return await self._run(work)

    def _refresh_caches(self, conn: sqlite3.Connection, session_id: str, now: str) -> None:
        """重算 title / status 两个派生缓存并写回。

        为什么不「增量更新」（比如「收到 decision_request 就把 status 置成
        awaiting_user」）？因为那会是 `derive_status` 的**第二份实现**，
        而两份实现迟早会漂移 —— 到时候真相和缓存对不上，排查的是缓存还是日志？
        说不清。

        这里是**直接重放全部事件**然后调用同一套纯函数，所以「缓存 == 真相」
        是构造上成立的，不是靠纪律维持的。

        代价是每次 append 都是 O(会话长度)。以这个产品的规模（一次会话几十到
        几百条事件）完全不值得优化。真到了需要的时候，正确做法是加一个
        增量版本 + 一条断言「它和全量重算结果一致」的测试，而不是把全量版本删掉。
        """
        rows = conn.execute(
            "SELECT seq, created_at, payload FROM events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        events = [
            StoredEvent(
                seq=row["seq"], created_at=row["created_at"], event=parse_log_event(row["payload"])
            )
            for row in rows
        ]
        conn.execute(
            "UPDATE sessions SET title = ?, status = ?, updated_at = ? WHERE id = ?",
            (derive_title(events), derive_status(events), now, session_id),
        )

    # ────────────────────────────────────────────── 租约

    async def acquire_lease(self, session_id: str, *, seconds: float = 120.0) -> str | None:
        """抢占会话。成功返回所有权令牌，已经有人占着且未过期时返回 None。

        整件事是一条带条件的 `UPDATE`，所以**原子性由数据库给**，
        不需要任何应用层的锁。这是把它放进数据库（而不是进程内的
        `asyncio.Lock`）的核心收益：多进程、多机都认这个租约。

        令牌在这一条语句里和 `lease_until` **一起**写进去 —— 不能拆成
        「先 UPDATE 再 SELECT」，那样两条并发请求会读到同一个令牌，
        等于令牌制度根本不存在。

        `lease_until` 存的是 unix 时间戳（REAL）而不是 ISO 字符串，
        因为比较要用数字。ISO 字符串的字典序比较**大体上**能用，
        但只要格式里出现过一次宽度不一致（比如省掉微秒）就会静默地比错 ——
        而租约比错的表现是「并发保护偶尔失灵」，最难查的那一类。
        """
        now = datetime.now(UTC)
        until = (now + timedelta(seconds=seconds)).timestamp()
        token = uuid.uuid4().hex

        def work(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE sessions SET lease_until = ?, lease_token = ?, updated_at = ? "
                "WHERE id = ? AND (lease_until IS NULL OR lease_until < ?)",
                (until, token, _now_iso(), session_id, now.timestamp()),
            )
            return cursor.rowcount == 1

        return token if await self._run(work) else None

    async def release_lease(self, session_id: str, *, token: str) -> None:
        """只释放令牌对得上的那条租约。

        令牌对不上时静默返回 —— 那不是错误，是「租约已经过期并被别人接管了」。

        ⚠️ 这个 `AND lease_token = ?` 就是整个所有权制度本身，别去掉。
        去掉以后的行为是：一个超过 TTL 的慢操作跑完时，会把**已经换手给别人**
        的租约放掉，于是第三个请求也能同时进来。见 `base.py` 里
        `acquire_lease` 的说明和 M6a 的 roadmap 记录。
        """

        def work(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE sessions SET lease_until = NULL, lease_token = NULL "
                "WHERE id = ? AND lease_token = ?",
                (session_id, token),
            )

        await self._run(work)

    async def clear_all_leases(self) -> int:
        """清空**所有**租约，不管属于谁。只在进程启动时调用。

        这是唯一一个合理地绕过所有权的地方：它要处理的正是「上一个进程
        被强杀，令牌再也回不来了」这种情况。
        """

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE sessions SET lease_until = NULL, lease_token = NULL"
            )
            return cursor.rowcount

        return await self._run(work)


def _row_to_session(row: sqlite3.Row) -> Session:
    """数据库行 → `Session`。

    `busy` 是**当场算的**，不是存储的一列：租约过没过期取决于「现在几点」，
    存下来就会过期得不及时（一条永远为 True 的 busy 比没有更糟）。
    """
    lease_until = row["lease_until"]
    busy = lease_until is not None and lease_until > datetime.now(UTC).timestamp()
    return Session(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        busy=busy,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SessionRecorder:
    """把 `TurnRecorder` 协议落到某个具体会话的事件日志上。

    这一层薄得几乎什么都不做（每次都转发给 store），但它存在的意义是
    **让 Agent 循环完全不知道存储的存在**：loop 只看到一个
    `assistant_round / tool_result / decision_requested` 的接口，
    不需要 import sqlite、不需要知道事件叫什么名字。
    """

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id

    async def assistant_round(self, message: Message) -> None:
        await self._store.append(self._session_id, LogAssistant(message=message))

    async def tool_result(self, message: Message, result: ToolResult) -> None:
        await self._store.append(self._session_id, LogTool(message=message, result=result))

    async def usage(
        self, *, prompt_tokens: int, completion_tokens: int, finish_reason: str
    ) -> None:
        await self._store.append(
            self._session_id,
            LogUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                finish_reason=finish_reason,
            ),
        )

    async def decision_requested(self, request: DecisionRequest) -> None:
        await self._store.append(
            self._session_id,
            LogDecisionRequest(
                call_id=request.call_id,
                question=request.question,
                options=request.options,
                allow_free_text=request.allow_free_text,
            ),
        )
