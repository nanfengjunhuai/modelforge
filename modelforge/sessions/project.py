"""把事件日志**投影**成「下一步要发给模型的历史」。

════════════════════════════════════════════════════════════════════════
为什么要有这一层
════════════════════════════════════════════════════════════════════════
日志是「发生过什么」的忠实记录 —— 碎片、工具结果、用户的每一次拍板、
token 用量，全都按真实顺序躺在那里。

而发给模型的必须是**另一回事**：OpenAI 格式的 message 列表，
没有碎片、没有决策卡片、没有用量统计。

投影就是从前者算出后者的那个纯函数。它没有 I/O、不碰数据库，
所以可以用手写的事件列表**穷举测试** —— 这正是它值得单独成一层的原因。

    events（七种 kind）  ──project_messages──►  list[Message]  ──►  Provider

════════════════════════════════════════════════════════════════════════
`project_messages` **必须是全函数**（这是本文件最重要的设计）
════════════════════════════════════════════════════════════════════════
OpenAI 协议有一条硬约束：assistant 消息里的每个 `tool_calls[].id`，
后面**必须**跟一条 `tool_call_id` 相同的 tool 消息。少一条，多数服务直接 400。

所以一个「遍历事件、按顺序 append」的天真实现是**会炸的** ——
只要有任何一条路径写进了一份没有结果的工具调用（中断、崩溃、
将来某个新加的流程忘了配对），整个会话就再也发不出去请求了。

这里的做法是两遍扫描：

    第一遍  把所有能找到的结果按 call_id 收进一张表
    第二遍  顺着事件走，遇到 assistant 就**照着它自己的 tool_calls 顺序**
            逐个查表；查不到就合成一条「未执行」的 tool 消息补上

两遍扫描换来两个性质：

  · **顺序天然正确** —— 补出来的 tool 消息位置和 tool_calls 里的顺序一致，
    而不是「谁先落盘谁排前面」。
  · **缺结果不会污染历史** —— 兜底在投影层，而不是「写入方的纪律」。
    写入方（Agent 循环）将来多一个中断路径忘了配对，历史依然是合法的。

换句话说：**把「历史永远合法」这件事做成一个纯函数的性质，而不是一条代码规范。**
规范会被人忘掉，函数不会。
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from modelforge.agents.prompts import SYSTEM_PROMPT
from modelforge.providers.base import Message
from modelforge.sessions.base import (
    LogAssistant,
    LogDecisionAnswer,
    LogDecisionRequest,
    LogEvent,
    LogTool,
    LogUser,
    SessionStatus,
    StoredEvent,
)

__all__ = [
    "DEFAULT_TITLE",
    "Decision",
    "build_messages",
    "derive_status",
    "derive_title",
    "list_decisions",
    "pending_decision",
    "project_messages",
]

# 补出来的 tool 消息长什么样。
#
# 措辞是写给**模型**看的，所以要说清楚三件事：这不是你的代码的错、
# 大概率是上次运行被打断了、以及你可以怎么办。含糊的一句话会让模型
# 以为工具坏了，然后开始反复重试一个根本没坏的工具。
_MISSING_RESULT = (
    "【这次工具调用没有留下结果】上次运行可能在它跑完之前就被中断了"
    "（比如页面被关掉、服务重启）。如果还需要这个结果，请重新调用一次；"
    "不需要的话就跳过它继续。"
)


def _unwrap(item: StoredEvent | LogEvent) -> LogEvent:
    """允许调用方传 `StoredEvent` 或裸的 `LogEvent`。

    存在的理由很实际：存储层给的是 `StoredEvent`（带 seq），
    而测试里手写一段事件序列时，再套一层 `StoredEvent(seq=..., ...)` 是纯噪音。
    投影只关心事件内容，不关心它在哪一行。
    """
    return item.event if isinstance(item, StoredEvent) else item


def _answer_content(event: LogDecisionAnswer) -> str:
    """把用户的拍板渲染成给模型读的一句话。

    注意带上 `note`：用户选了「A」但在备注里写了「不过我们组没做过 AHP」，
    后半句对模型同样重要 —— 丢掉它等于丢掉了半条信息。
    """
    text = f"用户的选择：{event.choice}"
    if event.note.strip():
        text += f"\n用户的补充说明：{event.note.strip()}"
    return text


def _answer_message(event: LogDecisionAnswer) -> Message:
    return {
        "role": "tool",
        "tool_call_id": event.call_id,
        "content": _answer_content(event),
    }


def project_messages(items: Sequence[StoredEvent | LogEvent]) -> list[Message]:
    """把事件日志重建成可以发给模型的对话历史。

    ⚠️ **只在「可以发请求」的状态下调用它。** 具体说：会话必须是 `idle`。

    在 `awaiting_user` 状态下投影，那条待答的 `ask_user` 调用会因为「还没有
    结果」而被补成一句【这次工具调用没有留下结果】——**语义是错的**
    （它不是在等结果，它是在等你），而且真发出去会让模型以为工具坏了。
    API 层用 `derive_status(...) == "idle"` 把这道门，见 `api/sessions.py`。

    这一层不做这个判断，是因为投影是纯函数、不该知道「现在能不能发请求」——
    那是调用方的处境，不是数据的内容。

    Args:
        items: 事件序列，**必须按 seq 升序**（存储层保证）。

    Returns:
        OpenAI 格式的消息列表。保证合法：每个 tool_call 都有对应的 tool 消息。
    """
    events = [_unwrap(item) for item in items]

    # ── 第一遍：把所有能找到的「工具结果」按 call_id 收起来 ──
    #
    # 这里刻意用「后写的覆盖先写的」—— 正常流程下一个 call_id 只会有一份结果，
    # 真出现重复说明有 bug，让最后一次胜出比抛异常温和，而投影层不该
    # 因为数据里的意外就直接崩掉整个会话。
    results: dict[str, Message] = {}
    for event in events:
        if isinstance(event, LogTool):
            call_id = event.message.get("tool_call_id")
            if isinstance(call_id, str):
                results[call_id] = event.message
        elif isinstance(event, LogDecisionAnswer):
            results[event.call_id] = _answer_message(event)

    # ── 第二遍：顺着事件走，顺序输出 ──
    messages: list[Message] = []
    for event in events:
        if isinstance(event, LogUser):
            messages.append({"role": "user", "content": event.content})

        elif isinstance(event, LogAssistant):
            messages.append(dict(event.message))
            # 照**这条消息自己**的 tool_calls 顺序补齐结果。
            # 不这么做的话，补出来的 tool 消息会按落盘先后排列，
            # 和 tool_calls 的顺序对不上 —— 多数实现按 id 匹配所以能过，
            # 但没必要去赌对端的宽松程度。
            for call in event.message.get("tool_calls") or []:
                call_id = call.get("id")
                messages.append(
                    results.get(call_id)
                    or {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _MISSING_RESULT,
                    }
                )

        # LogTool / LogDecisionAnswer 在第二遍里**故意跳过**：
        # 它们已经在上面跟着所属的 assistant 消息一起输出了。
        # LogDecisionRequest / LogUsage / LogAborted 同理 —— 它们不投影成消息
        # （前者的信息在 assistant 的 tool_calls 里，后两者根本不属于对话内容）。
    return messages


def build_messages(
    items: Sequence[StoredEvent | LogEvent], *, system_prompt: str | None = None
) -> list[Message]:
    """投影 + 注入人设 —— **所有发给模型的请求都必须经过这里**。

    ════════════════════════════════════════════════════════════════
    为什么要收敛成一个函数，而不是在各处 project 完之后自己插 system
    ════════════════════════════════════════════════════════════════
    因为漏掉它会产生一个**静默的行为退化**，而且只在恢复之后才出现：

        第一轮：一切正常，蒟蒻会问问题、会跑代码
        中断 → 用户拍板 → 恢复
        第二轮：人设没了 → 模型变成了一个普通的问答助手，
                不再停下来问问题、不再用工具

    而界面上没有任何异常 —— 只是「它好像变笨了」。这种 bug 极难定位，
    因为你会去怀疑提示词、怀疑模型、怀疑温度参数，唯独不会想到
    「恢复路径少插了一条 system 消息」。

    抽成一个函数 + 一条断言「恢复后的请求里第一条是 system」，
    这类问题就没有藏身之处了。
    """
    messages = project_messages(items)
    # 已经带了 system 就不重复插 —— 投影出来的历史里本来就可能有一条
    # （比如用户自己传了）。
    if not any(message.get("role") == "system" for message in messages):
        messages.insert(
            0, {"role": "system", "content": system_prompt or SYSTEM_PROMPT}
        )
    return messages


# ══════════════════════════════════════════════════════ 决策视图


class Decision(BaseModel):
    """一次「Agent 问、用户答」的完整记录。**给界面看的**，不是给模型的。

    注意 `choice is None` 表示**还等着用户拍板**。
    已答的决策也保留在列表里（而不是答完就消失）——
    刷新页面之后用户要能看到「我刚才选的是什么」，否则那段历史就凭空没了，
    而模型还记得，界面上看起来像是漏了一件事。
    """

    call_id: str
    question: str
    options: list[str]
    allow_free_text: bool
    choice: str | None = None
    note: str = ""
    seq: int
    """请求所在的行号。前端拿它当 React key（call_id 也可以，但 seq 更稳定）。"""


def list_decisions(items: Sequence[StoredEvent | LogEvent]) -> list[Decision]:
    """按时间顺序列出所有决策，含已答的和待答的。"""
    events = [_unwrap(item) for item in items]

    answers: dict[str, LogDecisionAnswer] = {}
    for event in events:
        if isinstance(event, LogDecisionAnswer):
            answers[event.call_id] = event

    decisions: list[Decision] = []
    for index, event in enumerate(events):
        if not isinstance(event, LogDecisionRequest):
            continue
        answer = answers.get(event.call_id)
        decisions.append(
            Decision(
                call_id=event.call_id,
                question=event.question,
                options=event.options,
                allow_free_text=event.allow_free_text,
                choice=answer.choice if answer else None,
                note=answer.note if answer else "",
                seq=index,
            )
        )
    return decisions


def pending_decision(items: Sequence[StoredEvent | LogEvent]) -> Decision | None:
    """返回那个还在等用户拍板的决策，没有就返回 None。

    语义上等价于 `next((d for d in list_decisions(...) if d.choice is None), None)`，
    单独写一个是因为它有两个读者（API 的状态校验、前端的高亮），
    而「待答」这个筛选条件散在两处容易写出不一致的版本。
    """
    for decision in list_decisions(items):
        if decision.choice is None:
            return decision
    return None


DEFAULT_TITLE = "新会话"


def derive_title(items: Sequence[StoredEvent | LogEvent], *, limit: int = 30) -> str:
    """会话标题 = 第一条用户消息的开头。没有消息时给个占位。

    为什么标题也是**派生**的、而不是「建会话时填一次」？
    因为建会话的那一刻还没有任何消息 —— `POST /api/sessions` 拿不到标题。
    要么允许那列为空（于是列表页要处理 null），要么事后补写一次
    （于是「谁负责补、补完覆盖不覆盖用户改的名字」变成一堆 if）。

    派生就没有这些问题：真相是日志，标题永远等于「第一条用户消息」，
    和 `derive_status` 走同一套机制（append 时在同一事务里刷新缓存）。
    """
    for item in items:
        event = _unwrap(item)
        if not isinstance(event, LogUser):
            continue
        # 折叠换行和连续空格：用户的第一句常常是多行的，直接截前 30 个字符
        # 会把一个换行符留在标题里，列表页看起来像坏了。
        text = " ".join(event.content.split())
        if not text:
            continue
        return text if len(text) <= limit else text[:limit] + "…"
    return DEFAULT_TITLE


def derive_status(items: Sequence[StoredEvent | LogEvent]) -> SessionStatus:
    """从日志推出现在该是 idle 还是 awaiting_user。

    ⚠️ 这是**真相**，`sessions.status` 那一列只是它的缓存。
    两者的关系有一条测试守着（`test_session_store.py`），
    缓存和重算结果对不上就会红。

    ⚠️ 注意这里推不出「有一条流正在跑」——那是进程的瞬时属性，
    由租约（`Session.busy`）表达，刻意不混进来。原因见 `base.SessionStatus`。
    """
    return "awaiting_user" if pending_decision(items) is not None else "idle"
