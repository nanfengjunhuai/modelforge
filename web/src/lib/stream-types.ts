/**
 * 流式事件的 TypeScript 定义 —— 后端 `modelforge/providers/events.py` 的镜像。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么要在前端「抄」一遍后端已经定义好的结构？
 * ════════════════════════════════════════════════════════════════════
 * 因为 TypeScript 的类型只存在于编译期，而 JSON 是运行期的。后端推过来的
 * 是一串字节，前端拿到的 `JSON.parse` 结果是 `any` —— 类型系统在这里断了。
 *
 * 这个文件的作用就是**把类型接回来**：告诉编译器「我保证这条流里只会出现
 * 这六种形状」。之后 `switch (event.type)` 就能得到完整的类型收窄，
 * 写错字段名、漏掉分支，`tsc` 会在编译期就报错。
 *
 * ⚠️ **这个文件与 events.py 是手动同步的。** 后端加一种事件类型时，
 *    这里必须跟着加，否则前端的 switch 会静默走进 default 分支 ——
 *    不报错，只是那个事件永远不显示。这是「没有共享类型定义」这个选择的代价，
 *    换来的是不用为了两个语言的互操作引入一整套代码生成工具链。
 *
 *    这道防线现在有两层：
 *      ① 后端 `tests/test_event_contract.py` 直接比对两边的 type 取值集合，
 *         后端加了事件却忘了改前端，CI 会红；
 *      ② 前端 `FINISH_LABEL` 是 `Record<FinishReason, string>`，
 *         新增结束原因会直接编译不过。
 *    第 ① 条管事件类型，第 ② 条管枚举取值 —— 两者合起来覆盖面才完整。
 *
 * ── 与后端一一对应的关系 ──
 *   Python 的 `Literal["text_delta"]`  →  TS 的 `type: 'text_delta'`
 *   Python 的 `Annotated[Union[...], Field(discriminator="type")]`
 *                                      →  TS 的可辨识联合（discriminated union）
 * 两边用的是同一个套路：拿一个字段当判别子，让非法状态无法表达。
 * 这个思路从 page.tsx 的 `Status`、到 Pydantic 的事件模型、到这里，一路没变过。
 */

/**
 * 一轮生成是怎么结束的。取值与后端 `FinishReason` 完全一致。
 *
 * ⚠️ 后端**加一个新取值时，这个文件不改就编译不过** ——
 *    因为下面 FINISH_LABEL 的类型是 `Record<FinishReason, string>`，
 *    漏一个 key 就是类型错误。这是故意的：让编译器替我们记住同步这件事，
 *    比靠人记住可靠。
 */
export type FinishReason =
  | 'stop' // 正常说完了
  | 'tool_calls' // 模型要求执行工具（中间轮次的这个值会被 Agent 循环吃掉）
  | 'length' // 撞到 max_tokens 被截断 —— 结果可能不完整
  | 'content_filter' // 被内容安全策略拦截
  | 'error' // 异常终止，前面通常已经有一条 error 事件
  | 'max_rounds' // 工具调用轮数达到上限，Agent 循环主动收尾
  | 'awaiting_user' // 停在决策点上等用户拍板：这条流结束，但对话没结束

/** 模型吐出的一小段文本。产品体验的核心：它到了就立刻显示。 */
export type TextDelta = {
  type: 'text_delta'
  text: string
}

/**
 * 工具调用的一个碎片。
 *
 * ⚠️ 这四个字段里**只有 index 一定每次都有**：
 *   · id   只在首个碎片出现
 *   · name 通常只在首个碎片出现，但协议没保证
 *   · arguments_delta 是 JSON 字符串的碎片，要累积到结束才能 parse
 *
 * M2 阶段还看不到它（后端没下发任何工具），M3 接入沙箱后才会出现。
 * 现在就把类型和处理逻辑写好，是为了 M3 只动后端。
 */
export type ToolCallDelta = {
  type: 'tool_call_delta'
  index: number
  id: string | null
  name: string | null
  arguments_delta: string
}

/** Token 用量。用来算成本和展示，通常在流的最后出现。 */
export type UsageEvent = {
  type: 'usage'
  prompt_tokens: number
  completion_tokens: number
}

/** 本轮结束。**契约：它永远是一条流里的最后一个事件。** */
export type FinishEvent = {
  type: 'finish'
  reason: FinishReason
}

/**
 * 流中途出错。
 *
 * 注意这是**带内**（in-band）错误，不是 HTTP 错误 —— HTTP 状态码在流开始
 * 之前就定死了，之后只能靠事件本身传递坏消息。详见后端 chat.py 的模块注释。
 *
 * `retryable` 是后端把「重试有没有意义」这个判断的结果一并告诉前端。
 */
export type ErrorEvent = {
  type: 'error'
  message: string
  retryable: boolean
}

/**
 * 产物的**大类**。界面据此决定画成缩略图还是一个下载链接。
 *
 * 只分四个而不是直接用 MIME：MIME 太细了，前端要为 `image/svg+xml` 和
 * `image/png` 写两遍一模一样的分支。这一层是**给界面看的提示**，
 * 不是安全判断 —— 猜错了只会让某张图显示成一个链接。
 */
export type ArtifactKind = 'image' | 'data' | 'document' | 'other'

/**
 * 一个已落盘的产物（M5 新增）—— 模型的代码画出来的一张图、一份 CSV。
 *
 * ⚠️ **注意这里没有文件系统路径，而且是刻意的。** 后端那边有意识地
 * 把这个模型和「带 path 的暂存文件」分成两个类型（见 `artifacts/base.py`），
 * 让「带路径的东西」在类型层面就不可能流到浏览器。
 *
 * 取文件走 `GET /api/artifacts/{session_id}/{id}`，见 `lib/sessions.ts`
 * 的 `artifactUrl()`。
 */
export type ArtifactRef = {
  id: string
  /** 模型起的名字（可能是中文）。**磁盘上不是这个名字** —— 那边是 uuid。 */
  name: string
  kind: ArtifactKind
  mime: string
  /** 字节数。 */
  size: number
}

/**
 * 一次工具执行的结果。
 *
 * ⚠️ 这是唯一一个**不是模型发出来的**事件 —— 它是后端跑完代码之后自己造的。
 * 后端为此把事件模型拆成了两个联合（`ProviderEvent` 五种 / `StreamEvent` 六种），
 * 就是为了让「Provider 产不出 ToolResult」这件事在类型层面成立。
 *
 * 三个错误字段的区别，决定了界面上该画成什么样：
 *   · `ok === false` + 有 stderr  → 代码跑了但报错（正常，模型会自己修）
 *   · `timed_out`                 → 跑太久被杀了
 *   · `error` 非空                → **我们这边**的问题（参数坏了、沙箱没装好），
 *                                   模型改代码也没用，该提示用户去处理
 */
export type ToolResultEvent = {
  type: 'tool_result'
  call_id: string
  name: string
  ok: boolean
  stdout: string
  stderr: string
  exit_code: number | null
  duration_ms: number
  timed_out: boolean
  error: string | null
  /**
   * 这次执行产出的文件（M5 新增）。没有产物时是空数组，不是 undefined ——
   * 后端那边是个带 `default_factory` 的列表字段，永远会给。
   */
  artifacts: ArtifactRef[]
}

/**
 * Agent 停在决策点上，把候选方案摆给用户（M4 新增）。
 *
 * ⚠️ 和 `ToolResultEvent` 一样，这是**唯一一种模型不会产出的事件** ——
 * 它是 Agent 循环自己造的。后端为此把事件模型拆成了两个联合
 * （`ProviderEvent` 五种 / `StreamEvent` 七种），就是为了让
 * 「Provider 产不出这个」在类型层面成立。
 *
 * ════════════════════════════════════════════════════════════════
 * 它本质上是一次「结果来自人」的工具调用
 * ════════════════════════════════════════════════════════════════
 * 后端那边 `ask_user` 和 `run_python` 是平级的工具，走同一条链。
 * 差别只在结果从哪来。所以恢复的形态是：
 *
 *     用户点选项 → 后端把它 append 成一条事件
 *     → 从事件日志重新投影出完整历史 → 重新跑一次 Agent 循环
 *
 * 前端这边要做的，是**在界面上给这个调用一个地方放答案**。
 *
 * 收到它之后流会以 `finish: 'awaiting_user'` 结束 —— 注意那是「这一轮结束了」，
 * 不是「对话结束了」。输入框应该锁住，而不是显示成完成状态。
 */
export type DecisionRequestEvent = {
  type: 'decision_request'
  /** 和 assistant 那次 `ask_user` 的 tool_call id 一致。提交答案时要带回去。 */
  call_id: string
  question: string
  options: string[]
  allow_free_text: boolean
}

/**
 * 一条流里可能出现的所有事件。
 *
 * 用法：`switch (event.type)` —— TypeScript 会在这个联合上做穷尽性检查，
 * 走进某个分支后 `event` 自动收窄成对应的类型，可以直接访问 `event.text`。
 */
export type StreamEvent =
  | TextDelta
  | ToolCallDelta
  | UsageEvent
  | FinishEvent
  | ErrorEvent
  | ToolResultEvent
  | DecisionRequestEvent
