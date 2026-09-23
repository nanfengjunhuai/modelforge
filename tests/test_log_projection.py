"""投影层的测试 —— 全部离线，纯函数。

`project_messages` 是整个 M4 里最值得单独测的一块，因为它是**唯一一处**
「历史合法性」被保证的地方：

    OpenAI 协议要求 assistant 消息里每个 tool_calls[].id，后面必须跟一条
    tool_call_id 相同的 tool 消息。少一条，多数服务直接 400。

而「少一条」在真实运行里太容易发生了（中断、崩溃、将来某个新加的流程忘了
配对）。所以设计上把兜底做成了**投影函数的性质** —— 遍历 assistant 自己的
tool_calls、逐个查结果、查不到就补一条。

这个文件做的就是把那段逻辑用各种畸形的事件序列轰一遍。
"""

from __future__ import annotations

import pytest

from modelforge.agents.prompts import SYSTEM_PROMPT
from modelforge.providers.events import ToolResult
from modelforge.sessions.base import (
    LogAborted,
    LogAssistant,
    LogDecisionAnswer,
    LogDecisionRequest,
    LogTool,
    LogUsage,
    LogUser,
    StoredEvent,
)
from modelforge.sessions.project import (
    DEFAULT_TITLE,
    build_messages,
    derive_status,
    derive_title,
    list_decisions,
    pending_decision,
    project_messages,
)

# ══════════════════════════════════════════════════════ 造事件的帮手


def assistant(*, text: str = "", calls: list[tuple[str, str]] | None = None) -> LogAssistant:
    """构造一条 assistant 记录。`calls` 是 [(call_id, 工具名), ...]。"""
    message: dict[str, object] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for call_id, name in calls
        ]
    return LogAssistant(message=message)


def tool_done(call_id: str, *, content: str = "5050", name: str = "run_python") -> LogTool:
    return LogTool(
        message={"role": "tool", "tool_call_id": call_id, "content": content},
        result=ToolResult(call_id=call_id, name=name, ok=True, stdout=content, exit_code=0),
    )


def asked(
    call_id: str, *, question: str = "选哪个？", options: list[str] | None = None
) -> LogDecisionRequest:
    return LogDecisionRequest(
        call_id=call_id, question=question, options=options or ["A", "B"]
    )


def answered(call_id: str, *, choice: str = "A", note: str = "") -> LogDecisionAnswer:
    return LogDecisionAnswer(call_id=call_id, choice=choice, note=note)


# ══════════════════════════════════════════════════════ 基本投影


def test_plain_conversation_projects_to_alternating_messages():
    events = [
        LogUser(content="你好"),
        assistant(text="你好，我是蒟蒻。"),
        LogUser(content="帮我算个数"),
        assistant(text="好的。"),
    ]

    messages = project_messages(events)

    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "你好"
    assert messages[2]["content"] == "帮我算个数"


def test_assistant_without_tool_calls_has_no_tool_calls_key():
    """**没有工具调用时不能输出 `"tool_calls": []`。**

    协议要求这个字段要么不存在、要么是非空数组。空数组是非法消息 ——
    而且它只在「模型单纯说了句话」的轮次出现，也就是绝大多数轮次。

    M4 之前这个问题不存在（因为那几轮压根不落盘），现在每一轮都落盘了，
    所以这条在真实对话里是**必然**会撞上的。
    """
    messages = project_messages([assistant(text="就这些。")])

    assert "tool_calls" not in messages[0]


def test_usage_and_aborted_events_are_not_projected():
    """用量统计和中断标记不是对话内容，不该出现在发给模型的历史里。

    它们落盘是为了别的目的（成本核算、以及让后人能区分「还没跑完」
    和「跑砸了」），投影的时候要跳过。
    """
    events = [
        LogUser(content="你好"),
        assistant(text="你好。"),
        LogUsage(prompt_tokens=10, completion_tokens=2, finish_reason="stop"),
        LogAborted(reason="客户端断开连接"),
    ]

    assert [m["role"] for m in project_messages(events)] == ["user", "assistant"]


# ══════════════════════════════════════════════════════ 工具配对（核心）


def test_tool_calls_are_paired_with_their_results():
    events = [
        LogUser(content="算一下"),
        assistant(text="我来跑个代码。", calls=[("c1", "run_python")]),
        tool_done("c1", content="5050"),
        assistant(text="答案是 5050。"),
    ]

    messages = project_messages(events)

    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[2]["tool_call_id"] == "c1"
    assert messages[2]["content"] == "5050"


def test_every_tool_call_gets_a_message_even_when_the_result_is_missing():
    """**本文件最重要的一条。**

    有 tool_calls 却没有对应结果时，投影必须**补一条**，而不是照原样输出。
    照原样输出的话，下一次请求会被服务端直接 400 —— 而这条路径在真实运行里
    太容易走到了：用户刷新页面、进程被杀、将来某个新加的中断流程忘了配对。

    所以兜底放在**投影层**（一个纯函数）而不是「写入方的纪律」。
    规范会被人忘掉，函数不会。
    """
    events = [
        LogUser(content="算一下"),
        assistant(text="我跑个代码。", calls=[("c1", "run_python")]),
        # c1 的结果丢了 —— 进程在这一刻被杀
        LogUser(content="在吗？"),
    ]

    messages = project_messages(events)

    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert "没有留下结果" in tool_messages[0]["content"]
    # 而且这个补齐不影响后面的对话
    assert messages[-1]["content"] == "在吗？"


def test_missing_results_follow_the_assistant_own_call_order():
    """补齐的消息要跟着 **assistant 自己的 tool_calls 顺序**，不是落盘顺序。

    这个区别只在「并行工具调用」时看得出来，但很重要：按落盘顺序补的话，
    补出来的 tool 消息排列和 assistant 里声明的顺序不一致。
    多数实现按 id 匹配所以能过 —— 但没必要去赌对端的宽松程度。
    """
    events = [
        assistant(calls=[("c_b", "run_python"), ("c_a", "run_python")]),
        # 只有第二个的结果回来了
        tool_done("c_a", content="A 的结果"),
    ]

    tool_messages = [m for m in project_messages(events) if m["role"] == "tool"]

    assert [m["tool_call_id"] for m in tool_messages] == ["c_b", "c_a"]
    assert "没有留下结果" in tool_messages[0]["content"]
    assert tool_messages[1]["content"] == "A 的结果"


def test_orphan_tool_results_are_ignored():
    """结果找不到对应的调用时静默丢掉。

    孤儿数据不该让投影崩掉 —— 一次投影失败意味着**整个会话再也打不开**。
    宁可少一条，也不要整段历史不可用。
    """
    events = [
        LogUser(content="你好"),
        tool_done("call_that_nobody_made", content="野生的结果"),
    ]

    assert [m["role"] for m in project_messages(events)] == ["user"]


# ══════════════════════════════════════════════════════ 决策


def test_decision_answer_becomes_a_tool_message():
    events = [
        assistant(text="我看有两个方向。", calls=[("c_ask", "ask_user")]),
        asked("c_ask", question="用哪种赋权方法？", options=["熵权法", "AHP"]),
        answered("c_ask", choice="熵权法"),
    ]

    messages = project_messages(events)

    assert [m["role"] for m in messages] == ["assistant", "tool"]
    assert messages[1]["tool_call_id"] == "c_ask"
    assert "熵权法" in messages[1]["content"]


def test_decision_note_is_carried_into_the_history():
    """用户写在备注里的补充说明必须一起给模型。

    用户选了「A」但备注「我们组没做过 AHP」—— 后半句对模型同样重要。
    丢掉它等于丢掉了半条信息，而模型会据此做出一个用户其实不想要的选择。
    """
    events = [
        assistant(calls=[("c_ask", "ask_user")]),
        asked("c_ask"),
        answered("c_ask", choice="A", note="但我们组没做过 AHP"),
    ]

    content = project_messages(events)[-1]["content"]

    assert "A" in content
    assert "没做过 AHP" in content


def test_decision_request_itself_is_not_projected():
    """决策请求**不**投影成消息 —— 模型本来就知道自己问了什么。

    它看到的是自己在 assistant 消息里发出的那次 `ask_user` 调用（含原始
    arguments），这就够了。这张记录是给**人和界面**看的：
    结构化、经过校验、不依赖解析模型生成的 JSON。
    """
    events = [
        assistant(calls=[("c_ask", "ask_user")]),
        asked("c_ask", question="这段文字不该出现在历史里"),
        answered("c_ask", choice="A"),
    ]

    joined = "\n".join(str(m.get("content", "")) for m in project_messages(events))

    assert "这段文字不该出现在历史里" not in joined


def test_pending_decision_without_an_answer_still_produces_a_legal_history():
    """**中断状态下投影也要合法**（虽然不应该拿它去发请求）。

    这条守的是「全函数」这个性质本身：不管事件序列多不完整，
    输出永远是合法的 OpenAI 消息列表。API 层另有一道门挡着
    （`status != idle` 直接 409），但那道门是业务规则，这条是数据结构性质。
    """
    events = [
        assistant(calls=[("c_ask", "ask_user")]),
        asked("c_ask"),
        # 用户还没答
    ]

    messages = project_messages(events)

    assert [m["role"] for m in messages] == ["assistant", "tool"]


# ══════════════════════════════════════════════════════ 决策视图


def test_list_decisions_keeps_answered_ones():
    """**已答的决策不能消失。**

    刷新页面之后用户要能看到「我刚才选的是什么」。答完就从列表里删掉的话，
    那段历史凭空没了，而模型还记得 —— 界面上看起来像是漏了一件事，
    用户会以为自己的选择没生效。
    """
    events = [
        assistant(calls=[("c1", "ask_user")]),
        asked("c1", question="第一个问题", options=["甲", "乙"]),
        answered("c1", choice="甲"),
        assistant(calls=[("c2", "ask_user")]),
        asked("c2", question="第二个问题", options=["丙", "丁"]),
    ]

    decisions = list_decisions(events)

    assert len(decisions) == 2
    assert decisions[0].question == "第一个问题"
    assert decisions[0].choice == "甲"  # 已答
    assert decisions[1].question == "第二个问题"
    assert decisions[1].choice is None  # 待答
    assert decisions[1].options == ["丙", "丁"]


def test_pending_decision_returns_only_the_unanswered_one():
    events = [
        asked("c1", question="第一个"),
        answered("c1", choice="甲"),
        asked("c2", question="第二个"),
    ]

    pending = pending_decision(events)

    assert pending is not None
    assert pending.call_id == "c2"


def test_pending_decision_is_none_when_everything_is_answered():
    assert pending_decision([asked("c1"), answered("c1")]) is None
    assert pending_decision([]) is None


# ══════════════════════════════════════════════════════ 派生：状态与标题


def test_status_flips_with_the_pending_decision():
    """状态是**推导出来的**，不是记的。

    这条测试和存储层的那条（`test_status_cache_matches_the_derivation`）
    配成一对：这里验证推导函数本身对，那里验证缓存和它一致。
    """
    before = [assistant(calls=[("c1", "ask_user")])]
    assert derive_status(before) == "idle"

    mid = [*before, asked("c1")]
    assert derive_status(mid) == "awaiting_user"

    after = [*mid, answered("c1")]
    assert derive_status(after) == "idle"


def test_title_comes_from_the_first_user_message():
    events = [LogUser(content="帮我做 2024 年国赛 A 题\n背景是这样的……")]

    # 换行被折叠成空格 —— 否则列表页的标题里会留一个断行，看起来像坏了
    assert derive_title(events) == "帮我做 2024 年国赛 A 题 背景是这样的……"


def test_title_is_truncated_and_has_a_placeholder():
    assert derive_title([]) == DEFAULT_TITLE
    assert derive_title([assistant(text="我先说两句")]) == DEFAULT_TITLE  # 没有用户消息

    long_title = derive_title([LogUser(content="一" * 100)])
    assert len(long_title) <= 31  # 30 个字符 + 省略号
    assert long_title.endswith("…")


# ══════════════════════════════════════════════════════ 组装请求


def test_build_messages_injects_the_persona():
    messages = build_messages([LogUser(content="你好")])

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SYSTEM_PROMPT
    assert messages[1]["content"] == "你好"


def test_build_messages_does_not_duplicate_an_existing_system_message():
    """历史里已经有 system 时不再插一条。"""
    events = [
        LogAssistant(message={"role": "system", "content": "你是猫"}),
        LogUser(content="喵"),
    ]

    messages = build_messages(events)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == "你是猫"


def test_build_messages_works_on_a_resumed_history():
    """**恢复路径上人设不能丢。**

    这是 `build_messages` 存在的主要理由。如果恢复时忘了插 system，
    会得到一个只在「中断之后」才出现的静默退化：蒟蒻人设消失，
    模型不再停下来问问题、不再用工具 —— 而界面上没有任何异常，
    只是「它好像变笨了」。这种 bug 极难定位。

    这条专门构造一个**中断后恢复**的历史来断言。
    """
    events = [
        LogUser(content="帮我选个模型"),
        assistant(calls=[("c1", "ask_user")]),
        asked("c1"),
        answered("c1", choice="熵权法"),
    ]

    messages = build_messages(events)

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SYSTEM_PROMPT
    # 而且恢复出来的历史是合法的（决策答案变成了 tool 消息）
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]


def test_stored_events_are_unwrapped_transparently():
    """投影同时接受 `StoredEvent`（存储层给的）和裸的 `LogEvent`（测试里手写的）。

    这条守的是一个便利性约定：存储层返回的是带 seq 的包装，
    而测试里再套一层 `StoredEvent(seq=..., created_at=...)` 是纯噪音。
    投影只关心内容，不关心它在哪一行。
    """
    wrapped = [
        StoredEvent(
            seq=0,
            created_at="2026-01-01T00:00:00.000000+00:00",
            event=LogUser(content="在"),
        ),
        StoredEvent(
            seq=1,
            created_at="2026-01-01T00:00:01.000000+00:00",
            event=assistant(text="在的"),
        ),
    ]

    assert [m["role"] for m in project_messages(wrapped)] == ["user", "assistant"]
    assert [m["role"] for m in project_messages([e.event for e in wrapped])] == [
        "user",
        "assistant",
    ]


@pytest.mark.parametrize(
    "events",
    [
        [],
        [LogUser(content="只有一句话")],
        [assistant(text="没有用户消息")],
        [asked("c_orphan")],  # 决策请求没有对应的 assistant 消息
        [tool_done("c_orphan")],  # 结果没有对应的调用
    ],
)
def test_projection_never_crashes_on_weird_input(events: list[object]):
    """投影对任何输入都不能抛异常 —— 它是纯函数，而且**每次刷新页面都会跑**。

    它一崩，整个会话就再也打不开了。用参数化把几种畸形序列都过一遍，
    断言只关心「不炸 + 输出是合法消息列表」。
    """
    messages = project_messages(events)  # type: ignore[arg-type]

    for message in messages:
        assert message["role"] in {"system", "user", "assistant", "tool"}
