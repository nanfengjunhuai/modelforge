'use client'

/**
 * 工作台左栏：会话清单。
 *
 * ════════════════════════════════════════════════════════════════════
 * 这一栏是 M4 留下的一个「接口写好了但没有界面」
 * ════════════════════════════════════════════════════════════════════
 * `listSessions()` / `deleteSession()` 在 M4 就写好了，注释里写着
 * 「M4 还不做界面，但接口先留着（M5 的会话列表要用）」。
 * 在那之前，会话是**建得出来、看不见也删不掉**的 —— 每测试一次
 * 就多一个永远留在库里的会话，而界面上只有一个「新会话」按钮。
 *
 * ════════════════════════════════════════════════════════════════════
 * 状态由 `page.tsx` 持有，这里只画
 * ════════════════════════════════════════════════════════════════════
 * 列表本身是这一栏的，但「哪个是当前的」和「点了要干什么」属于页面 ——
 * 因为切会话要动的是对话那一列的状态。所以这里接的是数据 + 回调，
 * 和 `parts.tsx` 一样是**纯展示**。
 */

import Link from 'next/link'
import { useState } from 'react'

import type { Session } from '@/lib/log-types'

export function SessionSidebar({
  sessions,
  currentId,
  loading,
  onSelect,
  onCreate,
  onDelete,
}: {
  sessions: Session[]
  currentId: string | null
  /** 首次拉列表时为 true —— 用来区分「加载中」和「一个会话都没有」。 */
  loading: boolean
  onSelect: (sessionId: string) => void
  onCreate: () => void
  onDelete: (sessionId: string) => void
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-hairline px-4 py-3">
        <h1 className="text-caption font-bold tracking-tight text-ink">模型工坊</h1>
        <button
          type="button"
          onClick={onCreate}
          title="新建会话"
          className="ml-auto rounded-control border border-hairline bg-raised px-2.5 py-1 text-caption text-ink-secondary transition-colors hover:border-brand-300 hover:text-ink"
        >
          ＋ 新会话
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading && sessions.length === 0 && (
          <p className="px-4 py-6 text-caption text-ink-muted">正在读取会话……</p>
        )}

        {!loading && sessions.length === 0 && (
          <p className="px-4 py-6 text-caption text-ink-muted">
            还没有会话。点上面的「＋ 新会话」开始。
          </p>
        )}

        <ul className="flex flex-col py-1">
          {sessions.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              current={session.id === currentId}
              onSelect={onSelect}
              onDelete={onDelete}
            />
          ))}
        </ul>
      </div>

      <div className="shrink-0 border-t border-hairline px-4 py-3">
        <Link
          href="/"
          className="text-caption text-ink-muted transition-colors hover:text-ink-secondary"
        >
          返回自检页
        </Link>
      </div>
    </div>
  )
}

function SessionRow({
  session,
  current,
  onSelect,
  onDelete,
}: {
  session: Session
  current: boolean
  onSelect: (sessionId: string) => void
  onDelete: (sessionId: string) => void
}) {
  // 删除做成**两步**，而不是弹一个 `window.confirm`。
  //
  // 用浏览器的 confirm 也能拦住误触，但它是模态的、样式不可控、
  // 而且在这个「本地工具」的语境里显得很重。两步确认更轻：
  // 第一下把按钮变成「确认删除」，第二下才真的删，点别处就取消。
  //
  // ⚠️ 无论哪种，**删会话是会连带删掉产物文件**的（后端 delete 的级联），
  //    所以这个确认不是走过场。
  const [confirming, setConfirming] = useState(false)

  return (
    <li>
      <div
        className={`group flex items-center gap-2 px-3 py-2 transition-colors ${
          current ? 'bg-brand-50' : 'hover:bg-plane'
        }`}
      >
        <button
          type="button"
          onClick={() => onSelect(session.id)}
          title={session.title}
          className="flex min-w-0 flex-1 flex-col gap-0.5 text-left"
        >
          <span
            className={`truncate text-caption ${
              current ? 'font-medium text-brand-600' : 'text-ink-secondary'
            }`}
          >
            {session.title}
          </span>
          <span className="flex items-center gap-2 text-caption text-ink-muted">
            {/* 状态只说**需要注意的**那种。空闲是常态，画出来只是噪声。 */}
            {session.busy ? (
              <span className="flex items-center gap-1.5 text-brand-600">
                <span className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-2 border-grid border-t-brand-500" />
                生成中
              </span>
            ) : session.status === 'awaiting_user' ? (
              <span className="flex items-center gap-1.5 text-warning-ink">
                <span className="h-2 w-2 shrink-0 rounded-full bg-warning" />
                等你拍板
              </span>
            ) : null}
            <span className="shrink-0">{relativeTime(session.updated_at)}</span>
          </span>
        </button>

        {confirming ? (
          <span className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => {
                setConfirming(false)
                onDelete(session.id)
              }}
              className="rounded-control bg-critical px-2 py-1 text-caption text-white"
            >
              删除
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-control px-2 py-1 text-caption text-ink-muted hover:text-ink"
            >
              取消
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            title="删除这个会话（连同它的产物）"
            // 平时藏起来，鼠标移上去或键盘聚焦时才出现 ——
            // 一排删除按钮会让整个列表看起来很危险。
            className="shrink-0 rounded-control px-1.5 py-1 text-caption text-ink-muted opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100 hover:text-critical-ink"
          >
            删除
          </button>
        )}
      </div>
    </li>
  )
}

/**
 * 时间戳 → 「3 分钟前」这种。
 *
 * 用**相对时间**而不是绝对时间，是因为这一栏的用途是「找到我刚才那个会话」——
 * 「14:32」需要用户自己心算现在几点，「3 分钟前」直接回答了那个问题。
 * 精确时间放在 `title` 里，鼠标停上去能看到。
 *
 * ⚠️ 这里读的是「现在」，天然不纯。之所以没问题，是因为**这一栏永远
 * 不在服务端渲染** —— 列表初始是空的，要等 `listSessions()` 回来才有内容。
 * 哪天把它改成从 props 直接渲染初始数据，就要小心水合不匹配了。
 */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''

  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}
