/**
 * 事件日志的 TypeScript 镜像 —— 后端 `modelforge/sessions/base.py` 的对应物。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么这里还有一套类型，而不是复用 stream-types.ts
 * ════════════════════════════════════════════════════════════════════
 * 因为**这是两个不同的问题**：
 *
 *     StreamEvent  线上传什么   —— 流式的碎片、实时的、转瞬即逝的
 *     LogEvent     盘上存什么   —— 累积完的、不可变的、可回放的
 *
 * 最明显的区别：`text_delta` 和 `tool_call_delta` 是流事件但**不落盘**
 * （它们累积完之后由一条 `assistant` 记录代替）；而 `kind: 'usage'`
 * 落盘但不在流里。两边只有一半重叠。
 *
 * 另一个信号是判别子：看到 `type` 就是流事件，看到 `kind` 就是日志记录。
 * 后端刻意这么定的，前端跟着用。
 *
 * ⚠️ 和后端是**手动同步**的。防线有两层：
 *   ① 后端 `tests/test_event_contract.py` 比对两边的 kind 集合**和
 *      每种事件的字段名集合** —— 把 `call_id` 写成 `callId` 会直接红；
 *   ② 下面的判别联合会让 `switch` 有穷尽性检查。
 */

/** 一条 OpenAI 格式的消息，**原样**存进日志。 */
export type LoggedMessage = {
  role: string
  content: string
  /** 只有 assistant 且它在请求工具时才有。没有工具调用时这个键**不存在**。 */
  tool_calls?: LoggedToolCall[]
  /** 只有 role 是 'tool' 时才有。 */
  tool_call_id?: string
}

export type LoggedToolCall = {
  id: string
  type: 'function'
  function: { name: string; arguments: string }
}

/** 一次工具执行的结果 —— 给界面回放用（stdout / 耗时 / 退出码）。 */
export type LoggedToolResult = {
  call_id: string
  name: string
  ok: boolean
  stdout: string
  stderr: string
  exit_code: number | null
  duration_ms: number
  timed_out: boolean
  error: string | null
}

// ══════════════════════════════════════════════════════ 七种记录

/** 用户说的一句话。 */
export type LogUserEvent = {
  kind: 'user'
  content: string
}

/** 一轮完整的模型生成（不是碎片 —— 碎片在 journal 里没有位置）。 */
export type LogAssistantEvent = {
  kind: 'assistant'
  message: LoggedMessage
}

/** 一次工具执行。`message` 是回填给模型的、已渲染好的文本。 */
export type LogToolEvent = {
  kind: 'tool'
  message: LoggedMessage
  result: LoggedToolResult
}

/**
 * Agent 停下来问用户。
 *
 * ⚠️ 这条**不投影进对话历史** —— 模型看到的是自己在 assistant 消息里发出的
 * 那次 `ask_user` 调用。这条记录是给**界面和审计**看的：
 * 结构化、经过校验、不依赖解析模型生成的 JSON。
 */
export type LogDecisionRequestEvent = {
  kind: 'decision_request'
  call_id: string
  question: string
  options: string[]
  allow_free_text: boolean
}

/** 用户拍板了。这条**会**投影进对话历史（变成一条 tool 消息）。 */
export type LogDecisionAnswerEvent = {
  kind: 'decision_answer'
  call_id: string
  choice: string
  note: string
}

/** 一轮的 token 用量。落盘但不投影 —— M5 的成本核算要用。 */
export type LogUsageEvent = {
  kind: 'usage'
  prompt_tokens: number
  completion_tokens: number
  finish_reason: string
}

/** 流跑到一半被中断（用户刷新、点停止、后端起停）。 */
export type LogAbortedEvent = {
  kind: 'aborted'
  reason: string
}

export type LogEvent =
  | LogUserEvent
  | LogAssistantEvent
  | LogToolEvent
  | LogDecisionRequestEvent
  | LogDecisionAnswerEvent
  | LogUsageEvent
  | LogAbortedEvent

/** 日志里的一行：一个事件 + 它在会话中的位置。 */
export type StoredEvent = {
  /** 会话内从 0 递增。渲染 key 用它 —— 比数组下标稳定。 */
  seq: number
  created_at: string
  event: LogEvent
}

// ══════════════════════════════════════════════════════ 会话

/** `idle` 可以发新消息；`awaiting_user` 有一个问题在等你拍板。 */
export type SessionStatus = 'idle' | 'awaiting_user'

export type Session = {
  id: string
  title: string
  status: SessionStatus
  /**
   * 此刻有没有一条流正在这个会话上跑。
   *
   * 和后端的 `status` 是**两件事**：`status` 是从事件日志推出来的，
   * `busy` 是进程的瞬时属性（数据库租约）。混在一起会得到一个
   * 永远不自洽的状态机 —— 详见后端 `sessions/base.py`。
   */
  busy: boolean
  created_at: string
  updated_at: string
}

/**
 * 一次「Agent 问、用户答」的完整记录。
 *
 * `choice === null` 表示**还等着用户拍板**。
 * 已答的决策也保留在列表里 —— 刷新页面之后要能看到「我刚才选了什么」，
 * 否则那段历史凭空消失，而模型还记得，界面上看起来像是漏了一件事。
 */
export type Decision = {
  call_id: string
  question: string
  options: string[]
  allow_free_text: boolean
  choice: string | null
  note: string
  /** 请求所在的 seq。渲染 key 用它。 */
  seq: number
}

/** `GET /api/sessions/{id}` 的响应 —— 前端**刷新后重建整个界面**靠它。 */
export type SessionDetail = {
  session: Session
  events: StoredEvent[]
  decisions: Decision[]
}
