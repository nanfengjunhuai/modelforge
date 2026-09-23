"""会话状态：事件日志的事件类型，以及两个协议。

════════════════════════════════════════════════════════════════════════
这个文件回答的问题：一次会话的「状态」到底存在哪
════════════════════════════════════════════════════════════════════════

ADR-002 定下了「状态落盘 + 点击时重建」。但「状态」这个词有两个候选形态：

  ① 快照 —— 每次节点跑完，把当前的完整对话历史覆盖写一行。
     恢复 = 读那一行。简单、快、够用。

  ② 事件日志 —— 每一步 append 一条**不可变**记录，当前状态是重放出来的。

这里选的是 ②。理由是**回不去了**：快照只能回答「现在是什么」，
日志能回答「当时为什么」——而后者才是这个产品的价值。数模工作流的高频动作是
「换个模型再算一遍对比」，那需要能回到任意决策点重来，而这在快照模型下
等于要重建整个历史。日志模型下它天然成立。

**代价写在明处**：日志只能 append，所以将来的「回退」必须是
「从某条 decision_answer 之前截断，且连同其后所有事件一起丢」。
截一半会得到一段非法历史。这条在 ADR-009 里有完整说明。

════════════════════════════════════════════════════════════════════════
三种「事件」，别搞混
════════════════════════════════════════════════════════════════════════

    ProviderEvent   五种    Provider 能产出的（providers/events.py）
    StreamEvent     七种    这条流上会出现的 = 上面五种 + ToolResult + DecisionRequest
    LogEvent        七种    会被**写进日志**的 ← 本文件

三者是不同的问题：前两个是「线上传什么」，第三个是「盘上存什么」。
StreamEvent 里 `text_delta` 和 `tool_call_delta` 是碎片，**不落盘**——
它们累积完之后由 `assistant` 一条记录代替。反过来 `usage` 不投影进对话历史，
但它落盘（M5 做成本核算要用）。

因为回答的问题不同，这里刻意用 `kind` 而不是 `type` 当判别子：
看到 `type` 就知道是流事件，看到 `kind` 就知道是日志记录。

════════════════════════════════════════════════════════════════════════
最容易写错的一处：`assistant` / `tool` 存的是**冻结的**消息
════════════════════════════════════════════════════════════════════════

`tool` 记录里同时存了 `message`（给模型的、已经渲染好的文本）和 `result`
（给界面回放的原始结构）。看起来冗余，但两者读者不同，**且重放必须确定**。

如果只存 `result`、投影时现调 `format_for_model`，会出两个问题：

  · M5 给 `format_for_model` 加产物、图片、或者只是改一句措辞 ——
    所有历史会话「模型当时看到的内容」会被**追溯性改写**。
    日志的全部意义就是忠实，一旦能被后来者改写就不是日志了。
  · `assistant` 里的 `arguments` 同理：必须存过了 `_safe_arguments()` 的版本。
    否则一次 max_tokens 截断留下的坏 JSON 会在恢复后让第一个请求直接 400。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol, get_args, runtime_checkable

from pydantic import BaseModel, Field, TypeAdapter

from modelforge.providers.base import Message
from modelforge.providers.events import DecisionRequest, ToolResult

__all__ = [
    "LOG_EVENT_KINDS",
    "DiscardRecorder",
    "LogAborted",
    "LogAssistant",
    "LogDecisionAnswer",
    "LogDecisionRequest",
    "LogEvent",
    "LogTool",
    "LogUsage",
    "LogUser",
    "Session",
    "SessionStatus",
    "SessionStore",
    "StoredEvent",
    "TurnRecorder",
    "parse_log_event",
]


# ══════════════════════════════════════════════════════ 会话

SessionStatus = Literal["idle", "awaiting_user"]
"""会话对「用户」而言的状态。

  · idle          —— 没有待办，可以发新消息
  · awaiting_user —— 有一个问题在等用户拍板

⚠️ **这里没有 `running`。** 「有一条流正在跑」是**进程的瞬时属性**，
从日志里推不出来 —— 它由 `Session.busy`（租约）表达，见下面的说明。
把这两件事塞进同一个字段会得到一个永远不自洽的状态机。
"""


class Session(BaseModel):
    """一个会话的元信息。不含对话内容 —— 那些在事件日志里。"""

    id: str
    title: str
    status: SessionStatus
    """派生值的**缓存**。真相是事件日志，这个字段是为了列表页不用重放所有事件。"""

    busy: bool = False
    """此刻有没有一条流正在这个会话上跑（数据库租约未过期）。

    刻意和 `status` 分开：`status` 是**日志的投影**（可重算、可断言一致），
    `busy` 是**进程的瞬时属性**（随流的开始和结束变化，日志里没有痕迹）。
    两者混在一个字段里的话，「status 缓存是否等于重算结果」这条测试
    在流的整个持续期间都是假的，只能写成自欺欺人的形式。
    """

    created_at: str
    updated_at: str


class StoredEvent(BaseModel):
    """日志里的一行：一个事件 + 它在会话中的位置。"""

    seq: int
    """会话内从 0 递增。前端拿它当渲染 key，将来的「回退」也靠它定位。"""

    created_at: str
    event: LogEvent


# ══════════════════════════════════════════════════════ 七种日志事件


class LogUser(BaseModel):
    """用户说的一句话。"""

    kind: Literal["user"] = "user"
    content: str


class LogAssistant(BaseModel):
    """**一轮完整的模型生成**。

    注意这里存的是「一轮」，不是「一个碎片」。线上传的 `text_delta` /
    `tool_call_delta` 到了这里已经被累积成一条完整的消息 ——
    日志记的是「发生了什么」，不是「字节是怎么到达的」。

    `message` 直接就是 OpenAI 格式的 assistant 消息（ADR-003 的回报：
    这里不需要任何转换），而且**可能是最终回答，也可能是中间的工具调用轮**——
    两者在格式上没区别，靠有没有 `tool_calls` 区分。
    """

    kind: Literal["assistant"] = "assistant"
    message: Message


class LogTool(BaseModel):
    """一次工具执行的结果。"""

    kind: Literal["tool"] = "tool"
    message: Message
    """回填给模型的那条 `{"role": "tool", ...}` 消息 —— **冻结**的。

    为什么要把渲染好的文本存下来，而不是存 `result` 到时候再渲染？
    见模块注释末尾：重放必须是确定的。
    """

    result: ToolResult
    """原始执行结果，只给界面回放用（stdout / stderr / 耗时 / 退出码）。"""


class LogDecisionRequest(BaseModel):
    """Agent 在决策点停下来，把候选方案摆给用户。

    **这条不投影进对话历史** —— 模型看到的是自己在 assistant 消息里
    发出的那次 `ask_user` 工具调用（含原始 arguments），这就够了。
    这条记录是给**人和审计**看的：结构化、经过校验、不依赖解析模型生成的 JSON。
    """

    kind: Literal["decision_request"] = "decision_request"
    call_id: str
    """和 `assistant.message.tool_calls[].id` 对应，是把它和答案配起来的钥匙。"""

    question: str
    options: list[str]
    allow_free_text: bool = True


class LogDecisionAnswer(BaseModel):
    """用户拍板了。**这条投影进对话历史**（变成一条 tool 消息）。

    和 `LogDecisionRequest` 的不对称是刻意的：请求不投影（模型本来就知道
    自己问了什么），答案必须投影（模型不知道用户选了什么）。
    """

    kind: Literal["decision_answer"] = "decision_answer"
    call_id: str
    choice: str
    note: str = ""


class LogUsage(BaseModel):
    """一轮生成的 token 用量。

    落盘但不投影 —— M5 要做成本核算和「这个会话花了多少钱」。
    现在不落的话，那些数字就永远丢了。
    """

    kind: Literal["usage"] = "usage"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"


class LogAborted(BaseModel):
    """流在跑到一半时被中断（用户刷新页面、点停止、或者后端起停）。

    必须记。否则日志会停在「用户问了一句，之后什么都没有」的状态，
    而后人（包括将来的自己）完全没法区分这是「还没跑完」还是「跑砸了」。
    """

    kind: Literal["aborted"] = "aborted"
    reason: str = ""


# ══════════════════════════════════════════════════════ 判别联合


LogEvent = Annotated[
    LogUser
    | LogAssistant
    | LogTool
    | LogDecisionRequest
    | LogDecisionAnswer
    | LogUsage
    | LogAborted,
    Field(discriminator="kind"),
]
"""会被写进日志的七种记录。

和 StreamEvent 一样用判别联合 —— 但要强调的是，**这两个联合的成员列表不同，
而且是应该不同**：线上的碎片不落盘，落盘的用量不在线上。
"""

# `get_args` 在 Annotated 外面那层返回 `(联合本身, FieldInfo)`，再对联合本身
# 调一次才拿到各个成员。这个嵌套是 Pydantic 判别联合的实现细节，
# 所以这里就地展开一次，别让它漏到使用方去。
_LOG_UNION = get_args(LogEvent)[0]

LOG_EVENT_KINDS = frozenset(
    member.model_fields["kind"].default for member in get_args(_LOG_UNION)
)
"""七种 kind 的取值集合。

给跨语言契约测试用：它会拿这个集合去比对 `web/src/lib/log-types.ts`。
（TS 无法 import Python，所以只能这样土法比对 —— 见 tests/test_event_contract.py。）
"""

_LogEventAdapter: TypeAdapter[LogEvent] = TypeAdapter(LogEvent)


def parse_log_event(payload: str | bytes | dict[str, Any]) -> LogEvent:
    """把存下来的 JSON 还原成一个 LogEvent。

    封装成一个函数而不是让各处直接 `TypeAdapter(LogEvent).validate_json`，
    是为了让「反序列化失败时该怎么办」只有一个答案。目前的答案是**直接抛**：
    一条读不出来的日志记录是数据损坏，静默跳过它会让对话历史悄悄缺一块，
    而缺一块的历史比报错难查得多。
    """
    if isinstance(payload, (str, bytes)):
        return _LogEventAdapter.validate_json(payload)
    return _LogEventAdapter.validate_python(payload)


# ══════════════════════════════════════════════════════ 存储协议


@runtime_checkable
class SessionStore(Protocol):
    """会话与事件日志的持久化。

    和 `CodeExecutor` 是同一个套路：上层只依赖协议，换存储（SQLite → Postgres）
    不用改任何调用方。

    注意所有方法都是 async —— 尽管 SQLite 驱动是阻塞的。这不是假装，
    而是因为**实现方要在内部用 `asyncio.to_thread` 把阻塞调用甩出去**，
    接口必须是异步的，实现才有地方做这件事。
    """

    async def create(self) -> Session:
        """建一个空会话。

        注意**没有 title 参数** —— 标题是从事件日志派生的（第一条用户消息），
        而建会话的那一刻还没有任何消息。见 `project.derive_title` 的说明。
        """
        ...

    async def get(self, session_id: str) -> Session | None:
        """取会话元信息。不存在返回 None（而不是抛异常）——
        「没有这个东西」是调用方要用 `if` 处理的分支，不是异常情况。"""
        ...

    async def list_recent(self, *, limit: int = 50) -> list[Session]: ...

    async def events(self, session_id: str) -> list[StoredEvent]: ...

    async def append(self, session_id: str, event: LogEvent) -> StoredEvent:
        """追加一条记录。

        **实现方必须在同一个事务里重算并写入 `sessions.status`** ——
        「先 append 再 set_status」两次写在中间断电就会得到缓存与真相分叉的状态。
        """
        ...

    async def delete(self, session_id: str) -> bool: ...

    # ── 租约（并发控制） ──────────────────────────────
    #
    # 为什么用数据库租约而不是进程内的 asyncio.Lock？
    # 因为 ADR-002 拒绝进程内挂起，理由就是「服务重启丢进度、无法水平扩展」。
    # 用一个只在单进程内有效的锁去保护一个跨进程的状态，是把那个理由
    # 原样搬了回来。租约写在数据库里，多进程（甚至多机）都认。

    async def acquire_lease(self, session_id: str, *, seconds: float) -> bool:
        """尝试独占一个会话。已经被人占着（且租约未过期）时返回 False。

        返回布尔而不是抛异常：抢不到锁是**正常的并发结果**，不是错误。
        抛异常会逼调用方写 try/except 去表达一个 if。
        """
        ...

    async def release_lease(self, session_id: str) -> None: ...

    async def clear_all_leases(self) -> int:
        """清空所有租约，返回清掉的条数。**进程启动时必须调用。**

        不调用的话，上次进程被杀（而不是优雅退出）时留下的租约永远不会过期到
        能被抢走的程度……不，租约本身有 TTL 会过期。真正的问题是**开发期**：
        热重载频繁重启，每次重启前那个会话都还挂着租约，用户在 TTL 内
        会看到一个莫名其妙的 409。启动时清一遍最省事。
        """
        ...


# ══════════════════════════════════════════════════════ 录制协议


@runtime_checkable
class TurnRecorder(Protocol):
    """Agent 循环把「跑的过程中发生了什么」报告出去。

    ════════════════════════════════════════════════════════════════
    为什么是 Protocol，以及为什么参数类型都是领域事件
    ════════════════════════════════════════════════════════════════
    `loop.py` 不该知道存储是 SQLite 还是文件还是别的 —— 所以它只依赖这个协议。
    更关键的是三个方法的参数类型（`Message` / `ToolResult` / `DecisionRequest`）
    **都是领域事件，不是存储行**：loop 不需要知道这些会被落成 `LogAssistant`
    还是别的什么，那是实现方的事。存储 schema 因此不会渗进循环里。

    ════════════════════════════════════════════════════════════════
    为什么 `run_agent_turn` 的 recorder 参数没有默认值
    ════════════════════════════════════════════════════════════════
    给一个 NoOp 默认值的话，会话端点里忘了传就会「一切正常，只是刷新之后
    什么都没有」—— 而「刷新不丢」恰恰是这个里程碑唯一的验收标准。
    必填参数能让 mypy 在调用点就抓住这个遗漏。

    代价是无状态的 `/api/chat/stream` 要显式传一个空的实现 —— 那正好，
    让「这个端点不落盘」这件事在代码里看得见。
    """

    async def assistant_round(self, message: Message) -> None:
        """一轮模型生成跑完了。

        ⚠️ **每轮都调，包括最后那一轮。** 这件事的重要性值得展开：

        循环有三个出口（正常结束 / 中断等用户 / 轮数用尽后收尾），
        最初的实现只在「要调工具」那条分支里把 assistant 消息写进历史，
        因为 M2/M3 的前端每次重发全量历史，所以看不出来。
        但 M4 的历史来自日志投影，于是最后一条回答**从来没被存下来**：

            用户：帮我算个和
            蒟蒻：5050（← 这句话不在日志里）
            用户：那乘 2 呢？  → 模型看到的是「我调了个工具，然后用户又问了新问题」
            刷新页面 → 刚才那句回答凭空消失

        所以实现上不在三个出口各写一次，而是把它提到分支**之前**无条件执行。
        「N 轮生成 → N 次 assistant_round」因此成为一条可离线断言的不变式。
        """
        ...

    async def tool_result(self, message: Message, result: ToolResult) -> None:
        """一个工具跑完了。

        `message` 是回填给模型的那条 `{"role":"tool",...}` 行（**已渲染**），
        `result` 是原始执行结果（给界面回放）。
        """
        ...

    async def usage(
        self, *, prompt_tokens: int, completion_tokens: int, finish_reason: str
    ) -> None:
        """这一轮花了多少 token。

        为什么值得占一个协议方法？因为这些数字**不记就永远丢了**。
        M5 要做成本核算（「这道题我一共花了多少钱」），而那时候再去补，
        已经跑过的会话全都没有数据。这是少数几个「现在不做以后补不回来」的东西。

        它和 `assistant_round` 是同一轮的两面：一个是产出，一个是成本。
        """
        ...

    async def decision_requested(self, request: DecisionRequest) -> None:
        """Agent 停下来了，在等用户拍板。

        调用它之后循环会立刻结束本轮 —— 所以这必须是**最后一条**写进去的记录。
        """
        ...


class DiscardRecorder:
    """什么都不做的 TurnRecorder —— 给无状态的 `/api/chat/stream` 用。

    为什么不给 `run_agent_turn` 的 recorder 参数一个默认值、省掉这个类？
    见上面「为什么没有默认值」。一句话：默认值会让「忘了传」变成一个
    静默的、只在刷新页面时才暴露的 bug，而这个里程碑的验收标准就是刷新不丢。
    让调用方显式写下 `DiscardRecorder()` 的代价只有一个词，
    换来的是「哪些路径不落盘」在代码里一眼可见。
    """

    async def assistant_round(self, message: Message) -> None:
        pass

    async def tool_result(self, message: Message, result: ToolResult) -> None:
        pass

    async def usage(
        self, *, prompt_tokens: int, completion_tokens: int, finish_reason: str
    ) -> None:
        pass

    async def decision_requested(self, request: DecisionRequest) -> None:
        pass
