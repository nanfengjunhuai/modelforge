'use client'

/**
 * 工作台外壳 —— 三栏：会话清单 · 对话 · 产物。
 *
 * ════════════════════════════════════════════════════════════════════
 * 这一版和 M4 那一版最根本的区别
 * ════════════════════════════════════════════════════════════════════
 * M4 的页面是「一个页面一个会话」：它自己建一个会话、自己跑它、刷新靠 URL 找回来。
 * 到了 M5b，会话成了**一个可以来回切的东西** —— 于是多出来的全是
 * 「切换时会发生什么」这一类问题（见下面「三处竞态」）。
 *
 * 状态仍然归这个文件所有，没有引入 context 或状态库。理由是具体的：
 * 工具卡片里的「在工作台里打开」和右侧面板必须共享「选中了哪个产物」，
 * 而 `sessionId` 已经在走同一条三层 props 链路了。再加一层间接解决不了
 * 任何问题，只会让「这个值从哪来」变得难查。
 *
 * ════════════════════════════════════════════════════════════════════
 * 三处竞态 —— 全都是「不报错但不对」那一类
 * ════════════════════════════════════════════════════════════════════
 * **① 切会话时的乱序响应。** 用户快速点了 A 再点 B，两个 `getSession`
 *    同时在飞。A 的响应后到，就会把 B 的界面内容覆盖掉 ——
 *    表现是「我点的是 B，显示的却是 A」，而且不报任何错。
 *    用一个单调递增的 `loadSeqRef` 当请求令牌，过期的响应直接丢掉。
 *
 * **② 切走时正在跑的流。** 不掐掉的话，那条流的后续事件会写进
 *    **已经换掉的那个会话**的界面状态里。用 `streamSeqRef` 做同样的令牌，
 *    并且真的 `abort()`（省掉还没到的字节）。
 *
 * **③ 开发模式 effect 跑两遍。** `bootedRef` 防的是 StrictMode 把
 *    挂载跑两遍时**建出两个幽灵会话** —— 其中一个永远没人用，
 *    却会出现在会话列表里。这个守卫在重构里被保留了，位置没挪。
 *
 * ════════════════════════════════════════════════════════════════════
 * 两条规矩（沿用自 M0 的自检页）
 * ════════════════════════════════════════════════════════════════════
 *   ① 零硬编码色值 —— 全部走 globals.css 的设计令牌
 *   ② 原始错误只进控制台，页面文案说人话 + 给下一步动作
 */

import { useEffect, useRef, useState } from 'react'

import {
  abortStream,
  allArtifacts,
  applyEvent,
  applyReport,
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
import type { LogReportEvent, Session } from '@/lib/log-types'
import type { ReportDocument } from '@/lib/report-types'
import {
  applyReportFrame,
  emptyProgress,
  progressFromDocument,
  startProgress,
  streamReport,
  type ReportProgress,
} from '@/lib/report-stream'
import {
  artifactUrl,
  createSession,
  deleteSession,
  getSession,
  listSessions,
} from '@/lib/sessions'
import { streamDecision, streamResume, streamSessionMessage } from '@/lib/sse'
import type { StreamEvent } from '@/lib/stream-types'

import { Conversation } from './conversation'
import { ArtifactPanel } from './panel'
import { ReportOverlay } from './report'
import { SessionSidebar } from './sidebar'

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
 * 天然不纯 —— 而组件作用域内的一切都被要求可重入。
 * 放在这里等于明确告诉读者：这不是渲染逻辑。
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
 * ⚠️ 切会话也走这里，所以**后退键不会回到上一个会话**。这是刻意的：
 *    会话切换在这个产品里更像「换文档」而不是「翻页」。
 */
function setSessionIdInUrl(sessionId: string | null): void {
  const url = new URL(window.location.href)
  if (sessionId) url.searchParams.set('s', sessionId)
  else url.searchParams.delete('s')
  window.history.replaceState(null, '', url)
}

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

// ══════════════════════════════════════════════════════ 组件

export default function ChatPage() {
  // ── 对话 ──
  const [state, setState] = useState<ChatState>(emptyChat)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [bootPhase, setBootPhase] = useState<'connecting' | 'ready' | 'failed'>(
    'connecting',
  )
  const [bootError, setBootError] = useState<string | null>(null)
  const [submittingCallId, setSubmittingCallId] = useState<string | null>(null)

  // ── 工作台外壳 ──
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [panelOpen, setPanelOpen] = useState(true)
  const [openArtifactId, setOpenArtifactId] = useState<string | null>(null)

  // ── 报告（M6a）──
  //
  // `progress` 是**界面唯一认的那一份**：正在生成时由流一帧帧喂，
  // 打开一份旧报告时由 `.report.json` 一次性灌。两条路都汇到这一个状态，
  // 所以「刚写的」和「昨天写的」渲染出来一模一样。
  const [progress, setProgress] = useState<ReportProgress | null>(null)
  const [reportOpen, setReportOpen] = useState(false)

  // 取消正在进行的请求。useRef 而不是 useState：它只是个「手柄」，
  // 变了不需要重新渲染。
  const abortRef = useRef<AbortController | null>(null)
  const reportAbortRef = useRef<AbortController | null>(null)

  // ⚠️ 三个请求令牌，防的是上面「三处竞态」的 ① 和 ②。
  // 用递增的整数而不是布尔：连续切三次会话时，需要知道
  // 「现在的最新一次是哪一次」，而不只是「有没有被顶掉」。
  const loadSeqRef = useRef(0)
  const streamSeqRef = useRef(0)
  // ③ 报告流有它自己的令牌和 abort 手柄，**不和聊天流共用** ——
  //    两条流的生命周期完全独立（报告不消耗对话轮次，也不写对话历史），
  //    共用一个手柄的话，切会话时先 abort 谁、令牌归谁都会变得说不清。
  const reportSeqRef = useRef(0)

  // ⚠️ 这个 ref 守卫的是 **React 开发模式的 StrictMode 双调用**。
  //
  // StrictMode 会故意把 effect 跑两遍（挂载 → 卸载 → 再挂载），
  // 目的是让「没写清理逻辑」的 bug 暴露出来。但我们的 effect 会
  // **POST 建一个新会话** —— 跑两遍就建出两个幽灵会话，
  // 其中一个永远没人用，却会出现在左边那一栏里。
  //
  // 这里不能用「卸载时清理」来解决（那会删掉正确的那个），
  // 所以用一个 ref 记住「已经启动过了」。生产构建下 StrictMode 不双调用。
  const bootedRef = useRef(false)

  const streaming = state.liveKey !== null
  const awaiting = pendingDecision(state) !== null
  const artifacts = allArtifacts(state)
  const currentSession = sessions.find((s) => s.id === sessionId) ?? null

  // ────────────────────────────────────────── 会话清单

  /**
   * 重新拉一遍会话列表。
   *
   * 调用时机是**显式**的（挂载后 / 建会话后 / 每轮跑完后 / 删会话后），
   * 而不是「监听某个状态变了就刷」—— 后者会写成一个 effect，
   * 而 effect 里的 setState 既多渲染一轮，又容易和别的更新互相触发。
   *
   * 标题取自第一条用户消息，所以它会在**第一轮结束时**才变 ——
   * 这正是「每轮跑完后刷一次」要覆盖的场景。
   */
  async function refreshSessions() {
    try {
      setSessions(await listSessions())
    } catch (err) {
      // 列表拉不到不该打断对话：用户正在读的东西比侧栏重要得多。
      console.error('拉取会话列表失败：', err)
    } finally {
      setSessionsLoading(false)
    }
  }

  /**
   * 掐掉当前这条流（如果有）。
   *
   * 两件事都要做，而且都要**立即**做：
   *   · `abort()` —— 省掉还没到的字节，也让服务端知道没人听了
   *   · 递增令牌 —— 已经排进微任务队列的事件会被下面的守卫丢掉
   *
   * 只做第一件的话，abort 之前已经 yield 出来、正排在微任务队列里的
   * 那几个事件仍然会进来，写进一个已经换掉的界面状态里。
   */
  function invalidateStream() {
    streamSeqRef.current += 1
    abortRef.current?.abort()
    abortRef.current = null

    // 报告流也要一起掐。切走之后它还往这里推帧的话，新会话的界面里
    // 会长出一份**别人**的报告 —— 而且它看起来完全正常，只是内容对不上。
    reportSeqRef.current += 1
    reportAbortRef.current?.abort()
    reportAbortRef.current = null
  }

  // ────────────────────────────────────────── 会话生命周期

  /**
   * 打开一个已有会话 —— 就是「刷新页面重建界面」那条路，只是 id 由人给。
   *
   * ⚠️ 这里**先清空再加载**。不清的话，加载的那几百毫秒里，
   * 顶上写着新会话的名字、下面显示着上一个会话的内容 —— 看起来
   * 像是切换没生效，用户会再点一次。
   */
  async function openSession(id: string) {
    if (id === sessionId) return

    invalidateStream()
    const token = ++loadSeqRef.current

    setSessionId(id)
    setSessionIdInUrl(id)
    setState(emptyChat())
    setOpenArtifactId(null)
    // 报告是**这个会话的**产物，切走就得一起收掉 ——
    // 留着的话，新会话的界面上会挂着一份别的会话的报告。
    setProgress(null)
    setReportOpen(false)
    setBootError(null)
    setBootPhase('connecting')

    try {
      const detail = await getSession(id)
      // ① 被更晚的一次点击顶掉了 —— 这份响应已经过期，丢掉。
      if (loadSeqRef.current !== token) return
      setState(fromSessionDetail(detail))
      setBootPhase('ready')
    } catch (err) {
      if (loadSeqRef.current !== token) return
      console.error('打开会话失败：', err)
      setBootError(describeFailure(err))
      setBootPhase('failed')
    }
  }

  /** 建一个全新的会话并打开它。 */
  async function createAndOpen() {
    invalidateStream()
    const token = ++loadSeqRef.current

    setSessionIdInUrl(null)
    setState(emptyChat())
    setSessionId(null)
    setOpenArtifactId(null)
    setProgress(null)
    setReportOpen(false)
    setBootError(null)
    setBootPhase('connecting')

    try {
      const session = await createSession()
      if (loadSeqRef.current !== token) return
      setSessionIdInUrl(session.id)
      setSessionId(session.id)
      setBootPhase('ready')
      await refreshSessions()
    } catch (err) {
      if (loadSeqRef.current !== token) return
      console.error('初始化会话失败：', err)
      setBootError(describeFailure(err))
      setBootPhase('failed')
    }
  }

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
    const existing = readSessionIdFromUrl()
    if (existing) await openSession(existing)
    else await createAndOpen()
  }

  /**
   * 删掉一个会话。
   *
   * ⚠️ 后端的级联会**连同它的产物文件一起删掉**（ADR-011），
   *    所以界面上的两步确认不是走过场。
   *
   * 删的是当前会话时，接替它的选择是「打开剩下最近的一个」，
   * 而不是「再建一个新的」—— 后者会在用户每次清理时都留下一个空会话。
   */
  async function removeSession(id: string) {
    try {
      await deleteSession(id)
    } catch (err) {
      console.error('删除会话失败：', err)
      return
    }

    if (id === sessionId) {
      const remaining = sessions.filter((s) => s.id !== id)
      if (remaining.length > 0) await openSession(remaining[0].id)
      else await createAndOpen()
    }
    await refreshSessions()
  }

  // ⚠️ 这个 effect 刻意放在 `boot` 的**定义之后**。
  //
  // 函数声明会提升，所以写在前面运行时也能跑 —— 但那样会有一个隐患：
  // 如果 `boot` 哪天变成了一个会变化的绑定（比如 useCallback 的返回值），
  // 那个提前的引用就会固化成旧的那份。
  // 把 effect 挪到定义之后，这个隐患就不存在了，而且可读性也更好 ——
  // 「启动时做什么」紧挨着「启动函数长什么样」。
  // ⚠️ 下面这行**刻意**关掉 `exhaustive-deps`，理由不是「懒得改」：
  //
  // `boot` 和 `refreshSessions` 每次渲染都是新的函数，所以
  //   · 把 `[]` 换成 `[boot, refreshSessions]` → effect 每次渲染都重跑
  //   · 给它们包 `useCallback` → 为了稳定还得再包它们的依赖，一级级传下去
  //
  // 而这两条路换来的都不是「只跑一次」—— 真正保证那件事的是 **`bootedRef`**。
  // 空依赖数组本身保证不了：StrictMode 下它会被跑两遍，而那正是当初
  // 加这个 ref 的原因。所以这里让 ref 当唯一的守卫，并说明白。
  //
  // ⚠️ 那行 disable 写在**依赖数组前面**而不是 `useEffect(` 前面，是因为
  // 这条规则报的位置是依赖数组那一行 —— 写成 `next-line` 放在 `useEffect(`
  // 上面会够不着它（实测过，eslint 会回一句「这条 disable 没用上」）。
  useEffect(() => {
    if (bootedRef.current) return
    bootedRef.current = true
    void boot()
    void refreshSessions()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 见上
  }, [])

  // 离开页面时掐掉还在跑的流。
  //
  // 之前不需要这个（这个页面就是应用本身，没什么可离开的）。现在有了
  // 「返回自检页」那个链接，用户可以在流跑到一半时点走 —— 不掐的话，
  // 那条 fetch 会继续往一个已经卸载的组件里推事件，
  // 而服务端也会继续把它跑完（白花 token）。
  useEffect(() => () => abortRef.current?.abort(), [])

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
    const token = streamSeqRef.current

    let accepted = false
    try {
      for await (const event of makeIter(controller.signal)) {
        // ② 用户切走了（或者点了新会话）—— 这条流已经不属于当前界面了。
        if (streamSeqRef.current !== token) return

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
      // ② 同上：这条流已经不属于当前界面了，它的失败也不该算在
      //    别人头上。最坏的情况是用户在会话 A 里发消息、立刻切到 B，
      //    而 A 的那条流**恰好在这时因为别的原因**报错 ——
      //    不拦的话 B 的界面上会冒出一句「生成中断了」，
      //    而 B 上什么都没发生过。
      if (streamSeqRef.current !== token) return

      // 用户自己点了「停止」、或者切走了 —— AbortError 应当静默忽略，
      // 否则用户会因为一次正常的切换看到一个「操作被中止」的报错。
      if (err instanceof DOMException && err.name === 'AbortError') {
        setState(abortStream)
      } else {
        console.error('请求失败：', err)
        setState((prev) => failWith(prev, describeFailure(err)))
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      if (streamSeqRef.current === token) {
        // 兜底：正常路径下 liveKey 已经由 finish / abort / error 三个分支清掉了。
        // 这里防的是「流结束了但一个 finish 都没发」—— 那是契约被破坏，
        // 真发生了就该修后端，但不该让界面永远卡在「正在思考」的转圈上。
        setState((prev) => (prev.liveKey ? { ...prev, liveKey: null } : prev))
        // 标题/状态是后端从事件日志推出来的，跑完一轮之后才会变
        // （标题取自第一条用户消息）。所以每轮结束刷一次侧栏。
        void refreshSessions()
      }
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
    invalidateStream()
    setState(abortStream)
  }

  /** 在右侧面板里打开一个产物（从工具卡片点过来时，顺带把面板展开）。 */
  function openArtifact(artifactId: string) {
    setOpenArtifactId(artifactId)
    setPanelOpen(true)
  }

  // ────────────────────────────────────────── 报告

  /**
   * 生成一份报告，**边生成边显示**。
   *
   * 一上来就把 overlay 打开、显示空壳 —— 而不是等全部写完再弹。
   * 报告是四节、每节一次模型调用，全程几十秒；让用户盯着一个按钮转圈，
   * 和看着它一节一节长出来，是完全不同的两种体验。
   */
  async function generateReport() {
    if (!sessionId || reportGenerating) return

    const controller = new AbortController()
    reportAbortRef.current = controller
    const token = ++reportSeqRef.current

    setProgress(startProgress())
    setReportOpen(true)

    try {
      for await (const frame of streamReport(sessionId, controller.signal)) {
        // 切走了 —— 这条流已经不属于当前界面了。
        if (reportSeqRef.current !== token) return

        setProgress((prev) => applyReportFrame(prev ?? emptyProgress(), frame))

        // 落盘之后把新报告插进产物清单。**必须在这里做**：
        // 面板读的是 `state.reports`，不插的话报告要刷新一次才出现 ——
        // 而用户刚刚才看着它写完。
        if (frame.name === 'report_done') {
          setState((prev) => applyReport(prev, frame.data.report))
        }
      }
    } catch (err) {
      if (reportSeqRef.current !== token) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      console.error('生成报告失败：', err)
      setProgress((prev) => ({
        ...(prev ?? emptyProgress()),
        running: false,
        error: describeFailure(err),
      }))
    } finally {
      if (reportAbortRef.current === controller) reportAbortRef.current = null
    }
  }

  /**
   * 打开一份**已经生成过的**报告（刷新之后点产物清单里的那一条）。
   *
   * 读的是 `.report.json` 而不是 `.md`：应用内视图需要知道
   * 「哪一块是模型写的、哪一块是程序投影的」，而 Markdown 把两者压成了
   * 同一种文本，那个区别就没了。要 Markdown 的话有「下载」。
   *
   * ⚠️ 这里**没有**重新跑模型的可能 —— 报告是已经落盘的产物，
   * 打开它不该花一分钱 token。
   */
  async function openReport(report: LogReportEvent) {
    if (!sessionId) return
    const token = ++reportSeqRef.current

    setProgress(startProgress())
    setReportOpen(true)

    try {
      const response = await fetch(artifactUrl(sessionId, report.sidecar.id))
      if (!response.ok) {
        throw new Error(
          response.status === 404
            ? '这份报告的文件已经不在磁盘上了。'
            : `取报告失败（HTTP ${response.status}）。`,
        )
      }
      const document = (await response.json()) as ReportDocument
      if (reportSeqRef.current !== token) return
      setProgress(progressFromDocument(document, report))
    } catch (err) {
      if (reportSeqRef.current !== token) return
      console.error('打开报告失败：', err)
      setProgress((prev) => ({
        ...(prev ?? emptyProgress()),
        running: false,
        error: describeFailure(err),
      }))
    }
  }

  const reportGenerating = progress?.running ?? false

  // ────────────────────────────────────────── 渲染

  const statusLine = awaiting
    ? '在等你拍板'
    : streaming
      ? '正在生成'
      : '人在环中'

  return (
    <main className="flex h-screen bg-plane">
      {/* ════════ 左：会话清单 ════════ */}
      {sidebarOpen && (
        <aside className="flex w-64 shrink-0 flex-col border-r border-hairline bg-surface">
          <SessionSidebar
            sessions={sessions}
            currentId={sessionId}
            loading={sessionsLoading}
            onSelect={(id) => void openSession(id)}
            onCreate={() => void createAndOpen()}
            onDelete={(id) => void removeSession(id)}
          />
        </aside>
      )}

      {/* ════════ 中：对话 ════════ */}
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center gap-3 border-b border-hairline bg-surface px-5 py-3">
          <button
            type="button"
            onClick={() => setSidebarOpen((open) => !open)}
            title={sidebarOpen ? '收起会话列表' : '展开会话列表'}
            className="shrink-0 rounded-control border border-hairline px-2 py-1 text-caption text-ink-muted transition-colors hover:text-ink"
          >
            {sidebarOpen ? '◀' : '▶'}
          </button>

          <div className="min-w-0">
            <p className="truncate text-body font-medium text-ink">
              {currentSession?.title ?? '新会话'}
            </p>
            <p className="text-caption text-ink-muted">{statusLine}</p>
          </div>

          <button
            type="button"
            onClick={() => setPanelOpen((open) => !open)}
            title={panelOpen ? '收起产物面板' : '展开产物面板'}
            className="ml-auto shrink-0 rounded-control border border-hairline px-2 py-1 text-caption text-ink-muted transition-colors hover:text-ink"
          >
            {panelOpen ? '收起产物 ▶' : '◀ 产物'}
            {artifacts.length > 0 && (
              <span className="ml-1.5 font-mono">{artifacts.length}</span>
            )}
          </button>
        </header>

        <Conversation
          state={state}
          sessionId={sessionId}
          boot={{
            phase: bootPhase,
            error: bootError,
            onRetry: () => void createAndOpen(),
          }}
          streaming={streaming}
          awaiting={awaiting}
          submittingCallId={submittingCallId}
          onSend={(text) => void send(text)}
          onDecide={(callId, choice, note) => void decide(callId, choice, note)}
          onRetry={() => void retry()}
          onStop={stop}
          onOpenArtifact={openArtifact}
        />
      </section>

      {/* ════════ 右：产物 ════════
          ⚠️ 面板折叠时**整个卸载**，而不是用 `hidden` 藏起来。
          图表是 ECharts 画的，而它在 0×0 的容器上 `init` **不报任何错** ——
          只是画布是空的。重新展开时如果没有触发 resize，用户面对的
          就是一块白板，而且刷新之后又好了，很难查。卸载掉最省心。 */}
      {panelOpen && sessionId && (
        <aside className="flex w-[26rem] shrink-0 flex-col border-l border-hairline bg-surface">
          {/* `key` 让切会话时面板重建：选中状态、展开的数据表、
              加载到一半的请求都不该跨会话带过去。 */}
          <ArtifactPanel
            key={sessionId}
            sessionId={sessionId}
            artifacts={artifacts}
            reports={state.reports}
            selectedId={openArtifactId}
            onSelect={setOpenArtifactId}
            onGenerate={() => void generateReport()}
            generating={reportGenerating}
            onOpenReport={(report) => void openReport(report)}
          />
        </aside>
      )}

      {/* ════════ 报告 ════════
          整页 overlay 而不是第三栏里的一块：一份数模论文塞进 400px
          等于从门缝里读报纸。状态归这个组件，没有多开一个路由 ——
          新路由要重做一遍数据加载、会话切换和错误处理。 */}
      {reportOpen && progress && sessionId && (
        <ReportOverlay
          progress={progress}
          sessionId={sessionId}
          onClose={() => setReportOpen(false)}
        />
      )}
    </main>
  )
}
