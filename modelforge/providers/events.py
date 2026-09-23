"""归一化的流式事件模型。

────────────────────────────────────────────────────────────────────────
为什么需要「归一化」？
────────────────────────────────────────────────────────────────────────
DeepSeek、通义、Kimi、Ollama、Anthropic 吐出来的响应格式各不相同。
如果上层的 Agent 代码直接消费这些原始格式，那么接一个新模型就要改一遍 Agent ——
这是灾难。

所以我们在这里定义一个「共同语言」：无论底层是谁，上层看到的都是这五种事件。
加新模型 = 写一个新的翻译器，Agent 代码一行不用改。

────────────────────────────────────────────────────────────────────────
为什么不能只返回一个字符串？
────────────────────────────────────────────────────────────────────────
因为工具调用是「碎片」流式返回的。模型的 `function.arguments` 不是一次给完整
JSON，而是一段一段吐字符：

    chunk 1 → {index:0, id:"call_abc", name:"run_python", arguments:""}
    chunk 2 → {index:0, arguments:"{\\"code\\":"}
    chunk 3 → {index:0, arguments:"\\"import nu"}
    chunk 4 → {index:0, arguments:"mpy\\"}"}

而且 index 的存在说明模型可以**同时发起多个工具调用**（并行）。所以上层必须知道
「文本在哪断开」「有几个工具调用」「哪个的参数拼完了」—— 一个字符串表达不了这些。

────────────────────────────────────────────────────────────────────────
为什么用 Pydantic 判别联合？
────────────────────────────────────────────────────────────────────────
和前端 page.tsx 里的 `type Status = 'idle' | 'loading' | ...` 是同一个思路：
用 `type` 字段作判别子，让「不可能的组合」在类型层面就无法表达。
副产品：M2 要把它序列化成 JSON 推给前端时，`model_dump_json()` 直接可用。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from modelforge.artifacts.base import ArtifactRef

__all__ = [
    "DecisionRequest",
    "ErrorEvent",
    "Finish",
    "FinishReason",
    "ProviderEvent",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolCallAccumulator",
    "ToolCallDelta",
    "ToolResult",
    "Usage",
]


# Finish 允许的结束原因。抽成类型别名（而不是把字面量写死在字段上）有三个好处：
#   1. `Finish` 的字段和 `classify_finish_reason` 的返回值共用同一定义，不会各自漂移；
#   2. mypy 会强制所有构造 Finish 的代码只能返回这五个值之一
#      —— 这条规则帮我抓出过一次真实疏漏：classify_finish_reason 原先声明返回 str，
#         等于放弃了「只返回合法值」的保证，mypy 在赋值处报错才暴露出来；
#   3. M2 生成 JSON Schema 时，前端能直接拿到这个枚举约束。
FinishReason = Literal[
    "stop",
    "tool_calls",
    "length",
    "content_filter",
    "error",
    "max_rounds",
    "awaiting_user",
]


# ══════════════════════════════════════════════════════ 五种事件


class TextDelta(BaseModel):
    """模型吐出的一小段文本。

    这是产品体验的核心：M2 要把它逐字推到页面上，让用户看着 Agent「思考」。
    """

    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallDelta(BaseModel):
    """工具调用的一个碎片。

    注意 —— 这四个字段里只有 `index` 保证每次都有：
      · `id`   只在首个碎片出现（后续碎片为 None）
      · `name` 通常只在首个碎片出现，但协议没有保证，所以消费方要「累积」而非「赋值」
      · `arguments_delta` 是 JSON 字符串的碎片，必须累积到结束才能解析

    消费这个事件请用本模块的 `ToolCallAccumulator`，不要手写累积逻辑。
    """

    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


class Usage(BaseModel):
    """这一轮的 token 用量。用于成本核算，也是 M5 要在界面上展示的东西。"""

    type: Literal["usage"] = "usage"
    prompt_tokens: int = 0
    completion_tokens: int = 0


class Finish(BaseModel):
    """本轮生成结束。

    **契约：Finish 永远是一条流里的最后一个事件。** Provider 实现有责任保证这一点
    （即使底层把结束标志和用量信息分散在不同 chunk 里）。消费方可以放心地把它
    当作循环终止的信号。

    reason 取值沿用 OpenAI 的约定（我们已决定用 OpenAI 格式作为内部标准）：
      · "stop"            正常说完了
      · "tool_calls"      模型要求执行工具 —— Agent 循环靠这个决定要不要调沙箱
      · "length"          撞到 max_tokens 被截断（上层应据此判断结果可能不完整）
      · "content_filter"  被内容安全策略拦截
      · "error"           异常终止（此时前面通常已有一条 ErrorEvent）
      · "max_rounds"      工具调用轮数达到上限，Agent 循环主动收尾
      · "awaiting_user"   M4 新增：停在决策点上等用户拍板，**这条流结束了但
                          对话没有结束**。前端据此把输入框锁住、把决策卡片高亮。

    后两个值**只有 Agent 循环会产出**，Provider 永远不会。加它们而不是复用
    "length" / "stop"，是因为它们对用户意味着完全不同的东西：
    length 是「回答被截断了」，max_rounds 是「它算得太久了，我喊停的」，
    awaiting_user 是「它在等你」。

    顺带一提，加这个值的成本只有一行 —— 前端那份 TypeScript 镜像会因为
    `Record<FinishReason, string>` 漏了 key **编译报错**，逼着人补上。
    这就是当初把 FinishReason 抽成类型别名、而不是散写字面量的回报。
    """

    type: Literal["finish"] = "finish"
    reason: FinishReason = "stop"


class ErrorEvent(BaseModel):
    """流中途出错。

    错误「在带内」（in-band）而不是抛异常，是刻意的设计：

      · 上层用 `async for event in provider.stream(...)` 统一处理，
        不需要在循环外面再包一层 try/except 去猜哪里会炸；
      · M2 要把错误直接转成 SSE 事件推给前端，在带内处理省掉一次格式转换；
      · `retryable` 把「能不能重试」的判断结果一并交给上层 ——
        Provider 自己绝不重试（已定决策），但上层状态机知道当前跑到哪一步，
        由它决定该重试、该降级、还是该告诉用户。
    """

    type: Literal["error"] = "error"
    message: str
    retryable: bool = False


# ══════════════════════════════════════════════════════ 沙箱事件
#
# ⚠️ 下面这个事件**不是模型发出来的**，是我们自己跑完代码之后造出来的。
#    它是目前唯一一个「非 Provider 来源」的事件 —— 这正是要把两个联合类型
#    分开的原因（见本文件末尾）。


class ToolResult(BaseModel):
    """一次工具执行的结果。

    为什么要把 stdout / stderr / 错误**分开**，而不是揉成一段文本？
    因为三者的读者不同：

      · `stdout` / `stderr` —— 会被原样回填给模型，是它的「眼睛」
      · `ok` / `timed_out`  —— 给**界面**看的，决定这块卡片画成绿的还是红的
      · `error`             —— 给我们自己看的（参数解析失败、找不到解释器等），
                               这种错误模型看了也没用，因为它不是代码的问题

    揉成一段的话，界面就没法区分「代码跑完了但有报错」（正常，模型会自己修）
    和「代码压根没跑起来」（我们这边的问题），而这两件事该给用户完全不同的提示。
    """

    type: Literal["tool_result"] = "tool_result"
    call_id: str
    name: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    timed_out: bool = False
    # 我们这边的失败（参数不是合法 JSON、沙箱解释器不存在……），不是代码的失败
    error: str | None = None

    artifacts: list[ArtifactRef] = Field(default_factory=list)
    """这次执行产出的文件（图、CSV……），M5 新增。

    ════════════════════════════════════════════════════════════════
    为什么加在这里，而不是新增一种日志事件
    ════════════════════════════════════════════════════════════════
    产物是**一次工具调用的结果**，就这么多。给它单开一种 `LogArtifact`
    事件的话，会立刻引出一个问题：它和那条 `tool` 事件怎么配对？
    答案是「靠 call_id 配」，于是同一件事就有了两个真相源，而它们可以不一致
    （日志里有 tool 没该 artifact、或者反过来）。

    挂在 `ToolResult` 上则完全不引入这种可能 —— 产物跟着结果走，
    一次调用要么有结果要么没有，不存在「产物的记录丢了但结果的记录还在」。

    实际收益很具体：**刷新页面时产物自动就回来了**。因为 M4 定下
    「`tool` 事件原样存整个 `ToolResult`」，产物引用就跟着一起进了日志，
    连一次新的查询都不用写。

    ⚠️ 它是**冻结**的。产物元数据一旦写进日志就不能再改（ADR-009：
    日志只能 append）。所以想做「删掉某个产物」，做法是删文件 + 在日志里
    追加一条「已删除」的记录，而不是回去把这条改了。M5 不做这个功能。
    """


class DecisionRequest(BaseModel):
    """Agent 停在决策点上，把候选方案摆给用户。M4 新增。

    ════════════════════════════════════════════════════════════════
    它的本质：一个「结果来自人」的工具调用
    ════════════════════════════════════════════════════════════════
    在 Agent 循环眼里，`ask_user` 和 `run_python` 是**平级的工具**，
    走的是同一条链（声明 → 碎片累积 → 派发 → 回填 → 继续）。
    唯一的差别是结果不在沙箱里。

    所以中断在这里不是「挂起一个生成器」，而是：

        如实记录一个还没有结果的工具调用 → 结束这条流
        （用户答完之后，答案作为那个调用的结果补上，从落盘的状态重建再跑）

    这正是 ADR-002 说的「状态落盘 + 点击时重建」的具体形态。

    ════════════════════════════════════════════════════════════════
    为什么 `options` 是字符串数组，而不是 {label, description} 对象
    ════════════════════════════════════════════════════════════════
    试过后者：模型开始把每个选项写成一整段小作文，卡片排版直接崩掉。
    现在是「一句话选项 + 把取舍写在句子里」（工具描述里明确要求了这一点），
    界面上一个选项一个按钮，长度可控。

    将来真需要「标题 + 副标题」的结构，正确的做法是**在服务端把一句话
    拆成主副标题**，而不是让模型自由发挥 —— 模型对「短一点」的理解
    和排版需要差得很远。
    """

    type: Literal["decision_request"] = "decision_request"
    call_id: str
    """和 assistant 消息里那次 `ask_user` 的 `tool_calls[].id` 一致。
    用户答完之后，答案以 `{"role":"tool","tool_call_id": call_id}` 的形式
    回到对话历史里 —— 所以这个 id 是整条恢复链路的钥匙。"""

    question: str
    options: list[str]
    allow_free_text: bool = True
    """允不允许「以上都不对」地自由作答。

    默认 True 是刻意的：数模题里用户的背景信息常常超出模型能枚举的范围
    （「我们组已经买了某软件」「赛题里其实还暗示了另一层条件」）。
    只给按钮会把这些信息挡在门外。
    """


# ══════════════════════════════════════════════════════ 判别联合

# 判别联合：Pydantic 会按 `type` 字段自动派发到具体类型。
# 注意这里必须用 Annotated + Field(discriminator=...)，否则 Pydantic 会尝试
# 逐个匹配所有成员，既慢又容易误判。
#
# 为什么是两个联合而不是一个？
# ------------------------------
# 因为「Provider 能产出什么」和「这条流上会出现什么」是**两件事**：
#
#     Provider 能产出的 = 前五种                        → ProviderEvent
#     这条流上会出现的   = 前五种 + ToolResult + DecisionRequest
#                                                      → StreamEvent
#
# 如果只留一个联合，`ChatProvider.stream()` 的返回类型就会包含 ToolResult 和
# DecisionRequest —— 那是在撒谎：Provider 根本没有能力产出「沙箱执行结果」，
# 更没有能力替 Agent 决定「该问用户了」。
# 而谎言会让人写出 `isinstance(event, ToolResult)` 的防御性代码去防一个
# 永远不会发生的情况，这种代码比没有更糟。
#
# 这和前端 `type Status = 'idle' | ...` 是同一个思路，只是往上提了一层：
# **用类型把「谁能干什么」划清楚，而不是靠约定和注释。**
#
# 代价：两个联合的成员列表有重复，加新事件时要记得两边都写。
# 这个代价由 tests/test_event_contract.py 兜底 —— 它会比对两边的 type 取值集合。
ProviderEvent = Annotated[
    TextDelta | ToolCallDelta | Usage | Finish | ErrorEvent,
    Field(discriminator="type"),
]

StreamEvent = Annotated[
    TextDelta | ToolCallDelta | Usage | Finish | ErrorEvent | ToolResult | DecisionRequest,
    Field(discriminator="type"),
]


# ══════════════════════════════════════════════════════ 碎片拼装


class ToolCall(BaseModel):
    """一个「拼装完成」的工具调用。

    注意这两个字段的角色完全不同，不要混淆：
      · `name`       —— 决定调用哪个工具，由模型给出，**不可信**
      · `arguments`  —— 一个还没解析的 JSON 字符串，**内容完全不可信**

    为什么这里存字符串而不是解析好的 dict？因为解析是沙箱层（M3）的职责，
    它要在解析前后做校验与白名单。在 Provider 层过早解析，等于把「不可信数据」
    偷偷变成了「看起来正常的对象」，丢掉了安全检查的机会。
    """

    id: str
    name: str
    arguments: str


class ToolCallAccumulator:
    """把碎片化的 ToolCallDelta 拼成完整的 ToolCall 列表。

    用法：

        acc = ToolCallAccumulator()
        async for event in provider.stream(...):
            if isinstance(event, ToolCallDelta):
                acc.feed(event)
            elif isinstance(event, Finish):
                tool_calls = acc.result()      # ← 这里才是完整的

    为什么按 index 分组而不是按顺序追加？因为模型可以并行发起多个工具调用，
    碎片的到达顺序不保证是「第一个调用的所有碎片 → 第二个调用的所有碎片」。
    """

    def __init__(self) -> None:
        # index -> 累积中的字段
        self._parts: dict[int, dict[str, str]] = {}

    def feed(self, delta: ToolCallDelta) -> None:
        """喂入一个碎片。"""
        slot = self._parts.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})

        # id 只在首个碎片出现，直接覆盖（后到的 None 不该清空已有的值）
        if delta.id:
            slot["id"] = delta.id

        # name 和 arguments 都可能是碎片，必须累加
        if delta.name:
            slot["name"] += delta.name
        if delta.arguments_delta:
            slot["arguments"] += delta.arguments_delta

    def result(self) -> list[ToolCall]:
        """返回拼装完成的工具调用，按 index 排序。"""
        return [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                # 无参数的工具调用，底层可能一个字符都不发。给个空对象让它仍能解析。
                arguments=slot["arguments"] or "{}",
            )
            for index, slot in sorted(self._parts.items())
        ]

    def reset(self) -> None:
        self._parts.clear()
