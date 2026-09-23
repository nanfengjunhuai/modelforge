'use client'

/**
 * 对话页 —— M4 之后它由**会话**驱动，不再由浏览器的内存驱动。
 *
 * ════════════════════════════════════════════════════════════════════
 * 这一版和 M2/M3 那一版最根本的区别
 * ════════════════════════════════════════════════════════════════════
 * M3 的页面是这样工作的：
 *
 *     整个对话历史活在 React 的 state 里 → 每次发送时全量重发给后端
 *
 * 所以刷新页面 = 历史全没了。而「刷新不丢」恰恰是 M4 要证明的事。
 *
 * 现在状态归后端所有，前端只是一个**视图**：
 *
 *     挂载时：URL 里有 ?s=<id> 就 GET 回来重建；没有就建一个新的
 *     发送时：只发这一条新消息（历史在后端的事件日志里）
 *     刷新时：重新 GET，界面一模一样地长回来
 *
 * 对应的那句产品承诺是「服务重启不丢进度」，而它在客户端就体现为
 * 「刷新不丢」—— 因为这两件事背后是同一件事：**状态在盘上，不在内存里**。
 *
 * ════════════════════════════════════════════════════════════════════
 * 三种「忙」要分清楚
 * ════════════════════════════════════════════════════════════════════
 *   connecting —— 正在确定会话（挂载后的头几百毫秒）
 *   streaming  —— 有一条流正在跑（`state.liveKey !== null`）
 *   awaiting   —— **不是忙**，是「等用户拍板」（推导出来的，不是存的）
 *
 * 第三种特别容易写错：它看起来像「卡住了」，实际上是「该你了」。
 * 界面上的表现完全不同 —— 输入框要锁住，但锁的理由要写出来
 * （在输入框的 placeholder 和决策卡片上），而不是显示一个转圈。
 *
 * ════════════════════════════════════════════════════════════════════
 * 两条规矩（沿用自 M0 的自检页）
 * ════════════════════════════════════════════════════════════════════
 *   ① 零硬编码色值 —— 全部走 globals.css 的设计令牌
 *   ② 原始错误只进控制台，页面文案说人话 + 给下一步动作
 */

import Link from 'next/link'
import { useEffect, useRef, useState } from 'react'

import {
  abortStream,
  applyEvent,
  emptyChat,
  failWith,
  fromSessionDetail,
  markDecision,
  newAssistantKey,
  pendingDecision,
  startAssistant,
  startTurn,
  type ChatState,
} from '@/lib/chat-state'
import { createSession, getSession } from '@/lib/sessions'
import { streamDecision, streamResume, streamSessionMessage } from '@/lib/sse'
import type { StreamEvent } from '@/lib/stream-types'

import { MessageBubble } from './parts'

// ══════════════════════════════════════════════════════ URL 里的会话 id

/**
 * 从地址栏里读会话 id。
 *
 * ⚠️ 为什么不用 Next.js 的 `useSearchParams()`？
 * 因为在这个版本里它**总是**需要一个 `<Suspense>` 边界 —— 搜索参数在构建时
 * 未知，会让整页退化成需要 fallback（见 `node_modules/next/dist/docs/`
 * 里 `instant-navigation.md` 的说明）。而我们只在**挂载时读一次**这个值，
 * 之后由 state 持有，不需要它响应式地跟着 URL 变。
 * 为一个只读一次的值引入一层 Suspense 边界不划算。
 *
 * 放在模块作用域（而不是组件里）还有第二个理由：它读的是 `window`，
 * 天然不纯 —— 而组件作用域内的一切都被 React Compiler 要求可重入。
 * 放在这里等于明确告诉编译器：这不是渲染逻辑。
 */
function readSessionIdFromUrl(): string | null {
  if (typeof window === 'undefined') return null
  return new URLSearchParams(window.location.search).get('s')
}

/**
 * 把会话 id 写进地址栏（或从地址栏抹掉）。
 *
 * 用 `replaceState` 而不是 `pushState`：用户并没有「导航」到别处，
 * 只是这个页面刚刚获得了身份。用 push 的话，浏览器后退键会把用户
 * 带回一个「还没有会话」的状态，然后页面又自动建一个新的 —— 纯困惑。
 *
 * Next.js 的 Router 会感知到 replaceState（见官方文档
 * `04-linking-and-navigating.md`），所以 `usePathname` 之类的 hook 保持同步。
 */
function setSessionIdInUrl(sessionId: string | null): void {
  const url = new URL(window.location.href)
  if (sessionId) url.searchParams.set('s', sessionId)
  else url.searchParams.delete('s')
  window.history.replaceState(null, '', url)
}

// ══════════════════════════════════════════════════════ 常量

/** 空状态下的引导语。挑的都是**会把决策点跑出来**的题 —— 便于验证 HITL。 */
const SUGGESTIONS = [
  '帮我选一个评价类模型，数据挺全的',
  '这道预测题该用 ARIMA 还是 LSTM？',
  '帮我算一下 1 到 100 的平方和',
]

/**
 * 把异常变成一句给用户看的话。
 *
 * `fetch` 在网络层失败时抛的是 `TypeError`（"Failed to fetch"）——
 * 那句话对用户毫无意义，真正的原因多半是后端没开。
 *
 * 而 HTTP 错误（4xx/5xx）抛出来的 message 是**后端写好人话的 detail**
 * （比如「还有一个问题在等你拍板，先回答它再发新消息」），要原样透出来。
 * 这是后端把用户级文案放在 HTTPException.detail 里的回报。
 */
function describeFailure(err: unknown): string {
  if (err instanceof TypeError) {
    return '连不上后端。检查一下 uvicorn 是不是没开？'
  }
  if (err instanceof Error && err.message) {
    return err.message
  }
  return '出了点问题，重试一次看看。'
}

/** 最后一条消息已经有可见内容了吗 —— 决定要不要显示「正在思考」的转圈。 */
function hasVisibleContent(state: ChatState): boolean {
  const last = state.messages[state.messages.length - 1]
  if (!last) return false
  return last.content !== '' || last.tools.length > 0
}

// ══════════════════════════════════════════════════════ 组件

export default function ChatPage() {
  const [state, setState] = useState<ChatState>(emptyChat)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [bootPhase, setBootPhase] = useState<'connecting' | 'ready' | 'failed'>('connecting')
  const [bootError, setBootError] = useState<string | null>(null)
  const [submittingCallId, setSubmittingCallId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  // 滚动容器。用 ref 直接操作 DOM，而不是把滚动位置也做成 state ——
  // 滚动位置每帧都在变，做成 state 会让整个消息列表疯狂重渲染。
  const scrollRef = useRef<HTMLDivElement>(null)

  // 取消正在进行的请求。useRef 而不是 useState：它只是个「手柄」，
  // 变了不需要重新渲染。
  const abortRef = useRef<AbortController | null>(null)

  // ⚠️ 这个 ref 守卫的是 **React 开发模式的 StrictMode 双调用**。
  //
  // StrictMode 会故意把 effect 跑两遍（挂载 → 卸载 → 再挂载），
  // 目的是让「没写清理逻辑」的 bug 暴露出来。但我们的 effect 会
  // **POST 建一个新会话** —— 跑两遍就建出两个幽灵会话，
  // 其中一个永远没人用，却会出现在将来的会话列表里。
  //
  // 这里不能用「卸载时清理」来解决（那会删掉正确的那个），
  // 所以用一个 ref 记住「已经启动过了」。生产构建下 StrictMode 不双调用。
  const bootedRef = useRef(false)

  const streaming = state.liveKey !== null
  const awaiting = pendingDecision(state) !== null

  // 新内容到达时自动滚到底部。依赖 state.messages 整体 ——
  // 每来一个文字碎片它都是新数组，于是每次都会滚。这正是我们想要的。
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [state.messages])

  // ────────────────────────────────────────── 启动

  /**
   * 确定「这个页面属于哪个会话」。
   *
   * 两条路，区别只在 URL 里有没有 `?s=`：
   *   · 有 → 拉回来重建（**刷新页面走的是这条**）
   *   · 没有 → 建一个新的，并把 id 写进 URL，这样刷新之后还能找回来
   *
   * 第二条里那个「写 URL」是整个机制的关键一环：不写的话，刷新就变成
   * 「开一个新会话」，用户会以为历史丢了。
   */
  async function boot() {
    try {
      const existing = readSessionIdFromUrl()
      if (existing) {
        const detail = await getSession(existing)
        setState(fromSessionDetail(detail))
        setSessionId(existing)
        setBootPhase('ready')
        return
      }
      const session = await createSession()
      setSessionIdInUrl(session.id)
      setSessionId(session.id)
      setBootPhase('ready')
    } catch (err) {
      console.error('初始化会话失败：', err)
      setBootError(describeFailure(err))
      setBootPhase('failed')
    }
  }

  // ⚠️ 这个 effect 刻意放在 `boot` 的**定义之后**。
  //
  // 函数声明会提升，所以写在前面运行时也能跑 —— 但 React Compiler 的
  // `immutability` 规则会报错：「`boot` is accessed before it is declared,
  // which prevents the earlier access from updating when this value changes
  // over time」。它说的是：如果 `boot` 哪天变成了一个会变化的绑定
  // （比如 useCallback 的返回值），那个提前的引用就会固化成旧的那份。
  //
  // 把 effect 挪到定义之后，这个隐患就不存在了。而且可读性也更好 ——
  // 「启动时做什么」紧挨着「启动函数长什么样」。
  useEffect(() => {
    if (bootedRef.current) return
    bootedRef.current = true
    void boot()
  }, [])

  /**
   * 开一个全新的会话。
   *
   * 先清 URL 再 boot —— 不清的话 boot 会读到那个旧的（可能已经失效的）
   * id，然后再次失败，用户点多少次都没用。
   */
  async function newSession() {
    setSessionIdInUrl(null)
    setState(emptyChat())
    setSessionId(null)
    setBootError(null)
    setBootPhase('connecting')
    bootedRef.current = true
    await boot()
  }

  // ────────────────────────────────────────── 跑一条流

  /**
   * 把一条事件流接进状态里。发送消息、提交决策、重试都走这里 ——
   * 因为「怎么读流」和「怎么折进状态」跟流的来源无关。
   *
   * `onAccepted` 在**第一个事件到达时**调用一次，它的语义是
   * 「HTTP 层已经接受了这个请求」，而不是「运行完了」。
   * 这个区别在提交决策时很关键：
   *
   *     · 请求被接受（2xx）→ 答案已经在后端落盘了，卡片可以翻成「已拍板」
   *     · 请求被拒绝（409/422）→ 什么都没发生，卡片必须保持可点
   *
   * 只靠 try/catch 分不出这两者 —— 因为流**中途**的错误也是 catch 到的，
   * 而那些错误发生时答案已经落盘了。所以用「第一个事件」当分界线。
   */
  async function runStream(
    makeIter: (signal: AbortSignal) => AsyncGenerator<StreamEvent>,
    onAccepted?: () => void,
  ) {
    const controller = new AbortController()
    abortRef.current = controller

    let accepted = false
    try {
      for await (const event of makeIter(controller.signal)) {
        if (!accepted) {
          accepted = true
          onAccepted?.()
        }
        // ⚠️ 必须是函数式更新 `(prev) => ...`，不能写成 `applyEvent(state, event)`。
        // 原因：`state` 是这次渲染闭包里的快照。流式场景下更新频率极高，
        // 直接用快照会丢掉期间提交的其它更新。
        setState((prev) => applyEvent(prev, event))
      }
    } catch (err) {
      // 用户自己点了「停止」不算错误 —— AbortError 应当静默忽略，
      // 否则用户会看到一个「操作被中止」的报错，莫名其妙。
      if (err instanceof DOMException && err.name === 'AbortError') {
        setState(abortStream)
      } else {
        console.error('请求失败：', err)
        setState((prev) => failWith(prev, describeFailure(err)))
      }
    } finally {
      abortRef.current = null
      // 兜底：正常路径下 liveKey 已经由 finish / abort / error 三个分支清掉了。
      // 这里防的是「流结束了但一个 finish 都没发」—— 那是契约被破坏，
      // 真发生了就该修后端，但不该让界面永远卡在「正在思考」的转圈上。
      setState((prev) => (prev.liveKey ? { ...prev, liveKey: null } : prev))
    }
  }

  // ────────────────────────────────────────── 三个动作

  async function send(text: string) {
    const content = text.trim()
    // 四种不发的情况：空消息、会话还没建好、已经在流、在等用户拍板。
    //
    // 最后一种很重要 —— 后端也会用 409 挡住（那是**正确性**），
    // 但让用户先按一下再报错是很差的体验（这是**体验**）。两者都要有。
    if (!content || !sessionId || streaming || awaiting) return

    setDraft('')
    const assistantKey = newAssistantKey()
    setState((prev) => startTurn(prev, content, assistantKey))

    await runStream((signal) => streamSessionMessage(sessionId, content, signal))
  }

  async function decide(callId: string, choice: string, note: string) {
    if (!sessionId || streaming || submittingCallId) return

    setSubmittingCallId(callId)
    const assistantKey = newAssistantKey()
    // 提交决策之后**不加用户气泡** —— 用户刚才是点了卡片上的按钮，
    // 不是在输入框里说话。多一个气泡会让人以为那句话是自己打的。
    setState((prev) => startAssistant(prev, assistantKey))

    try {
      await runStream(
        (signal) => streamDecision(sessionId, { callId, choice, note }, signal),
        // 请求被接受之后卡片就可以翻成「已拍板」了：答案已经在后端落盘。
        () => setState((prev) => markDecision(prev, callId, choice, note)),
      )
    } finally {
      setSubmittingCallId(null)
    }
  }

  /**
   * 按现有历史重开一条流，**不追加任何事件**。
   *
   * 这是 ADR-004 那条重试路径在界面上的落点。最容易遇到的场景：
   * 用户点了决策卡片，答案已经落盘，但续流的请求在网络层抖了一下 ——
   * 此时**决策已经答过了**，再点一次卡片会被「一个 call_id 只能回答一次」
   * 的数据库约束挡回来。没有这个按钮的话，用户只能另发一条消息，
   * 而模型的历史里那条 tool 结果就永远没有后续了。
   *
   * 它能这么便宜地存在，正是因为状态在盘上：
   * 「重试」= 照着事件日志重建一次，不需要恢复任何进程内的东西。
   */
  async function retry() {
    if (!sessionId || streaming) return
    setState((prev) => ({ ...prev, error: null }))
    await runStream((signal) => streamResume(sessionId, signal))
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
          <span className="text-caption text-ink-muted">对话 · M4 人在环中</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => void newSession()}
            disabled={bootPhase === 'connecting'}
            className="rounded-control px-3 py-1.5 text-caption text-ink-secondary transition-colors hover:bg-plane hover:text-ink disabled:opacity-40"
          >
            新会话
          </button>
          <Link
            href="/"
            className="rounded-control px-3 py-1.5 text-caption text-ink-secondary transition-colors hover:bg-plane hover:text-ink"
          >
            返回自检页
          </Link>
        </div>
      </header>

      {/* ════════ 消息区 ════════ */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-8">
        <div className="mx-auto flex max-w-3xl flex-col gap-6">
          {/* ── 状态一：正在确定会话 ── */}
          {bootPhase === 'connecting' && (
            <div className="mt-16 flex items-center justify-center gap-3 text-body text-ink-muted">
              <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-grid border-t-brand-500" />
              正在准备工作区……
            </div>
          )}

          {/* ── 状态二：会话没建起来 ── */}
          {bootPhase === 'failed' && (
            <div className="mt-16 rounded-card border border-hairline bg-surface p-6">
              <p className="flex items-center gap-3 text-body font-medium text-critical-ink">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-critical" />
                {bootError}
              </p>
              <p className="mt-3 text-caption text-ink-muted">
                工作区没能建起来，所以现在发不了消息。
              </p>
              <button
                type="button"
                onClick={() => void newSession()}
                className="mt-5 rounded-control bg-brand-500 px-4 py-2.5 text-body font-medium text-white transition-colors hover:bg-brand-600"
              >
                重新建立工作区
              </button>
            </div>
          )}

          {/* ── 状态三：空会话 ── */}
          {bootPhase === 'ready' && state.messages.length === 0 && (
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
                    type="button"
                    onClick={() => void send(s)}
                    className="rounded-control border border-hairline bg-surface px-4 py-2.5 text-body text-ink-secondary transition-colors hover:border-brand-300 hover:bg-brand-50 hover:text-ink"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {state.messages.map((m) => (
            <MessageBubble
              key={m.key}
              msg={m}
              streaming={streaming}
              submittingCallId={submittingCallId}
              onDecide={(callId, choice, note) => void decide(callId, choice, note)}
            />
          ))}

          {/* 首字还没到时的占位提示。
              模型从收到请求到吐出第一个字有几百毫秒延迟（实测 571ms），
              这段时间界面必须给出反馈，否则用户会以为卡死了。 */}
          {streaming && !hasVisibleContent(state) && (
            <div className="flex items-center gap-3 text-body text-ink-muted">
              <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-grid border-t-brand-500" />
              蒟蒻正在思考……
            </div>
          )}

          {state.error && (
            <div className="rounded-card border border-hairline bg-surface p-5">
              <p className="flex items-center gap-3 text-body font-medium text-critical-ink">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-critical" />
                {state.error}
              </p>
              {sessionId && (
                <button
                  type="button"
                  onClick={() => void retry()}
                  className="mt-4 rounded-control border border-hairline bg-raised px-4 py-2 text-body text-ink-secondary transition-colors hover:text-ink"
                >
                  重试这一轮
                </button>
              )}
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
              // 不是「发送」。不判断的话，用拼音打字时选词会把没打完的句子发出去。
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                void send(draft)
              }
            }}
            rows={2}
            // 有决策待拍板时输入框锁住，并把理由**写在 placeholder 里** ——
            // 光禁用不解释，用户会对着一动不动的输入框发懵。
            disabled={streaming || awaiting}
            placeholder={
              awaiting
                ? '先回答上面的问题，蒟蒻才能接着往下算'
                : '问点什么……（Enter 发送，Shift+Enter 换行）'
            }
            className="flex-1 resize-none rounded-card border border-hairline bg-plane px-4 py-3 text-body text-ink placeholder:text-ink-muted focus:border-brand-400 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
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
              disabled={!draft.trim() || awaiting || bootPhase !== 'ready'}
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
