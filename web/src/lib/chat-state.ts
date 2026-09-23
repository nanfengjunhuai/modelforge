/**
 * 对话界面的状态模型 —— 把「事件」折叠成「视图」的纯函数。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么要把这段逻辑单独拿出来
 * ════════════════════════════════════════════════════════════════════
 * 界面上的东西有两个来源，而它们的数据形态完全不同：
 *
 *   **实时路径** —— 后端一路 SSE 推过来，是**碎片**：
 *       text_delta（半个字）、tool_call_delta（半个 JSON）、tool_result…
 *       要边收边拼。
 *
 *   **回放路径** —— 刷新页面后 GET 一次，是**完整事件日志**：
 *       一句话就是一条 `assistant` 记录，一个工具调用就是一条完整的
 *       tool_calls 数组。
 *
 * 两条路的输入差别很大，但**输出必须一模一样** —— 否则「刷新前」和
 * 「刷新后」的界面会长得不一样，用户会以为自己的东西丢了。
 *
 * 所以这里有两个入口（`applyEvent` 和 `fromSessionDetail`），
 * 产出同一种 `ChatState`。界面只认 ChatState，不关心它是怎么来的。
 *
 * ════════════════════════════════════════════════════════════════════
 * 「正在流式输出」是怎么表达的
 * ════════════════════════════════════════════════════════════════════
 * 用 `liveKey` —— 指向那条正在被写入的助手消息。
 *
 * 不用一个布尔 `streaming`，是因为**光知道「在流」不够用**：每来一个
 * 文字碎片，都得知道该往哪条消息后面追加。用一个 key 把这两件事
 * （在不在流 / 写到哪里）合成一个字段，就没有「状态不一致」的可能 ——
 * 而分开存的话，`streaming === true` 但 `liveKey === null` 是个
 * 能表达但不该存在的状态。
 *
 * 同理，「有没有待拍的决策」也是**推导**出来的（见 `pendingDecision`），
 * 不是存下来的。后端那边 `derive_status` 不存 status 是同一个道理：
 * 能算出来的东西就别存，存了就有不一致的风险。
 */

import type { Decision, LoggedMessage, SessionDetail } from './log-types'
import type { FinishReason, ToolResultEvent, StreamEvent } from './stream-types'

// ══════════════════════════════════════════════════════ 类型

/**
 * 一个决策点的界面状态。
 *
 * `choice === null` 表示**还等着用户拍板** —— 它和 `ToolCallView.result === null`
 * 是同一个套路：用一个可空的字段天然表达「还没发生」，不需要额外的布尔。
 */
export type DecisionView = {
  callId: string
  question: string
  options: string[]
  allowFreeText: boolean
  choice: string | null
  note: string
}

/**
 * 界面上呈现的一个工具调用。
 *
 * 「调用」和「结果」在**协议上是分开的**（tool_call_delta / tool_result），
 * 在界面上合成一条。理由：分开存的话，渲染时要按 id 去另一个数组里查配对，
 * 而且「结果先到、调用后到」这种乱序会让查询落空。
 * 合成一条之后，`result === null` 天然就表示「还在跑」。
 *
 * `decision` 字段是把「ask_user 也是一个工具」这件事贯彻到底的地方 ——
 * 后端就是这么建模的（见 providers/events.py 的 DecisionRequest），
 * 前端跟着做，于是决策卡片和工具卡片走同一套增删改逻辑。
 * 区别只在渲染时：有 decision 的走 DecisionCard，没有的走 ToolCallCard。
 */
export type ToolCallView = {
  /** React key。用 `消息key:序号` 而不是裸的序号 —— 后者只在单条消息内唯一。 */
  key: string
  /** 后端回填结果时的配对钥匙。只在首个碎片里出现，所以是可空的。 */
  id: string | null
  name: string
  /** 未解析的 JSON 字符串碎片，边流边拼。解析见 `extractCode` / `extractQuestion`。 */
  args: string
  result: ToolResultEvent | null
  decision: DecisionView | null
}

export type Msg = {
  key: string
  role: 'user' | 'assistant'
  content: string
  tools: ToolCallView[]
}

export type ChatState = {
  messages: Msg[]
  /** 正在被流式写入的那条助手消息；null = 当前没有在流。 */
  liveKey: string | null
  /** 面向用户的错误文案。原始报错只进控制台。 */
  error: string | null
}

export function emptyChat(): ChatState {
  return { messages: [], liveKey: null, error: null }
}

// ══════════════════════════════════════════════════════ 身份

/**
 * 消息 key 的计数器。放模块级而不是 state，因为它不该触发重新渲染。
 *
 * 为什么不直接用数组下标？因为流式更新时数组一直在变（消息会被追加、
 * 内容会被就地改写），用下标做 key 会让 React 认错元素 ——
 * 症状是输入框失焦、动画重放、折叠状态乱跳，而且很难联想到是 key 的问题。
 */
let nextKey = 1

function makeKey(prefix: string): string {
  return `${prefix}-${nextKey++}`
}

// ══════════════════════════════════════════════════════ 推导

/** 有没有正在等用户拍板的决策。有的话输入框要锁住。 */
export function pendingDecision(
  state: ChatState,
): { messageKey: string; tool: ToolCallView } | null {
  for (const message of state.messages) {
    for (const tool of message.tools) {
      if (tool.decision && tool.decision.choice === null) {
        return { messageKey: message.key, tool }
      }
    }
  }
  return null
}

/** 所有决策，含已答的 —— 界面要把用户选过的东西也显示出来。 */
export function allDecisions(state: ChatState): DecisionView[] {
  return state.messages.flatMap((m) =>
    m.tools.flatMap((t) => (t.decision ? [t.decision] : [])),
  )
}

// ══════════════════════════════════════════════════════ 辅助

/**
 * 往某条消息上打一个补丁，其余原样返回。
 *
 * 找不到就直接返回原状态（引用不变）—— 这样 React 能跳过这次渲染。
 * 流式场景下这个「找不到」其实经常发生（消息已经被别的更新替换掉了），
 * 返回原引用比返回一个新对象便宜得多。
 */
function patchMessage(state: ChatState, key: string, fn: (msg: Msg) => Msg): ChatState {
  let touched = false
  const messages = state.messages.map((m) => {
    if (m.key !== key) return m
    touched = true
    return fn(m)
  })
  return touched ? { ...state, messages } : state
}

// ══════════════════════════════════════════════════════ 开始一轮

/** 用户发了一句话 —— 追加用户气泡，并占好助手气泡的位置。 */
export function startTurn(state: ChatState, content: string, assistantKey: string): ChatState {
  return {
    ...state,
    error: null,
    messages: [
      ...state.messages,
      { key: makeKey('user'), role: 'user', content, tools: [] },
      { key: assistantKey, role: 'assistant', content: '', tools: [] },
    ],
    liveKey: assistantKey,
  }
}

/**
 * 开一条新的助手消息，但**不加用户气泡**。
 *
 * 用在提交决策之后：用户刚才点的是卡片上的按钮，不是在输入框里说话，
 * 界面上不该多出一个用户气泡。
 */
export function startAssistant(state: ChatState, assistantKey: string): ChatState {
  return {
    ...state,
    error: null,
    messages: [
      ...state.messages,
      { key: assistantKey, role: 'assistant', content: '', tools: [] },
    ],
    liveKey: assistantKey,
  }
}

/** 分配一个助手消息的 key。在调用 startTurn / startAssistant 之前调。 */
export function newAssistantKey(): string {
  return makeKey('assistant')
}

// ══════════════════════════════════════════════════════ 实时路径

/**
 * 把一条流事件折进状态里。
 *
 * 这是个**纯函数**：同样的 (state, event) 必然得到同样的结果。
 * 界面层的职责只剩下「把它接进 setState」。
 */
export function applyEvent(state: ChatState, event: StreamEvent): ChatState {
  switch (event.type) {
    // ── 文本碎片：追加到正在写的那条消息末尾 ──
    case 'text_delta': {
      if (!state.liveKey) return state
      const key = state.liveKey
      return patchMessage(state, key, (m) => ({ ...m, content: m.content + event.text }))
    }

    // ── 工具调用碎片：按 index 累积 ──
    case 'tool_call_delta': {
      if (!state.liveKey) return state
      const key = state.liveKey
      return patchMessage(state, key, (msg) => {
        const index = msg.tools.findIndex((t) => t.key === toolKeyOf(key, event.index))

        if (index === -1) {
          return {
            ...msg,
            tools: [
              ...msg.tools,
              {
                key: toolKeyOf(key, event.index),
                // id 只在首个碎片里出现。首个碎片没给的话先留 null，
                // 后面靠 tool_result 的顺序兜底认领（见下面）。
                id: event.id,
                name: event.name ?? '',
                args: event.arguments_delta,
                result: null,
                decision: null,
              },
            ],
          }
        }

        return {
          ...msg,
          tools: msg.tools.map((t, i) =>
            i === index
              ? {
                  ...t,
                  // ⚠️ id 和下面两个的规则**不一样**：
                  //    id 是「赋值」（首个碎片给了之后，后续都是 null），
                  //    name / args 是「累加」（真的会分片到达）。
                  //    搞混了不会立刻出错 —— id 拼成 "call_xx" + "" 还是原样，
                  //    但只要哪个服务多发一次 id，就变成 "call_xxcall_xx" 了。
                  id: t.id ?? event.id,
                  name: t.name + (event.name ?? ''),
                  args: t.args + event.arguments_delta,
                }
              : t,
          ),
        }
      })
    }

    // ── 工具执行结果：认领到对应的那张卡片上 ──
    case 'tool_result': {
      if (!state.liveKey) return state
      const key = state.liveKey
      return patchMessage(state, key, (msg) => {
        // 首选按 call_id 精确配对。
        const byId = msg.tools.map((t) => (t.id === event.call_id ? { ...t, result: event } : t))
        if (byId.some((t) => t.result === event)) return { ...msg, tools: byId }

        // 兜底：id 对不上时按顺序认领第一张还没结果的卡片。
        // 什么时候会对不上？有的兼容服务根本不返回工具调用 id，
        // 后端会用 "call_{index}" 补一个占位，跟前端拼出来的对不上号。
        //
        // ⚠️ 兜底只对**有结果的**工具生效。被中断的 ask_user 永远不会有
        //    tool_result（它的结果来自人），所以不会在这里被误认领 ——
        //    它会经由 decision_request 走另一条路。
        const index = byId.findIndex((t) => t.result === null && t.decision === null)
        if (index === -1) return msg
        const tools = [...byId]
        tools[index] = { ...tools[index], result: event }
        return { ...msg, tools }
      })
    }

    // ── Agent 停下来问用户 ──
    case 'decision_request': {
      if (!state.liveKey) return state
      const key = state.liveKey
      const decision: DecisionView = {
        callId: event.call_id,
        question: event.question,
        options: event.options,
        allowFreeText: event.allow_free_text,
        choice: null,
        note: '',
      }
      return patchMessage(state, key, (msg) => {
        if (msg.tools.some((t) => t.id === event.call_id || t.decision?.callId === event.call_id)) {
          return {
            ...msg,
            tools: msg.tools.map((t) =>
              t.id === event.call_id || t.decision?.callId === event.call_id
                ? { ...t, decision }
                : t,
            ),
          }
        }
        // 没找到对应的工具调用也要显示 —— 问题绝不能因为配对失败就丢掉，
        // 那会让用户看到一个卡住、又无从下手的界面。补一张卡片出来。
        return {
          ...msg,
          tools: [
            ...msg.tools,
            {
              key: toolKeyOf(key, msg.tools.length),
              id: event.call_id,
              name: 'ask_user',
              args: '',
              result: null,
              decision,
            },
          ],
        }
      })
    }

    case 'usage':
      // 用量要显示的话得单开一处 state。M4 不做（M5 做成本面板时再说）——
      // 它已经落在后端的事件日志里了，将来随时能从 GET 拿回来。
      return state

    // ── 一轮结束 ──
    case 'finish':
      // `awaiting_user` 不是「出错了」，而是「流结束了，但对话没结束」。
      // 界面上它表现为：输入框锁住、决策卡片高亮。
      // 这里只要把 liveKey 清掉（流停了），其余交给 pendingDecision 推导。
      return { ...state, liveKey: null }

    case 'error':
      // ⚠️ event.message 是模型/网络层的原始报错，可能夹带敏感信息。
      //    按项目规矩：只进控制台，页面说人话。
      console.error('流内错误：', event.message, '可重试:', event.retryable)
      return { ...state, error: '生成中断了。多半是网络抖动，重发一次通常就好。' }
  }
}

/** 工具卡的 key：`消息key:序号`。序号在一条消息内唯一，够用了。 */
function toolKeyOf(messageKey: string, index: number): string {
  return `${messageKey}:${index}`
}

/**
 * 把某个决策标成「已答」。
 *
 * 为什么要前端自己标，而不是等后端推一条 `decision_answer` 事件过来？
 * 因为**后端根本不推** —— 用户提交答案之后，后端做的是「把答案落盘、
 * 重建历史、接着跑」，推过来的是模型接下来的回答，不是「你刚才选了什么」。
 *
 * 落盘那个答案是写进事件日志的，只有 GET 才拿得到。所以这里做一次
 * **乐观更新**：用户点了按钮，卡片立刻翻成「已拍板」，
 * 而事实（事件日志）在后端是同一时刻写下的。
 *
 * ⚠️ 调用时机很关键 —— 见下方 `onAccepted` 的说明。
 */
export function markDecision(
  state: ChatState,
  callId: string,
  choice: string,
  note: string,
): ChatState {
  return {
    ...state,
    messages: state.messages.map((m) => ({
      ...m,
      tools: m.tools.map((t) =>
        t.decision?.callId === callId
          ? { ...t, decision: { ...t.decision, choice, note } }
          : t,
      ),
    })),
  }
}

/** 用户点了「停止」——流被中止，不动内容，只把 liveKey 清掉。 */
export function abortStream(state: ChatState): ChatState {
  return { ...state, liveKey: null }
}

export function failWith(state: ChatState, message: string): ChatState {
  return { ...state, error: message, liveKey: null }
}

// ══════════════════════════════════════════════════════ 回放路径

/**
 * 从完整的事件日志重建整个界面 —— **刷新页面走的就是这条路**。
 *
 * 每条事件产出的东西和实时路径必须一致，否则「刷新前」和「刷新后」
 * 会长得不一样。三处对应关系：
 *
 *     LogUser            ←→  user 气泡
 *     LogAssistant       ←→  assistant 气泡 + 它的工具卡
 *     LogTool            ←→  某张工具卡的 result
 *     LogDecisionRequest ←→  DecisionCard（question / options）
 *
 * `decisions` 是后端已经合并好的决策列表（含答案），用它而不是自己从
 * 事件里配对 request 和 answer —— 那是后端的 `list_decisions` 干的事，
 * 前端再实现一遍就是第二份投影逻辑了。
 */
export function fromSessionDetail(detail: SessionDetail): ChatState {
  const decisionByCall = new Map<string, Decision>(
    detail.decisions.map((d) => [d.call_id, d]),
  )

  const messages: Msg[] = []

  for (const stored of detail.events) {
    const event = stored.event

    switch (event.kind) {
      case 'user':
        messages.push({
          key: makeKey('user'),
          role: 'user',
          content: event.content,
          tools: [],
        })
        break

      case 'assistant': {
        const key = makeKey('assistant')
        messages.push({
          key,
          role: 'assistant',
          content: event.message.content,
          tools: (event.message.tool_calls ?? []).map((call, index) => {
            const decision = decisionByCall.get(call.id)
            return {
              key: toolKeyOf(key, index),
              id: call.id,
              name: call.function.name,
              // 日志里存的就是完整的参数字符串（不像 SSE 那样是碎片），
              // 所以这里不需要累积 —— 这正是「回放」和「实时」最大的差别。
              args: call.function.arguments,
              result: null,
              decision: decision
                ? {
                    callId: decision.call_id,
                    question: decision.question,
                    options: decision.options,
                    allowFreeText: decision.allow_free_text,
                    choice: decision.choice,
                    note: decision.note,
                  }
                : null,
            }
          }),
        })
        break
      }

      case 'tool': {
        const callId = event.message.tool_call_id
        if (!callId) break
        attachResult(messages, callId, {
          ...event.result,
          // 日志里存的是裸的 ToolResult（没有判别子），补上 type 让它
          // 长得和流事件一样 —— 界面只认一种形状。
          type: 'tool_result',
        })
        break
      }

      // decision_request 的信息已经通过上面的 decisionByCall 归位了；
      // usage / aborted 不产出任何界面元素。
      case 'decision_request':
      case 'decision_answer':
      case 'usage':
      case 'aborted':
        break
    }
  }

  return { messages, liveKey: null, error: null }
}

/** 把结果挂到**最后一条**含这个 call_id 的助手消息上。 */
function attachResult(messages: Msg[], callId: string, result: ToolResultEvent): void {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i]
    if (msg.role !== 'assistant') continue
    const index = msg.tools.findIndex((t) => t.id === callId)
    if (index !== -1) {
      msg.tools = msg.tools.map((t, j) => (j === index ? { ...t, result } : t))
      return
    }
  }
}

// ══════════════════════════════════════════════════════ 展示用

/**
 * `finish_reason` 到人话的映射。
 *
 * 为什么值得单独做一张表？因为有些结束方式**用户必须知道** ——
 * 它们意味着「上面那段回答是不可信的」。把 'length' 原样显示给用户
 * 等于什么都没说。
 *
 * ⚠️ 类型是 `Record<FinishReason, string>`：后端加一个新的结束原因时，
 *    这里不补就**编译不过**。这道强制同步机制是刻意留的 ——
 *    它比「记得去改前端」可靠。
 */
export const FINISH_LABEL: Record<FinishReason, string> = {
  stop: '正常结束',
  tool_calls: '正在调用工具',
  length: '撞到长度上限被截断，上面的内容可能不完整',
  content_filter: '被内容安全策略拦截',
  error: '异常终止',
  max_rounds: '工具调用次数达到上限，它被喊停了',
  awaiting_user: '在等你拍板',
}

/**
 * 从累积的 JSON 碎片里抠出 `code` 字段。
 *
 * 为什么要 try/catch？因为**流式过程中这个字符串一直是残缺的**：
 * arguments 是一段一段到的，任何中间时刻 `JSON.parse` 都会抛异常。
 * 这不是错误处理，这是正常状态 —— 参数还没流完而已。
 */
export function extractCode(args: string): string | null {
  const parsed = parseArgs(args)
  if (parsed && typeof parsed.code === 'string') return parsed.code
  return null
}

/**
 * 从**已完成**的 assistant 消息里，把某个 `ask_user` 调用的问题抠出来。
 *
 * 为什么需要它？因为决策卡片的 question / options 在实时路径上是
 * `decision_request` 事件给的，而在回放路径上是后端合并好的 `Decision`。
 * 两条路都有 —— 那这个函数是给谁用的？
 *
 * 给**流还没跑完、decision_request 还没到**的那一小段时间用的：
 * 界面要在模型刚要完工具、事件还没到齐时，就把「它准备问你点什么」
 * 显示出来，而不是先显示一张「正在执行…」的代码卡片再突然变成问题卡片。
 */
export function extractAskUser(args: string): {
  question: string
  options: string[]
} | null {
  const parsed = parseArgs(args)
  if (typeof parsed?.question !== 'string') return null
  const options = Array.isArray(parsed.options)
    ? parsed.options.filter((o): o is string => typeof o === 'string')
    : []
  return { question: parsed.question, options }
}

function parseArgs(args: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(args)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
    return null
  } catch {
    // 参数还没流完，或者本来就不是合法 JSON。两种都是正常状态。
    return null
  }
}

/** 日志里的消息 → 界面上要显示的文本。目前只用于调试，留着给 M5 的产物面板。 */
export function loggedMessageText(message: LoggedMessage): string {
  return message.content
}
