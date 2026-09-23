'use client'

/**
 * 对话界面的展示组件。
 *
 * 单独一个文件，而不是像 M3 那样全塞在 `page.tsx` 里 —— 因为 M4 之后
 * 那个文件要同时管三件事（会话生命周期、事件流、渲染），
 * 再往里堆组件会变成一个没人愿意打开的文件。
 * 拆出来的这一半是**纯展示**：给 props 就画，不碰任何网络或状态。
 *
 * 两条规矩（沿用自 M0 的自检页）：
 *   ① 零硬编码色值 —— 全部走 globals.css 的设计令牌
 *   ② 状态色永远配「圆点/图标 + 文字」，不靠颜色单独传达信息
 *      （设计系统定的硬性约束，见 globals.css 的配色校验结论）
 */

import { useState } from 'react'

import {
  extractAskUser,
  extractCode,
  type DecisionView,
  type Msg,
  type ToolCallView,
} from '@/lib/chat-state'

// ══════════════════════════════════════════════════════ 消息气泡

/**
 * 一条消息气泡。
 *
 * 拆成独立组件不是「为了看起来结构清晰」，而是有实际收益：
 * 用户消息的 props 在流式过程中**从不变化**，而助手消息每来一个字就变一次。
 * 拆开之后 React 更容易跳过用户消息的重新渲染。
 */
export function MessageBubble({
  msg,
  streaming,
  submittingCallId,
  onDecide,
}: {
  msg: Msg
  streaming: boolean
  /** 正在提交中的决策 call_id —— 用来禁用按钮，防止连点。 */
  submittingCallId: string | null
  onDecide: (callId: string, choice: string, note: string) => void
}) {
  const isUser = msg.role === 'user'
  // 「正在进行」= 这条是正在被写入的那条助手消息，且已经有内容了。
  const isLive = !isUser && streaming && msg.content !== ''

  return (
    <div className={isUser ? 'flex justify-end' : ''}>
      {!isUser && (
        <p className="mb-2 flex items-center gap-2 text-caption font-medium text-ink-muted">
          <span className="h-2 w-2 rounded-full bg-brand-500" />
          蒟蒻
        </p>
      )}

      {/* 助手消息可能一个字都没说就在调工具，这时不渲染空的气泡 */}
      {msg.content !== '' && (
        <div
          className={
            isUser
              ? 'max-w-[85%] rounded-card rounded-br-sm bg-brand-500 px-5 py-3.5 text-body whitespace-pre-wrap text-white'
              : 'text-body whitespace-pre-wrap text-ink'
          }
        >
          {msg.content}
          {/* 光标用 inline-block 而不是绝对定位 —— 它会自然跟在最后一个字后面，
              不需要知道文字的宽度。 */}
          {isLive && (
            <span className="ml-0.5 inline-block h-[1.1em] w-[2px] animate-pulse bg-brand-500 align-middle" />
          )}
        </div>
      )}

      {msg.tools.length > 0 && (
        <div className="mt-4 flex flex-col gap-3">
          {msg.tools.map((tool) => (
            <ToolView
              key={tool.key}
              tool={tool}
              submitting={submittingCallId === tool.decision?.callId}
              onDecide={onDecide}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * 一个工具调用该画成什么。
 *
 * 这一行 `if` 就是「ask_user 是一个工具」这条后端建模在界面上的体现：
 * 它和 run_python 走同一套增删改逻辑，只在**最后一步渲染**时分叉。
 */
function ToolView({
  tool,
  submitting,
  onDecide,
}: {
  tool: ToolCallView
  submitting: boolean
  onDecide: (callId: string, choice: string, note: string) => void
}) {
  if (tool.decision || tool.name === 'ask_user') {
    return <DecisionCard tool={tool} submitting={submitting} onDecide={onDecide} />
  }
  return <ToolCallCard tool={tool} />
}

// ══════════════════════════════════════════════════════ 决策卡片

/**
 * 决策卡片 —— M4 最重要的界面元素，也是整个产品的定位所在。
 *
 * ════════════════════════════════════════════════════════════════════
 * 已答的卡片不能消失
 * ════════════════════════════════════════════════════════════════════
 * 用户拍板之后卡片会变成「已选择：X」的只读状态，而**不是被移除**。
 *
 * 这不是偷懒。刷新页面之后，用户要能看到「我刚才选的是什么」——
 * 而模型确实还记得那个选择（它变成了历史里的一条 tool 消息）。
 * 如果界面上那块凭空消失，用户会以为自己点的那一下没生效，
 * 或者忘了自己选过什么，然后开始怀疑后面的结论。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么选项按钮要占满一整行
 * ════════════════════════════════════════════════════════════════════
 * 因为选项的文案是「方案名（代价）」的形式，通常有一二十个字
 * （后端工具描述里明确要求模型这么写）。横排按钮会把它们挤成
 * 两行甚至溢出，而竖排每行一个读起来最舒服，也最容易点。
 */
function DecisionCard({
  tool,
  submitting,
  onDecide,
}: {
  tool: ToolCallView
  submitting: boolean
  onDecide: (callId: string, choice: string, note: string) => void
}) {
  const [draft, setDraft] = useState('')

  // 优先用后端给的 decision_request（结构化、经过校验）。
  // 极少数情况下事件还没到，这时从参数碎片里抠一个出来先显示着 ——
  // 总比让用户盯着一张「正在执行…」的代码卡片好。
  const fallback = tool.decision ? null : extractAskUser(tool.args)
  const decision: DecisionView | null =
    tool.decision ??
    (fallback
      ? {
          callId: tool.id ?? '',
          question: fallback.question,
          options: fallback.options,
          allowFreeText: true,
          choice: null,
          note: '',
        }
      : null)

  if (!decision) {
    return (
      <div className="rounded-card border border-hairline bg-surface px-4 py-3 text-caption text-ink-muted">
        蒟蒻准备问你一个问题……
      </div>
    )
  }

  const answered = decision.choice !== null

  return (
    <div
      className={`overflow-hidden rounded-card border bg-surface ${
        answered ? 'border-hairline' : 'border-brand-300'
      }`}
    >
      {/* ── 状态头 ── */}
      <div className="flex items-center gap-3 px-4 py-2.5 text-caption">
        <span
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${
            answered ? 'bg-good' : 'bg-brand-500'
          }`}
        />
        <span className={answered ? 'text-good-ink' : 'text-brand-600'}>
          {answered ? '已拍板' : '等你拍板'}
        </span>
      </div>

      {/* ── 问题 ── */}
      <p className="border-t border-hairline px-4 py-3 text-body font-medium text-ink">
        {decision.question}
      </p>

      {answered ? (
        // ── 已答：显示选择结果，只读 ──
        <div className="border-t border-hairline bg-plane px-4 py-3">
          <p className="text-body text-ink">{decision.choice}</p>
          {decision.note && (
            <p className="mt-2 text-caption text-ink-muted">补充：{decision.note}</p>
          )}
        </div>
      ) : (
        <>
          {/* ── 待答：选项按钮 ── */}
          <div className="flex flex-col gap-2 border-t border-hairline p-3">
            {decision.options.map((option) => (
              <button
                key={option}
                type="button"
                disabled={submitting}
                onClick={() => onDecide(decision.callId, option, '')}
                className="rounded-control border border-hairline bg-raised px-4 py-3 text-left text-body text-ink-secondary transition-colors hover:border-brand-400 hover:bg-brand-50 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
              >
                {option}
              </button>
            ))}
            {submitting && (
              <p className="px-1 pt-1 text-caption text-ink-muted">正在提交……</p>
            )}
          </div>

          {/* ── 自由作答 ── */}
          {decision.allowFreeText && (
            <form
              className="flex items-end gap-2 border-t border-hairline bg-plane p-3"
              onSubmit={(e) => {
                e.preventDefault()
                const text = draft.trim()
                if (!text || submitting) return
                onDecide(decision.callId, text, '')
              }}
            >
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={1}
                placeholder="或者直接告诉我你的想法……"
                className="flex-1 resize-none rounded-control border border-hairline bg-surface px-3 py-2.5 text-body text-ink placeholder:text-ink-muted focus:border-brand-400 focus:outline-none"
              />
              <button
                type="submit"
                disabled={!draft.trim() || submitting}
                className="shrink-0 rounded-control bg-brand-500 px-4 py-2.5 text-body font-medium text-white transition-colors hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-40"
              >
                回答
              </button>
            </form>
          )}
        </>
      )}
    </div>
  )
}

// ══════════════════════════════════════════════════════ 工具卡片

/**
 * 一张工具执行卡片：状态头 + 代码 + 输出。
 *
 * M3 最重要的界面元素 —— 它把「Agent 在后台干了什么」摊开给用户看。
 * 没有它，用户只能看到模型说「我算出来是 5050」，无从判断这个数是怎么来的。
 */
function ToolCallCard({ tool }: { tool: ToolCallView }) {
  // 折叠状态交给 React 管（受控），而不是让 <details> 自己管（非受控）。
  // 非受控的话会有一个很隐蔽的坑：组件每次重渲染，React 都会把 open 属性
  // 按 vdom 里的值重新写一遍，把用户手动折叠的状态冲掉。
  const [open, setOpen] = useState(true)

  const result = tool.result
  const code = extractCode(tool.args)

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
        <code className="font-medium text-ink">{tool.name || '…'}</code>
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
