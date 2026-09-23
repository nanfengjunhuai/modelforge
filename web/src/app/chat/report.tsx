'use client'

/**
 * 报告的两种读法：面板里的**预览**，和整页的 **overlay**。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么一件东西要有两个尺寸
 * ════════════════════════════════════════════════════════════════════
 * 第三栏是 400px 宽。一份数模论文塞进去，等于让人从门缝里读报纸 ——
 * 每一行都要折三次，图只有指甲盖大。
 *
 * 但另开一个路由（`/chat/report/[id]`）要重做一遍数据加载、会话切换、
 * 错误处理，而这些东西在 `page.tsx` 里已经有一份了。
 *
 * overlay 是这两者之间唯一合理的落点：它只是 `page.tsx` 的一个状态，
 * 但铺满整屏。面板里留一个「展开」入口。
 *
 * ════════════════════════════════════════════════════════════════════
 * 界面上一件必须做到的事：**让人一眼看出哪句是谁写的**
 * ════════════════════════════════════════════════════════════════════
 * 这份报告的可信度全部建立在「图和数字不经过模型的手」这一条上
 * （ADR-015）。而这件事在代码里是结构性的、在界面上却可能完全看不出来 ——
 * 一段模型写的正文和一段程序投影的决策记录，如果长得一样，
 * 那这个架构对读者的价值就归零了。
 *
 * 所以两类块**样式必须明显不同**：
 *
 *     正文    普通段落，跟在正常文档流里
 *     过程记录 带左侧竖线的引用块 + 一行「过程记录 · 第 N 步」的小标
 *
 * 用户扫一眼就知道哪些是蒟蒻在说话、哪些是**当时真的发生过的事**。
 */

import type { ReactNode } from 'react'

import { artifactUrl } from '@/lib/sessions'
import type { ArtifactRef } from '@/lib/stream-types'
import type { ReportBlock } from '@/lib/report-types'
import type { ReportProgress, SectionDraft } from '@/lib/report-stream'

// ══════════════════════════════════════════════════════ 预览（第三栏用）

/**
 * 面板里的紧凑版：标题 + 每节的第一句。
 *
 * **不渲染整篇** —— 理由见文件头。这里要回答的是「报告写出来了没有、
 * 大概讲了什么」，读全文是 overlay 的事。
 */
export function ReportPreview({
  progress,
  onExpand,
}: {
  progress: ReportProgress
  onExpand: () => void
}) {
  const { title, sections, running, error, document } = progress

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <p className="min-w-0 flex-1 truncate text-caption font-medium text-ink">
          {title || '报告'}
        </p>
        {running && (
          <span className="shrink-0 text-caption text-brand-600">正在写……</span>
        )}
      </div>

      {error && (
        <p className="flex items-start gap-2 text-caption text-serious-ink">
          <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-serious" />
          {error}
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {sections.map((section, index) => (
          <li key={`${section.title}-${index}`} className="flex flex-col gap-0.5">
            <span className="text-caption font-medium text-ink-secondary">
              {section.title}
            </span>
            <span className="line-clamp-2 text-caption text-ink-muted">
              {section.error
                ? section.error
                : section.prose.trim() || (running ? '正在写……' : '（没有正文）')}
            </span>
          </li>
        ))}
      </ul>

      {document && (
        <button
          type="button"
          onClick={onExpand}
          className="self-start rounded-control border border-hairline bg-surface px-3 py-1.5 text-caption text-ink-secondary transition-colors hover:text-ink"
        >
          读全文
        </button>
      )}
    </div>
  )
}

// ══════════════════════════════════════════════════════ 整页阅读

export function ReportOverlay({
  progress,
  sessionId,
  onClose,
}: {
  progress: ReportProgress
  sessionId: string
  onClose: () => void
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-plane"
      role="dialog"
      aria-modal="true"
      aria-label="报告"
    >
      <header className="flex shrink-0 items-center gap-3 border-b border-hairline bg-surface px-6 py-3">
        <h2 className="min-w-0 flex-1 truncate text-body font-medium text-ink">
          {progress.title || '报告'}
        </h2>
        {progress.running && (
          <span className="shrink-0 text-caption text-brand-600">
            蒟蒻正在写……
          </span>
        )}
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded-control px-3 py-1.5 text-caption text-ink-secondary transition-colors hover:text-ink"
        >
          关闭
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto bg-plane">
        <article className="mx-auto flex max-w-3xl flex-col gap-8 px-6 py-8">
          {progress.error && (
            <p className="flex items-start gap-2 rounded-card border border-hairline bg-surface px-4 py-3 text-caption text-serious-ink">
              <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-serious" />
              {progress.error}
            </p>
          )}

          {/* 文档级的来源说明。**这份报告可不可信，读者有权知道是谁写的。**
              用一条左边线和正文分开：它是给读者的旁注，不是论文内容 ——
              混在正文里的话，用户复制到 Word 里会把它一起带走。 */}
          {progress.document?.notes.map((note) => (
            <p
              key={note}
              className="border-l-2 border-hairline pl-3 text-caption text-ink-muted"
            >
              {note}
            </p>
          ))}

          {progress.sections.map((section, index) => (
            <ReportSectionView
              key={`${section.title}-${index}`}
              section={section}
              sessionId={sessionId}
            />
          ))}

          {progress.document === null && !progress.running && !progress.error && (
            <p className="text-caption text-ink-muted">这份报告是空的。</p>
          )}
        </article>
      </div>
    </div>
  )
}

function ReportSectionView({
  section,
  sessionId,
}: {
  section: SectionDraft
  sessionId: string
}) {
  return (
    <section className="flex flex-col gap-4">
      <h3 className="text-title font-medium text-ink">{section.title}</h3>

      {section.error ? (
        <p className="flex items-start gap-2 text-caption text-serious-ink">
          <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-serious" />
          {section.error}
        </p>
      ) : (
        section.prose.trim() && <Prose text={section.prose} />
      )}

      {/* 核对结果紧跟在正文后面 —— 放到整篇末尾的话，
          用户读到「四、结论」时早就忘了第三节里那个数字。 */}
      {section.notes.map((note) => (
        <p
          key={note}
          className="border-l-2 border-warning pl-3 text-caption text-warning-ink"
        >
          {note}
        </p>
      ))}

      {section.blocks.map((block, index) => (
        <BlockView
          key={`${block.kind}-${index}`}
          block={block}
          sessionId={sessionId}
        />
      ))}
    </section>
  )
}

/** 正文。`whitespace-pre-wrap` 保住模型可能写的换行和缩进。 */
function Prose({ text }: { text: string }) {
  return (
    <p className="text-body leading-relaxed whitespace-pre-wrap text-ink-secondary">
      {text}
    </p>
  )
}

// ══════════════════════════════════════════════════════ 程序块
//
// 下面每一个都长成「带左竖线的引用块 + 一行小标」—— 那是「**这不是模型写的**」
// 的视觉签名。见文件头那段。

function BlockView({
  block,
  sessionId,
}: {
  block: ReportBlock
  sessionId: string
}) {
  switch (block.kind) {
    case 'decision':
      return (
        <RecordBox label={`过程记录 · 第 ${block.seq} 步 · 你拍板`}>
          <p className="text-caption text-ink-secondary">{block.question}</p>
          <p className="mt-1.5 text-caption text-ink-muted">
            候选：{block.options.join(' ／ ')}
          </p>
          <p className="mt-1.5 text-caption font-medium text-ink">
            选择：{block.choice}
          </p>
          {block.note && (
            <p className="mt-1.5 text-caption text-ink-muted">
              你的备注：{block.note}
            </p>
          )}
        </RecordBox>
      )

    case 'figure':
      return (
        <figure className="flex flex-col gap-2 rounded-card border border-hairline bg-surface p-3">
          {/* 原生 <img>：这是后端产出的 300 dpi 位图，已经是最优形态。
              见 parts.tsx 里同一处更长的说明。 */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={artifactUrl(sessionId, block.artifact_id)}
            alt={block.caption || block.name}
            className="w-full rounded-control bg-surface"
          />
          <figcaption className="text-caption text-ink-muted">
            {block.caption || block.name}
          </figcaption>
        </figure>
      )

    case 'code':
      return (
        <RecordBox label={`过程记录 · 第 ${block.seq} 步 · 代码${block.ok ? '' : '（没跑通）'}`}>
          <pre className="overflow-x-auto font-mono text-caption whitespace-pre text-ink-secondary">
            {block.code}
          </pre>
          {block.stdout && (
            <>
              <p className="mt-2 text-caption text-ink-muted">输出</p>
              <pre className="mt-1 overflow-x-auto font-mono text-caption whitespace-pre text-ink-muted">
                {block.stdout}
              </pre>
            </>
          )}
        </RecordBox>
      )

    // prose / note 在上面的 `ReportSectionView` 里就被分出去了，
    // 走不到这里。但类型上它们仍是合法成员，所以留一个明确的分支 ——
    // 比一个 `default: return null` 好：后者会在加了新块类型时静默吞掉它。
    case 'prose':
      return <Prose text={block.text} />
    case 'note':
      return (
        <p className="border-l-2 border-warning pl-3 text-caption text-warning-ink">
          {block.text}
        </p>
      )
  }
}

function RecordBox({
  label,
  children,
}: {
  label: string
  children: ReactNode
}) {
  return (
    <div className="rounded-control border-l-2 border-brand-300 bg-surface py-3 pl-3">
      <p className="mb-2 font-mono text-caption text-ink-muted">{label}</p>
      {children}
    </div>
  )
}

// ══════════════════════════════════════════════════════ 入口卡片

/**
 * 产物清单里那一条报告。
 *
 * 它和别的产物行不同的地方：点开是**读**，不是下载。
 * 一份报告的意义在于被读，而 `.md` 和 `.report.json` 两个文件
 * 都是它的载体，不该让用户去分辨要点哪个。
 */
export function ReportArtifactRow({
  artifact,
  sessionId,
  onOpen,
}: {
  artifact: ArtifactRef
  sessionId: string
  onOpen: () => void
}) {
  return (
    <div className="flex items-center gap-2 px-4 py-2.5 transition-colors hover:bg-plane">
      <span className="shrink-0 rounded-control bg-raised px-1.5 py-0.5 text-caption text-ink-muted">
        报告
      </span>
      <button
        type="button"
        onClick={onOpen}
        title={artifact.name}
        className="min-w-0 flex-1 truncate text-left text-caption text-ink-secondary transition-colors hover:text-ink"
      >
        {artifact.name}
      </button>
      <a
        href={artifactUrl(sessionId, artifact.id, { download: true })}
        className="shrink-0 text-caption text-brand-600 hover:underline"
      >
        下载
      </a>
    </div>
  )
}
