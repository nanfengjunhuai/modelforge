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
 * 这五种形状」。之后 `switch (event.type)` 就能得到完整的类型收窄，
 * 写错字段名、漏掉分支，`tsc` 会在编译期就报错。
 *
 * ⚠️ **这个文件与 events.py 是手动同步的。** 后端加一种事件类型时，
 *    这里必须跟着加，否则前端的 switch 会静默走进 default 分支。
 *    这是「没有共享类型定义」这个选择的代价 —— 换来的是不用为了两个语言
 *    的互操作引入一套代码生成工具链。
 *    M5 会写一个测试来盯住这件事（对比两边的 type 取值集合）。
 *
 * ── 与后端一一对应的关系 ──
 *   Python 的 `Literal["text_delta"]`  →  TS 的 `type: 'text_delta'`
 *   Python 的 `Annotated[Union[...], Field(discriminator="type")]`
 *                                      →  TS 的可辨识联合（discriminated union）
 * 两边用的是同一个套路：拿一个字段当判别子，让非法状态无法表达。
 * 这个思路从 page.tsx 的 `Status`、到 Pydantic 的事件模型、到这里，一路没变过。
 */

/** 一轮生成是怎么结束的。取值与后端 `FinishReason` 完全一致。 */
export type FinishReason =
  | 'stop' // 正常说完了
  | 'tool_calls' // 模型要求执行工具（M3 会用到）
  | 'length' // 撞到 max_tokens 被截断 —— 结果可能不完整
  | 'content_filter' // 被内容安全策略拦截
  | 'error' // 异常终止，前面通常已经有一条 error 事件

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
