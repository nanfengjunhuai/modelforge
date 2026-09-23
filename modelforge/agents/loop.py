"""Agent 循环 —— 让模型能跑代码，也让它能停下来问人。

════════════════════════════════════════════════════════════════════════
一个「回合」里发生了什么
════════════════════════════════════════════════════════════════════════

    用户：帮我算一下 1 到 100 的和，再开个平方
      │
      ├─ 第 1 轮 ────────────────────────────────────────────
      │   模型：我要调用 run_python({"code": "..."})
      │   （finish_reason = "tool_calls"）
      │        ↓ 执行（沙箱）
      │   ToolResult(stdout="5050")
      │        ↓ 回填成 {"role": "tool", ...}
      │
      ├─ 第 2 轮 ────────────────────────────────────────────
      │   模型：我要调用 ask_user({"question": "用哪种方式开方？", ...})
      │        ↓ **中断**
      │   DecisionRequest → 用户拍板 → decision_answer 落盘
      │
      └─ （流在这里结束，Finish(reason="awaiting_user")）
         ……用户点选之后，从落盘的状态重建，重新跑一次这个函数……

**循环的终止条件是模型自己说「我说完了」**，不是我们数够了几轮。
轮数上限只是一道保险丝（见下面「为什么必须有上限」）。

════════════════════════════════════════════════════════════════════════
中断 = 一个「结果来自人」的工具调用
════════════════════════════════════════════════════════════════════════
`ask_user` 和 `run_python` 在这里是**平级的工具**：都走「模型发起 → 我们处理
→ 结果回填 → 继续」。唯一的差别是结果不在沙箱里，在人那里。

所以中断不是「挂起一个生成器，等用户答完再唤醒」——那种做法进程一重启就没了
（ADR-002 拒绝它的原因）。这里是：

    如实记录一个还没有结果的调用 → 结束这条流
    → 用户答完后，答案作为那个调用的结果补上 → 重新跑一次本函数

**整条恢复路径没有一行是「恢复」代码**，它只是「用重建出来的历史重新跑一遍」。
这是把状态交给事件日志之后免费得到的好处。

════════════════════════════════════════════════════════════════════════
三个不明显但必须做对的地方
════════════════════════════════════════════════════════════════════════

① **中间那几轮的 Finish 不能直接转发。**

   `Finish` 是整条流的最后一个事件（M1 定的契约）。而每一轮模型生成结束时
   底层都会给一个 Finish —— 如果照单转发，前端会在第一轮工具调用之后就
   以为「完了」，直接退出循环，后面的答案一个字都收不到。

   所以这里的做法是：**扣住每一轮的 Finish，只在真正结束的那一刻发一个。**
   和 M1 里 Provider 扣住底层 Finish 再统一发，是同一个套路，只是又往上
   提了一层。

② **必须把「模型请求调用工具」这条消息写回历史。**

   如果只追加工具结果（role=tool），模型下一轮会看到一个「工具返回了结果」
   却找不到对应的「我请求过这个工具」—— 大多数服务会直接返回 400。
   所以 assistant 消息（带 tool_calls）和 tool 消息必须**成对**出现。

③ **每一轮生成都要落盘，包括最后那一轮。**（M4 新增，曾经是个真实的 bug）

   老实现只在「这一轮要调工具」的分支里把 assistant 消息写进历史 ——
   因为 M2/M3 的前端每次都把全量历史重发一遍，所以完全看不出来。
   但 M4 的历史是从事件日志投影出来的，于是：

       用户：帮我算 1 到 100 的和
       蒟蒻：5050                        ← 这句话从来没被存下来
       用户：那乘 2 呢？                  → 模型看到「我调了个工具，然后用户又问了新问题」
       刷新页面 → 刚才那句回答凭空消失

   修法不是在三个出口（正常结束 / 中断 / 轮数用尽）各补一次写入 ——
   那样漏一个就又是同一个 bug。**把「生成 + 落盘」提到分支判断之前无条件执行**，
   于是「N 轮生成 → N 次 assistant_round」成为一条可以离线断言的不变式。

════════════════════════════════════════════════════════════════════════
为什么必须有轮数上限
════════════════════════════════════════════════════════════════════════
模型完全可以陷入「反复调用同一个工具」的死循环 —— 尤其是当它修不好一个
报错的时候，它会一遍遍改、一遍遍失败。没有闸门的话，这就是一台不停烧
token 的机器，而用户只会看到转圈。

上限用完之后不是硬掐断，而是**再问一次但不给工具** —— 逼它用手上已有的
信息作答。给用户一个不完美的答案，比给一个空白页面好。

⚠️ M4 之后这个上限的**语义变了**：它是「单轮的保险丝」，不再是「一次会话的
   总预算」。因为中断恢复是「重新调用一次本函数」，每次调用都会重新计数 ——
   一个问了 5 次问题的会话实际允许的轮数会多得多。这是有意的取舍（见 ADR-009），
   不是疏漏。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from modelforge.config import get_settings
from modelforge.providers.base import ChatProvider, Message, ToolSpec
from modelforge.providers.events import (
    Finish,
    FinishReason,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallAccumulator,
    ToolCallDelta,
    ToolResult,
    Usage,
)
from modelforge.sandbox.base import CodeExecutor
from modelforge.sandbox.tools import (
    ASK_USER_NAME,
    BadArguments,
    build_decision_request,
    dispatch,
    format_for_model,
)
from modelforge.sessions.base import TurnRecorder

logger = logging.getLogger(__name__)

__all__ = ["run_agent_turn"]


@dataclass
class _RoundState:
    """一轮生成的汇总。

    存在的理由：异步生成器没法同时「边 yield 边 return 一个值」。
    所以把汇总通过这个可变对象带出来，让上层在 `async for` 结束后能读到。
    （另一种写法是用 `gen.__anext__()` 手动捕获 StopAsyncIteration.value，
     能用但可读性差很多。）
    """

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    finish: FinishReason = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _safe_arguments(raw: str) -> str:
    """确保回传给 API 的 arguments 是合法 JSON。

    模型偶尔会产出被截断的 JSON（撞到 max_tokens）。有些兼容服务在**下一次**
    请求里会去解析历史消息中的 `tool_calls[].function.arguments` —— 一个坏
    JSON 就能让整个请求 400，一次截断毁掉整条对话。

    所以这里兜个底：坏的换成 `{}`。原始内容仍然完整地出现在工具的错误信息里
    （见 `tools.dispatch`），模型看得到自己写歪了什么。

    ⚠️ 这个函数在 M4 之后多了一层重要性：落到事件日志里的是**它的返回值**。
    日志要存「模型当时到底看到了什么」，所以过滤必须在写日志之前完成 ——
    否则一次截断会在恢复之后让第一个请求直接 400，而且几乎无法复现。
    """
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return "{}"
    return raw


def _assistant_message(text: str, calls: list[ToolCall]) -> Message:
    """把「模型这一轮说了什么、要调什么工具」还原成 OpenAI 格式的 assistant 消息。

    格式细节来自 ADR-003：内部统一用 OpenAI 的格式，所以这里不需要任何转换，
    直接拼就行 —— 这正是当初选它当内部标准的回报。
    """
    message: Message = {
        "role": "assistant",
        # 模型可能一边说话一边调工具。就算它一个字没说，content 也要给个空串
        # 而不是 None：各家兼容服务对 null 的处理不一致，空串是最安全的。
        "content": text,
    }

    # ⚠️ 没有工具调用时**不要**输出 `tool_calls` 键。
    # 输出 `"tool_calls": []` 是非法消息 —— 协议要求这个字段要么不存在，
    # 要么是非空数组。这个分支 M4 才出现（因为现在每轮都会构造这条消息了，
    # 包括模型只是说了一句话就结束的那一轮）。
    if calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": _safe_arguments(call.arguments),
                },
            }
            for call in calls
        ]
    return message


def _tool_message(call: ToolCall, result: ToolResult) -> Message:
    """工具结果 → 回填给模型的那条 `{"role": "tool", ...}` 消息。

    内容在这里**渲染好就冻结**：它同时被写进事件日志。日志存的是
    「模型当时看到的那段文本」，而不是「原始结果 + 一个以后可能变形的渲染函数」。
    理由见 `sessions/base.py` 的模块注释末尾。
    """
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": format_for_model(result),
    }


async def _record_round(
    recorder: TurnRecorder, state: _RoundState, message: Message
) -> None:
    """把「这一轮跑完了」这件事完整地报告出去。

    封成函数而不是在两处各写两行，是因为**两个地方必须完全一致**：
    正常结束的那一轮和轮数用尽后的收尾轮。M4 之前那个「最后一轮不落盘」
    的 bug，正是这种「同一件事在两个出口各写一遍」的典型后果 ——
    写的时候觉得只是复制粘贴，改的时候漏一处就静默失效。
    """
    await recorder.assistant_round(message)
    await recorder.usage(
        prompt_tokens=state.prompt_tokens,
        completion_tokens=state.completion_tokens,
        finish_reason=state.finish,
    )


async def _stream_round(
    provider: ChatProvider,
    messages: list[Message],
    *,
    tools: list[ToolSpec] | None,
    temperature: float,
    max_tokens: int | None,
    into: _RoundState,
) -> AsyncIterator[StreamEvent]:
    """跑一轮模型生成：转发事件，同时把结果写进 `into`。

    "边转发边汇总" 是这个函数存在的全部理由 —— 事件的转发必须**即时**，
    攒完再发就没有流式效果了；而汇总只能在流结束后才知道。
    """
    accumulator = ToolCallAccumulator()
    text_parts: list[str] = []

    async for event in provider.stream(
        messages, tools=tools, temperature=temperature, max_tokens=max_tokens
    ):
        if isinstance(event, ToolCallDelta):
            accumulator.feed(event)
            yield event
        elif isinstance(event, Finish):
            # 扣住不发。这一轮可能只是中间轮次，真正该发的 Finish
            # 由外层在整条对话结束时统一发。
            into.finish = event.reason
        else:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, Usage):
                # 用量也要汇总 —— 它和 assistant 消息是同一轮的两面
                # （一个是产出、一个是成本），只是走不同的出口落盘。
                into.prompt_tokens = event.prompt_tokens
                into.completion_tokens = event.completion_tokens
            yield event

    into.text = "".join(text_parts)
    into.calls = accumulator.result()


async def run_agent_turn(
    provider: ChatProvider,
    messages: list[Message],
    *,
    executor: CodeExecutor,
    recorder: TurnRecorder,
    tools: list[ToolSpec] | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    max_rounds: int | None = None,
    scope: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """跑完一个 Agent 回合，产出**一条连续的、契约完整的**事件流。

    Args:
        provider: 模型接入方。
        messages: 对话历史。**会被就地扩展** —— 每轮的工具调用和结果都要写进去，
            模型下一轮才看得到自己刚才干了什么。调用方应传入一个可变的、
            属于本次请求的列表（会话路径下它是从事件日志投影出来的）。
        executor: 代码执行后端。
        recorder: 把跑的过程中发生的事报告出去（落盘）。**没有默认值** ——
            给了默认值的话，忘了传就会变成「一切正常，只是刷新后什么都没有」，
            而这正是 M4 唯一的验收标准。必填能让 mypy 在调用点抓住它。
            无状态端点显式传 `DiscardRecorder()`。
        tools: 可用的工具。None 或空列表表示这一轮不让模型用工具。
            会话路径传的是 `TOOL_SPECS + [ASK_USER_SPEC]`。
        temperature: 采样温度。
        max_tokens: 单轮输出上限。
        max_rounds: 工具调用轮数上限。None 表示用配置里的默认值。
        scope: 工具产出的文件归属谁（会话 id），M5 新增。

            这个参数只是**路过**这里 —— 循环自己不碰产物，它把 scope 原样
            转交给 `dispatch`，最后进到 `executor.run()`。之所以要在这里
            开一个口子，是因为循环是唯一同时知道「要执行什么」和「这次执行
            属于哪次对话」的地方：

                api/sessions.py   知道会话，但不知道工具调用的细节
                sandbox/tools.py  知道怎么执行，但不知道会话
                agents/loop.py    ← 两者在这里交汇，所以在这里对接

            无状态端点不传，产物就不保留 —— 那里没有地方放，也没有界面展示。

    Yields:
        `StreamEvent`：文本增量、工具调用碎片、**工具执行结果**、
        **决策请求**、用量、以及最后恰好一个 `Finish`。

    契约（与 M1 一致，且更严格）：
      · **恰好一个 Finish，且在最后。** 中间轮次的 Finish 会被吃掉。
      · **不抛异常。** 失败以带内的 ErrorEvent 表达，最后补一个 Finish。
        （`asyncio.CancelledError` 除外 —— 那是上层要停我们，让它传播。）
    """
    settings = get_settings()
    limit = max_rounds if max_rounds is not None else settings.agent_max_tool_rounds
    # 没有工具可用时，「工具调用」这件事根本不存在，轮数上限也就没有意义 ——
    # 直接按一轮处理，省掉一层循环。
    usable_tools = tools or None
    effective_limit = limit if usable_tools else 0

    for round_index in range(effective_limit):
        state = _RoundState()
        async for event in _stream_round(
            provider,
            messages,
            tools=usable_tools,
            temperature=temperature,
            max_tokens=max_tokens,
            into=state,
        ):
            yield event

        # ★ M4 的关键改动：无条件生成并落盘这一轮的 assistant 消息。
        #
        # 位置是刻意的 —— 在下面所有分支判断**之前**。这样三个出口
        # （正常结束 / 中断等用户 / 轮数用尽后收尾）自动全覆盖，
        # 而且「N 轮生成 → N 次 assistant_round」是一条能离线断言的不变式。
        # 见模块注释的「③」。
        assistant_message = _assistant_message(state.text, state.calls)
        messages.append(assistant_message)
        await _record_round(recorder, state, assistant_message)

        # 模型不再要工具了（或者中间出错了）→ 这一轮就是最终答案。
        # 注意 "error" 也会走到这里：Provider 出错后会给 Finish(reason="error")，
        # 我们不重试、也不再调工具，直接把错误如实传达给用户。
        if state.finish != "tool_calls" or not state.calls:
            yield Finish(reason=state.finish)
            return

        logger.info(
            "第 %d/%d 轮工具调用：%s",
            round_index + 1,
            effective_limit,
            [call.name for call in state.calls],
        )

        # ── ① 执行所有**不需要人**的调用 ─────────────────────────────
        #
        # 先把它们跑完再处理 ask_user，而不是「遇到 ask_user 就停下，
        # 把后面的标记成已取消」。后者看起来更「忠于顺序」，实际会引入
        # 一个错误的语义：那些被取消的记录会经由 format_for_model 渲染成
        # 「【沙箱未能执行代码】这不是你的代码的问题」—— 一句彻头彻尾的假话，
        # 会把模型引向完全错误的方向。
        #
        # 先跑完在语义上也是对的：模型**并行**发起的调用本来就互不依赖，
        # 否则它就该分两轮发了。
        tool_messages: dict[str, Message] = {}
        for call in state.calls:
            if call.name == ASK_USER_NAME:
                continue
            result = await dispatch(call, executor=executor, scope=scope)
            message = _tool_message(call, result)
            tool_messages[call.id] = message
            # 落盘在 yield 之前：让数据库始终**领先于**网络。
            # 用户正好在这一刻刷新页面时，他会看到结果而不是「未执行」。
            await recorder.tool_result(message, result)
            yield result

        # 按 tool_calls 的**原始顺序**写回内存里的历史。
        # 必须和 `project_messages` 的装配顺序一致（它也是按 tool_calls 顺序），
        # 否则同一轮对话在内存里和在日志里的排列会不一样，
        # 而那种不一致只会在恢复之后才显现出来。
        for call in state.calls:
            recorded = tool_messages.get(call.id)
            if recorded is not None:
                messages.append(recorded)

        # ── ② 需要人拍板的那个，放在最后 ─────────────────────────────
        pending = next((c for c in state.calls if c.name == ASK_USER_NAME), None)
        if pending is None:
            continue

        try:
            request = build_decision_request(pending)
        except BadArguments as exc:
            # 参数写歪了。这和 run_python 的参数写歪是**同一类**错误 ——
            # 模型能自己修，所以回填一条失败结果让它重试，而不是中断。
            # 中断去问一个连问题都没成型的东西，对用户是纯打扰。
            logger.warning("ask_user 的参数不合法：%s", exc)
            result = ToolResult(
                call_id=pending.id, name=pending.name, ok=False, error=str(exc)
            )
            message = _tool_message(pending, result)
            messages.append(message)
            await recorder.tool_result(message, result)
            yield result
            continue

        logger.info("停在决策点等用户：%s", request.question)
        await recorder.decision_requested(request)
        yield request
        yield Finish(reason="awaiting_user")
        return

    # 配额用尽。最后再问一次，**这次不给工具** —— 逼它用手上已有的信息作答。
    # 给用户一个「我没能完全搞定」的答案，比给一个空白页面好。
    logger.warning("工具调用达到上限 %d 轮，最后一轮不再提供工具", effective_limit)
    state = _RoundState()
    async for event in _stream_round(
        provider,
        messages,
        tools=None,
        temperature=temperature,
        max_tokens=max_tokens,
        into=state,
    ):
        yield event

    # 收尾轮同样要落盘 —— 这是「③」里那个 bug 的最后一个出口。
    assistant_message = _assistant_message(state.text, state.calls)
    messages.append(assistant_message)
    await _record_round(recorder, state, assistant_message)

    # 正常情况下这一轮不给工具，模型只能说话，finish 会是 "stop"。
    # 但有些模型会硬编一个 tool_calls 出来 —— 那种情况下如实标成 max_rounds。
    yield Finish(reason="max_rounds" if state.finish == "tool_calls" else state.finish)
