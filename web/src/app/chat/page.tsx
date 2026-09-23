'use client'

/**
 * M2 流式对话视图 —— 让 Agent 的思考过程逐字出现在屏幕上。
 *
 * ════════════════════════════════════════════════════════════════════
 * 这个组件在做一件反直觉的事：它把「一次请求」拆成了几百次渲染。
 * ════════════════════════════════════════════════════════════════════
 * 传统做法是「发请求 → 等几秒 → 一次性拿到完整回答 → 显示」。
 * 这里不一样：每收到一小段文本就更新一次 state，于是用户看到的字是
 * **流出来**的，而不是**蹦出来**的。
 *
 * 这带来的第一个工程问题就是：**状态什么时候推进？**
 *
 *   idle ──点击发送──> streaming ──收到 finish──> idle
 *                        │
 *                        └──收到 error──> 显示错误，仍然是 idle
 *
 * 注意这里只有一个布尔意义上的「忙/闲」，而不是 page.tsx 自检页那种四状态。
 * 因为在这个页面上，「有数据」和「在加载」是可以同时成立的 ——
 * 文字一边流，人一边读。这是流式界面和普通请求-响应界面在状态建模上的
 * 根本区别：**中间态本身就是最终态的一部分**。
 *
 * ════════════════════════════════════════════════════════════════════
 * 两条规矩（沿用自 M0 的自检页）
 * ════════════════════════════════════════════════════════════════════
 *   ① 零硬编码色值 —— 全部走 globals.css 的设计令牌
 *   ② 原始错误只进控制台，页面文案说人话 + 给下一步动作
 */

import Link from 'next/link'
import { useEffect, useRef, useState } from 'react'

import { streamChat, type ChatMessage } from '@/lib/sse'
import type { FinishReason, ToolResultEvent } from '@/lib/stream-types'

// ══════════════════════════════════════════════════════ 类型

/**
 * 界面上的一条消息。
 *
 * `id` 是本地生成的序号，**不能**用数组下标当 key ——
 * 流式更新时数组会不断变化，用下标做 key 会让 React 认错元素、
 * 造成输入框失焦、动画重放之类的怪问题。
 * 用一个只增不减的计数器，每个元素的身份就永远稳定。
 */
type Msg = {
  id: number
  role: 'user' | 'assistant'
  content: string
  /** 本轮的工具调用（M3 接入沙箱后才会出现，现在永远是空数组）。 */
  tools: ToolCallView[]
}

/**
 * 界面上呈现的一个工具调用。
 *
 * 注意它把「调用」和「结果」**合成了一条**，而后端是分成两个事件发的
 * （tool_call_delta / tool_result）。合并的理由：
 *
 *   分开存的话，界面渲染时要按 id 去另一个数组里查配对，
 *   而且「结果先到、调用后到」这种乱序会让查询落空。
 *   合成一条之后，`result === null` 天然就表示「还在跑」，
 *   不需要额外的状态字段。
 *
 * `id` 是配对的钥匙。后端的 ToolResult 带的是 call_id，而 call_id 只在
 * **第一个** tool_call_delta 碎片里出现 —— 所以累加时要记住它，
 * 不能像 name/args 那样无脑相加。
 */
type ToolCallView = {
  index: number
  id: string | null
  name: string
  /** 未解析的 JSON 字符串碎片，边流边拼。解析见 extractCode()。 */
  args: string
  result: ToolResultEvent | null
}

/** 上一轮跑完之后的统计，显示在回答下面。 */
type TurnMeta = {
  promptTokens: number
  completionTokens: number
  reason: FinishReason
  elapsedMs: number
}

// ══════════════════════════════════════════════════════ 常量

/**
 * finish_reason 到人话的映射。
 *
 * 为什么值得单独做一张表？因为 `length` 和 `error` 这两种结束方式
 * **用户必须知道** —— 它们意味着「上面那段回答是不可信的」。
 * 把 'length' 原样显示给用户等于什么都没说。
 */
const FINISH_LABEL: Record<FinishReason, string> = {
  stop: '正常结束',
  tool_calls: '正在调用工具',
  length: '撞到长度上限被截断，上面的内容可能不完整',
  content_filter: '被内容安全策略拦截',
  error: '异常终止',
  max_rounds: '工具调用次数达到上限，它被喊停了',
}

/**
 * 从累积的 JSON 碎片里把代码抠出来。
 *
 * 为什么要 try/catch？因为**流式过程中这个字符串一直是残缺的**：
 * arguments 是一段一段到的，任何中间时刻 `JSON.parse` 都会抛异常。
 * 这不是错误处理，这是正常状态 —— 参数还没流完而已。
 *
 * 返回 null 时界面显示「正在生成参数…」，而不是硬撑着显示一段半截 JSON。
 */
function extractCode(args: string): string | null {
  try {
    const parsed: unknown = JSON.parse(args)
    if (parsed && typeof parsed === 'object' && 'code' in parsed) {
      const code = (parsed as { code: unknown }).code
      if (typeof code === 'string') return code
    }
    return null
  } catch {
    return null
  }
}

/** 空状态下的引导语。降低第一次使用的门槛。 */
const SUGGESTIONS = [
  '用三句话解释什么是熵权法',
  '评价类问题一般怎么做？给个思路',
  '帮我算一下 1 到 100 的和',
]

/** id 计数器。放模块级而不是 state，是因为它不该触发重新渲染。 */
let nextId = 1

/**
 * 读单调时钟，返回毫秒。
 *
 * 为什么要单独抽成模块级的函数，而不是直接在 send() 里写 `performance.now()`？
 * 因为 React Compiler 的「纯度」检查（react-hooks/purity）会管住组件作用域内的
 * **一切**代码，包括定义在组件里的函数 —— 它要求同样的输入必然得到同样的输出，
 * 而「读时钟」天然违反这一条。抽到模块作用域，等于明确告诉编译器：
 * 这不是渲染逻辑，它的不确定性是有意为之。
 *
 * 为什么不用 Date.now()？因为 performance.now() 是**单调时钟**，
 * 不受系统时间调整影响（NTP 校时、用户手动改时间都可能让 Date.now() 倒退）。
 * 凡是测「耗时」，就该用单调时钟。
 */
function monotonicNow(): number {
  return performance.now()
}

// ══════════════════════════════════════════════════════ 组件

export default function ChatPage() {
  const [messages, setMessages] = useState<Msg[]>([])
  const [phase, setPhase] = useState<'idle' | 'streaming'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<TurnMeta | null>(null)
  const [draft, setDraft] = useState('')

  // 滚动容器。用 ref 直接操作 DOM，而不是把滚动位置也做成 state ——
  // 滚动位置每帧都在变，做成 state 会让整个消息列表疯狂重渲染。
  const scrollRef = useRef<HTMLDivElement>(null)

  // 用来取消正在进行的请求。useRef 而不是 useState：
  // 它只是个「手柄」，变了不需要重新渲染。
  const abortRef = useRef<AbortController | null>(null)

  const streaming = phase === 'streaming'

  // 新内容到达时自动滚到底部。
  // 依赖 messages 整体 —— 每来一个文字碎片 messages 都是新数组，于是每次都会滚。
  // 这正是我们想要的（流式输出时应当一直贴着底部）。
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  // ────────────────────────────────────────── 发送

  async function send(text: string) {
    const content = text.trim()
    // 空消息不发；已经在流式输出中也不发（防止叠加两条流）。
    if (!content || streaming) return

    const userMsg: Msg = { id: nextId++, role: 'user', content, tools: [] }
    // 先占好助手消息的位置，内容为空。用户会立刻看到一个「正在思考」的气泡，
    // 而不是点了发送之后界面毫无反应 —— 这个即时反馈很重要。
    const assistantId = nextId++

    // ⚠️ 这里必须是函数式更新 `(prev) => ...`，不能写成 [...messages, userMsg]。
    // 原因：`messages` 是这次渲染闭包里的快照。如果期间已有别的更新提交过，
    // 直接用快照会把它覆盖掉。函数式更新拿到的永远是**最新**的 state。
    // 流式场景下这个坑尤其致命，因为更新频率极高。
    setMessages((prev) => [
      ...prev,
      userMsg,
      { id: assistantId, role: 'assistant', content: '', tools: [] },
    ])
    setDraft('')
    setPhase('streaming')
    setError(null)
    setMeta(null)

    // 组装发给后端的对话历史。注意用 `messages`（本次渲染的快照）就够了 ——
    // 它正是「按下发送键那一刻」的历史，不含刚加进去的这两条。
    const history: ChatMessage[] = [
      ...messages.map((m) => ({ role: m.role, content: m.content })),
      { role: 'user', content },
    ]

    const controller = new AbortController()
    abortRef.current = controller
    const startedAt = monotonicNow()

    let promptTokens = 0
    let completionTokens = 0
    let reason: FinishReason = 'stop'

    try {
      for await (const event of streamChat(history, { signal: controller.signal })) {
        switch (event.type) {
          // ── 文本碎片：追加到助手消息末尾 ──
          case 'text_delta':
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId ? { ...m, content: m.content + event.text } : m,
              ),
            )
            break

          // ── 工具调用碎片：按 index 累积 ──
          case 'tool_call_delta':
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantId) return m

                if (!m.tools.some((t) => t.index === event.index)) {
                  return {
                    ...m,
                    tools: [
                      ...m.tools,
                      {
                        index: event.index,
                        id: event.id, // 只在首个碎片里出现
                        name: event.name ?? '',
                        args: event.arguments_delta,
                        result: null,
                      },
                    ],
                  }
                }

                return {
                  ...m,
                  tools: m.tools.map((t) =>
                    t.index === event.index
                      ? {
                          ...t,
                          // ⚠️ 注意 id 和下面两个的规则**不一样**：
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
              }),
            )
            break

          // ── 工具执行结果：认领到对应的那张卡片上 ──
          case 'tool_result':
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantId) return m

                const claimed = m.tools.map((t) =>
                  t.id === event.call_id ? { ...t, result: event } : t,
                )
                if (claimed.some((t) => t.result === event)) {
                  return { ...m, tools: claimed }
                }

                // 兜底：id 对不上时按顺序认领第一张还没结果的卡片。
                // 什么时候会对不上？有的兼容服务根本不返回工具调用 id，
                // 后端会用 "call_{index}" 补一个占位，跟前端拼出来的对不上号。
                // 并行调多个工具时这个兜底可能配错 —— 但配错的后果只是
                // 卡片和结果的对应关系乱了，文字内容不受影响。
                const index = claimed.findIndex((t) => t.result === null)
                if (index === -1) return m
                const tools = [...claimed]
                tools[index] = { ...tools[index], result: event }
                return { ...m, tools }
              }),
            )
            break

          case 'usage':
            promptTokens = event.prompt_tokens
            completionTokens = event.completion_tokens
            break

          case 'finish':
            reason = event.reason
            break

          // ── 带内错误：流已经开始，HTTP 状态码改不了了，只能在这里接 ──
          case 'error':
            // ⚠️ event.message 是「模型/网络层」的原始报错，可能夹带 API Key 片段，
            //    按规矩只进控制台，不进页面。
            console.error('流内错误：', event.message, '可重试:', event.retryable)
            setError('生成中断了。多半是网络抖动，重发一次通常就好。')
            break
        }
      }
    } catch (err) {
      // 用户自己点了「停止」不算错误 —— AbortError 应当静默忽略，
      // 否则用户会看到一个「操作被中止」的报错，莫名其妙。
      if (err instanceof DOMException && err.name === 'AbortError') {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId && m.content === ''
              ? { ...m, content: '（已停止）' }
              : m,
          ),
        )
      } else {
        // 走到这里说明是 fetch 层面的失败：后端没开、CORS 被拦、网络断了。
        console.error('请求失败：', err)
        setError('连不上后端。检查一下 uvicorn 是不是没开？')
      }
    } finally {
      // 无论成功、失败还是被中止，都要回到 idle，否则发送按钮永远锁着。
      setPhase('idle')
      abortRef.current = null
      // 只有在真的产出过内容时才显示统计，被中止的空回答没什么可展示的。
      if (completionTokens > 0) {
        setMeta({
          promptTokens,
          completionTokens,
          reason,
          elapsedMs: Math.round(monotonicNow() - startedAt),
        })
      }
    }
  }

  function stop() {
    abortRef.current?.abort()
  }

  // ────────────────────────────────────────── 渲染

  return (
    <main className="flex h-screen flex-col bg-plane">
      {/* ════════ 顶栏 ════════ */}
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-hairline bg-surface px-6 py-4">
        <div className="flex items-baseline gap-3">
          <h1 className="text-title font-bold tracking-tight text-ink">模型工坊</h1>
          <span className="text-caption text-ink-muted">对话 · M2 流式输出</span>
        </div>
        <Link
          href="/"
          className="rounded-control px-3 py-1.5 text-caption text-ink-secondary transition-colors hover:bg-plane hover:text-ink"
        >
          返回自检页
        </Link>
      </header>

      {/* ════════ 消息区 ════════ */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-8">
        <div className="mx-auto flex max-w-3xl flex-col gap-6">
          {/* 空状态 */}
          {messages.length === 0 && (
            <div className="mt-16 text-center">
              <p className="text-lead text-ink-secondary">
                蒟蒻agent在线，有什么数模问题尽管问。
              </p>
              <p className="mt-3 text-body text-ink-muted">
                它拿不准的地方会停下来问你，不会硬编。
              </p>
              <div className="mt-10 flex flex-wrap justify-center gap-3">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="rounded-control border border-hairline bg-surface px-4 py-2.5 text-body text-ink-secondary transition-colors hover:border-brand-300 hover:bg-brand-50 hover:text-ink"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m) => (
            <MessageBubble key={m.id} msg={m} streaming={streaming} />
          ))}

          {/* 首字还没到时的占位提示。
              模型从收到请求到吐出第一个字有几百毫秒延迟（实测 571ms），
              这段时间界面必须给出反馈，否则用户会以为卡死了。 */}
          {streaming &&
            messages[messages.length - 1]?.content === '' &&
            messages[messages.length - 1]?.tools.length === 0 && (
              <div className="flex items-center gap-3 text-body text-ink-muted">
                <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-grid border-t-brand-500" />
                蒟蒻正在思考……
              </div>
            )}

          {/* 错误提示。文案规矩：说人话 + 给下一步动作。 */}
          {error && (
            <div className="rounded-card border border-hairline bg-surface p-5">
              <p className="flex items-center gap-3 text-body font-medium text-critical-ink">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-critical" />
                {error}
              </p>
            </div>
          )}

          {/* 上一轮的统计 */}
          {meta && !streaming && (
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-hairline pt-4 text-caption text-ink-muted">
              <span>
                输入 <span className="font-mono text-ink-secondary">{meta.promptTokens}</span> tokens
              </span>
              <span>
                输出 <span className="font-mono text-ink-secondary">{meta.completionTokens}</span> tokens
              </span>
              <span>
                耗时 <span className="font-mono text-ink-secondary">{meta.elapsedMs}</span> ms
              </span>
              <span
                className={
                  meta.reason === 'stop' ? 'text-ink-muted' : 'font-medium text-warning-ink'
                }
              >
                {FINISH_LABEL[meta.reason]}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ════════ 输入区 ════════ */}
      <footer className="shrink-0 border-t border-hairline bg-surface px-6 py-5">
        <form
          className="mx-auto flex max-w-3xl items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            void send(draft)
          }}
        >
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              // Enter 发送，Shift+Enter 换行 —— 聊天框的通用约定。
              // 注意要判断 isComposing：中文输入法选词时按 Enter 是「确认候选词」，
              // 不是「发送」。不判断的话，用拼音打字时选词会直接把没打完的句子发出去。
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                void send(draft)
              }
            }}
            rows={2}
            placeholder="问点什么……（Enter 发送，Shift+Enter 换行）"
            className="flex-1 resize-none rounded-card border border-hairline bg-plane px-4 py-3 text-body text-ink placeholder:text-ink-muted focus:border-brand-400 focus:outline-none"
          />

          {streaming ? (
            <button
              type="button"
              onClick={stop}
              className="h-[3.25rem] shrink-0 rounded-control border border-hairline bg-raised px-6 text-body font-medium text-ink-secondary transition-colors hover:text-ink"
            >
              停止
            </button>
          ) : (
            <button
              type="submit"
              disabled={!draft.trim()}
              className="h-[3.25rem] shrink-0 rounded-control bg-brand-500 px-6 text-body font-medium text-white transition-colors hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-40"
            >
              发送
            </button>
          )}
        </form>
      </footer>
    </main>
  )
}

// ══════════════════════════════════════════════════════ 子组件

/**
 * 一条消息气泡。
 *
 * 拆成独立组件不是「为了看起来结构清晰」，而是有实际收益：
 * 用户消息的 props 在流式过程中**从不变化**，而助手消息每来一个字就变一次。
 * 拆开之后，React 可以跳过所有用户消息的重新渲染 —— 它们的内容引用没变。
 * （要真正吃到这个优化还差一个 React.memo，等消息多到卡顿再加。）
 */
function MessageBubble({ msg, streaming }: { msg: Msg; streaming: boolean }) {
  const isUser = msg.role === 'user'

  // 「正在进行」= 这条是最后一条助手消息，且全局还在流式状态。
  // 用它来决定要不要画那个闪烁的光标。
  const isLive = !isUser && streaming && msg.content !== ''

  return (
    <div className={isUser ? 'flex justify-end' : ''}>
      {!isUser && (
        <p className="mb-2 flex items-center gap-2 text-caption font-medium text-ink-muted">
          <span className="h-2 w-2 rounded-full bg-brand-500" />
          蒟蒻
        </p>
      )}

      <div
        className={
          isUser
            ? 'max-w-[85%] rounded-card rounded-br-sm bg-brand-500 px-5 py-3.5 text-body whitespace-pre-wrap text-white'
            : 'text-body whitespace-pre-wrap text-ink'
        }
      >
        {msg.content}
        {/* 光标用 inline-block 而不是绝对定位 —— 它会自然跟在最后一个字后面，
            不需要知道文字的宽度。align-middle 让它和汉字基线对齐。 */}
        {isLive && (
          <span className="ml-0.5 inline-block h-[1.1em] w-[2px] animate-pulse bg-brand-500 align-middle" />
        )}
      </div>

      {/* 工具执行卡片 */}
      {msg.tools.length > 0 && (
        <div className="mt-4 flex flex-col gap-3">
          {msg.tools.map((t) => (
            <ToolCallCard key={t.index} call={t} />
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * 一张工具执行卡片：状态头 + 代码 + 输出。
 *
 * 这是 M3 最重要的界面元素 —— 它把「Agent 在后台干了什么」摊开给用户看。
 * 没有它，用户只能看到模型说「我算出来是 5050」，无从判断这个数是怎么来的。
 */
function ToolCallCard({ call }: { call: ToolCallView }) {
  // 折叠状态交给 React 管（受控），而不是让 <details> 自己管（非受控）。
  // 非受控的话会有一个很隐蔽的坑：组件每次重渲染，React 都会把 open 属性
  // 按 vdom 里的值重新写一遍，把用户手动折叠的状态冲掉。
  // 受控写法多三行，但行为是可预期的。
  const [open, setOpen] = useState(true)

  const result = call.result
  const code = extractCode(call.args)

  // 五种状态，用一张表而不是散落的 if 链。
  // 散落的 if 会漏掉组合情况（比如「超时」和「沙箱报错」同时为真时该画什么），
  // 而这里是从上往下第一个匹配的胜出，顺序即优先级。
  const status = !result
    ? { dot: 'bg-brand-500', ink: 'text-brand-600', label: '正在执行…', running: true }
    : result.error
      ? { dot: 'bg-critical', ink: 'text-critical-ink', label: '沙箱未能执行', running: false }
      : result.timed_out
        ? { dot: 'bg-warning', ink: 'text-warning-ink', label: '执行超时被终止', running: false }
        : result.ok
          ? { dot: 'bg-good', ink: 'text-good-ink', label: '执行成功', running: false }
          : { dot: 'bg-serious', ink: 'text-serious-ink', label: '运行出错', running: false }

  // 状态色永远配「圆点或转圈 + 文字」，不靠颜色单独传达信息 ——
  // 这是设计系统定下的硬性约束（见 globals.css 的注释）。
  const indicator = status.running ? (
    <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-grid border-t-brand-500" />
  ) : (
    <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${status.dot}`} />
  )

  // stderr 的颜色取决于「这次算不算失败」：
  //   失败 → 用警示色（这是要看的报错）
  //   成功 → 用弱化色（多半只是 numpy 的 DeprecationWarning 之类，别吓唬人）
  const stderrInk = result?.ok ? 'text-ink-muted' : 'text-serious-ink'

  return (
    <div className="overflow-hidden rounded-card border border-hairline bg-surface">
      {/* ── 状态头：始终可见，不看细节也能知道跑没跑成 ── */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-caption">
        {indicator}
        <code className="font-medium text-ink">{call.name || '…'}</code>
        <span className={status.ink}>{status.label}</span>
        {result && (
          <span className="ml-auto font-mono text-ink-muted">
            {result.duration_ms} ms
            {result.exit_code !== null && ` · 退出码 ${result.exit_code}`}
          </span>
        )}
      </div>

      {/* ── 主体：代码 + 输出，可折叠 ── */}
      <details
        open={open}
        onToggle={(e) => {
          const next = e.currentTarget.open
          // onToggle 在挂载时也会触发一次，这时候状态没变，跳过以避免多余渲染
          if (next !== open) setOpen(next)
        }}
        className="border-t border-hairline"
      >
        <summary className="cursor-pointer select-none px-4 py-2 text-caption text-ink-muted transition-colors hover:text-ink-secondary">
          代码与输出
        </summary>

        <pre className="overflow-x-auto border-t border-hairline px-4 py-3 text-caption leading-relaxed text-ink-secondary">
          {code ?? '（参数还在生成中……）'}
        </pre>

        {result && (
          <div className="border-t border-hairline bg-plane">
            {result.error && (
              <p className="px-4 py-3 text-caption leading-relaxed text-critical-ink">
                {result.error}
              </p>
            )}

            {result.stdout && (
              <pre className="max-h-72 overflow-auto px-4 py-3 text-caption leading-relaxed text-ink-secondary">
                {result.stdout}
              </pre>
            )}

            {result.stderr && (
              <pre
                className={`max-h-72 overflow-auto px-4 py-3 text-caption leading-relaxed ${stderrInk} ${
                  result.stdout ? 'border-t border-hairline' : ''
                }`}
              >
                {result.stderr}
              </pre>
            )}

            {!result.stdout && !result.stderr && !result.error && (
              <p className="px-4 py-3 text-caption text-ink-muted">
                （没有任何输出。它大概是忘了 print。）
              </p>
            )}
          </div>
        )}
      </details>
    </div>
  )
}
