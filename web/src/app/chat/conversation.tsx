'use client'

/**
 * 中间那一列：消息流 + 输入区。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么把它从 `page.tsx` 抽出来
 * ════════════════════════════════════════════════════════════════════
 * M5b 给页面加了两栏和一个三栏外壳之后，`page.tsx` 要同时管三件事：
 * 会话生命周期（建 / 切 / 删）、事件流的接入、以及**三个栏的渲染**。
 * 那是 M4 把组件从 `page.tsx` 拆到 `parts.tsx` 时的同一条理由。
 *
 * 拆的位置也是同一条线：**这一列是纯展示 + 滚动**，
 * 所有「什么时候发请求、发什么」都留在 `page.tsx`。
 * 所以这个文件里的 props 全是回调，没有一个 fetch。
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
 */

import { useEffect, useRef, useState } from 'react'

import type { ChatState } from '@/lib/chat-state'

import { MessageBubble } from './parts'

/** 空状态下的引导语。挑的都是**会把决策点跑出来**的题 —— 便于验证 HITL。 */
const SUGGESTIONS = [
  '帮我选一个评价类模型，数据挺全的',
  '这道预测题该用 ARIMA 还是 LSTM？',
  '帮我算一下 1 到 100 的平方和',
]

/** 最后一条消息已经有可见内容了吗 —— 决定要不要显示「正在思考」的转圈。 */
function hasVisibleContent(state: ChatState): boolean {
  const last = state.messages[state.messages.length - 1]
  if (!last) return false
  return last.content !== '' || last.tools.length > 0
}

export function Conversation({
  state,
  sessionId,
  boot,
  streaming,
  awaiting,
  submittingCallId,
  onSend,
  onDecide,
  onRetry,
  onStop,
  onOpenArtifact,
}: {
  state: ChatState
  sessionId: string | null
  boot: {
    phase: 'connecting' | 'ready' | 'failed'
    error: string | null
    onRetry: () => void
  }
  streaming: boolean
  awaiting: boolean
  submittingCallId: string | null
  onSend: (text: string) => void
  onDecide: (callId: string, choice: string, note: string) => void
  onRetry: () => void
  onStop: () => void
  onOpenArtifact: (artifactId: string) => void
}) {
  const [draft, setDraft] = useState('')

  // 滚动容器。用 ref 直接操作 DOM，而不是把滚动位置也做成 state ——
  // 滚动位置每帧都在变，做成 state 会让整个消息列表疯狂重渲染。
  const scrollRef = useRef<HTMLDivElement>(null)

  // 新内容到达时自动滚到底部。依赖 state.messages 整体 ——
  // 每来一个文字碎片它都是新数组，于是每次都会滚。这正是我们想要的。
  useEffect(() => {
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
  }, [state.messages])

  function submit() {
    const text = draft.trim()
    if (!text) return
    setDraft('')
    onSend(text)
  }

  return (
    <>
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-8">
        <div className="mx-auto flex max-w-3xl flex-col gap-6">
          {/* ── 状态一：正在确定会话 ── */}
          {boot.phase === 'connecting' && (
            <div className="mt-16 flex items-center justify-center gap-3 text-body text-ink-muted">
              <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-grid border-t-brand-500" />
              正在准备工作区……
            </div>
          )}

          {/* ── 状态二：会话没建起来 ── */}
          {boot.phase === 'failed' && (
            <div className="mt-16 rounded-card border border-hairline bg-surface p-6">
              <p className="flex items-center gap-3 text-body font-medium text-critical-ink">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-critical" />
                {boot.error}
              </p>
              <p className="mt-3 text-caption text-ink-muted">
                工作区没能建起来，所以现在发不了消息。
              </p>
              <button
                type="button"
                onClick={boot.onRetry}
                className="mt-5 rounded-control bg-brand-500 px-4 py-2.5 text-body font-medium text-white transition-colors hover:bg-brand-600"
              >
                重新建立工作区
              </button>
            </div>
          )}

          {/* ── 状态三：空会话 ── */}
          {boot.phase === 'ready' && state.messages.length === 0 && (
            <div className="mt-16 text-center">
              <p className="text-lead text-ink-secondary">
                蒟蒻agent在线，有什么数模问题尽管问。
              </p>
              <p className="mt-3 text-body text-ink-muted">
                它拿不准的地方会停下来问你，不会硬编。
              </p>
              <div className="mt-10 flex flex-wrap justify-center gap-3">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => onSend(suggestion)}
                    className="rounded-control border border-hairline bg-surface px-4 py-2.5 text-body text-ink-secondary transition-colors hover:border-brand-300 hover:bg-brand-50 hover:text-ink"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* `sessionId &&` 这个守卫是必要的，不只是为了类型：消息气泡里的
              产物要从 `/api/artifacts/{会话}/…` 取，没有会话 id 就拼不出 URL。
              启动过程中（还没建好会话）本来也没有消息可显示。 */}
          {sessionId &&
            state.messages.map((message) => (
              <MessageBubble
                key={message.key}
                msg={message}
                streaming={streaming}
                submittingCallId={submittingCallId}
                onDecide={onDecide}
                onOpenArtifact={onOpenArtifact}
                sessionId={sessionId}
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
                  onClick={onRetry}
                  className="mt-4 rounded-control border border-hairline bg-raised px-4 py-2 text-body text-ink-secondary transition-colors hover:text-ink"
                >
                  重试这一轮
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      <footer className="shrink-0 border-t border-hairline bg-surface px-6 py-5">
        <form
          className="mx-auto flex max-w-3xl items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            submit()
          }}
        >
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // Enter 发送，Shift+Enter 换行 —— 聊天框的通用约定。
              // 注意要判断 isComposing：中文输入法选词时按 Enter 是「确认候选词」，
              // 不是「发送」。不判断的话，用拼音打字时选词会把没打完的句子发出去。
              if (
                event.key === 'Enter' &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault()
                submit()
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
              onClick={onStop}
              className="h-[3.25rem] shrink-0 rounded-control border border-hairline bg-raised px-6 text-body font-medium text-ink-secondary transition-colors hover:text-ink"
            >
              停止
            </button>
          ) : (
            <button
              type="submit"
              disabled={!draft.trim() || awaiting || boot.phase !== 'ready'}
              className="h-[3.25rem] shrink-0 rounded-control bg-brand-500 px-6 text-body font-medium text-white transition-colors hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-40"
            >
              发送
            </button>
          )}
        </form>
      </footer>
    </>
  )
}
