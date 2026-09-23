'use client'

/**
 * 工作台右栏：**本会话的产物清单**，以及点开之后的查看器。
 *
 * ════════════════════════════════════════════════════════════════════
 * 它和工具卡片里的缩略图是什么关系
 * ════════════════════════════════════════════════════════════════════
 * 不是同一份信息的两个副本，而是**两个层次**：
 *
 *   工具卡片里那份 —— 局部上下文。「这段代码生成了这张图」，
 *                     它长在产出它的那一次执行旁边。
 *   右栏这份       —— 全局清单。这个会话一路产出了什么。
 *
 * 一份图多到十几张的时候，前者要靠翻聊天记录才能找回来；
 * 后者是一眼看到的。所以两边都留着。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么面板里**没有**再放一份静态 PNG 的缩略图
 * ════════════════════════════════════════════════════════════════════
 * 因为那一份在工具卡片里已经有了，而这里的位置更该留给
 * **交互图** —— 它是同一个数组的另一个视图，是新增的信息量。
 * 想看原始位图就点「看原图」，跳到接口拿 300 dpi 那份。
 *
 * ════════════════════════════════════════════════════════════════════
 * 表格视图不是「多给一个选项」
 * ════════════════════════════════════════════════════════════════════
 * 设计系统的两条硬约束（亮色下 3 个分类色对比度 <3:1；暗色下最差相邻
 * CVD ΔE = 10.3）合起来是「**图表永远不能只靠颜色区分系列**」，
 * 而其中一条明确的落地方式就是「必须带直接标签**或表格视图**」。
 *
 * 另外一条同样硬的理由：**悬停提示框不能是读到数值的唯一途径**。
 * 提示框在触摸屏上没有、被截图的论文里没有、打印出来也没有。
 * 所以表格是一个必须存在的对等视图，不是锦上添花。
 */

import dynamic from 'next/dynamic'
import { useEffect, useMemo, useState } from 'react'

import {
  extensionOf,
  formatBytes,
  groupArtifacts,
  isChartData,
  KIND_LABEL,
  type ArtifactGroup,
} from '@/lib/artifact-format'
import {
  buildChartOption,
  buildChartTable,
  parseChartSpec,
  type ChartSpec,
} from '@/lib/chart-spec'
import type { ChartTokens } from '@/lib/chart-tokens'
import type { LogReportEvent } from '@/lib/log-types'
import { artifactUrl } from '@/lib/sessions'
import type { ArtifactRef } from '@/lib/stream-types'

import { useChartTokens } from './use-chart-tokens'

// ⚠️ ECharts 是**懒加载**的（约 100 KB）。它只在这一栏真的显示一张图时
// 才下载 —— 打开会话、看对话、不点图表的人一个字节都不付。
//
// `ssr: false` 是必须的：ECharts 要 `window` 和 canvas，服务端跑不了。
// Next 16 要求这个选项只能出现在**客户端组件**里（写在服务端组件里
// 会直接报错），而这个文件是 `'use client'` 的，所以位置是对的。
const InteractiveChart = dynamic(() => import('./chart'), {
  ssr: false,
  loading: () => (
    <div className="flex h-[340px] items-center justify-center text-caption text-ink-muted">
      正在准备图表……
    </div>
  ),
})

// ══════════════════════════════════════════════════════ 面板

export function ArtifactPanel({
  sessionId,
  artifacts,
  reports,
  selectedId,
  onSelect,
  onGenerate,
  generating,
  onOpenReport,
}: {
  sessionId: string
  artifacts: ArtifactRef[]
  /** 这个会话生成过的报告 —— 用来认出「这一组其实是一份报告」。 */
  reports: LogReportEvent[]
  /** 当前选中的产物 id；null = 还没选。由 `page.tsx` 持有。 */
  selectedId: string | null
  onSelect: (artifactId: string | null) => void
  onGenerate: () => void
  generating: boolean
  onOpenReport: (report: LogReportEvent) => void
}) {
  const selected = artifacts.find((a) => a.id === selectedId) ?? null

  // ⚠️ 分组结果要 memo：不 memo 的话每次渲染都是一个新数组，
  //    而下面是 `groups.map(...)` —— 数组身份变了 React 就得全部重渲染。
  //    流式生成时每秒几十次渲染，这个差别是真实存在的。
  const groups = useMemo(() => groupArtifacts(artifacts), [artifacts])

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-3 border-b border-hairline px-4 py-3">
        <h2 className="text-caption font-medium text-ink-secondary">产物</h2>
        <span className="font-mono text-caption text-ink-muted">
          {groups.length}
        </span>
        <button
          type="button"
          onClick={onGenerate}
          disabled={generating}
          title="把这次会话整理成一篇论文草稿"
          className="ml-auto shrink-0 rounded-control border border-hairline bg-surface px-2.5 py-1 text-caption text-ink-secondary transition-colors hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
        >
          {generating ? '正在写……' : '生成报告'}
        </button>
        {selected && (
          <button
            type="button"
            onClick={() => onSelect(null)}
            className="shrink-0 rounded-control px-2 py-1 text-caption text-ink-muted transition-colors hover:text-ink"
          >
            收起
          </button>
        )}
      </div>

      {/* 查看器。`key` 让切换产物时整个重建 —— 否则上一个产物的
          加载状态、展开的表格会原样带过来，看起来像是新图还没加载完。 */}
      {selected && (
        <div
          key={selected.id}
          className="max-h-[62%] shrink-0 overflow-y-auto border-b border-hairline bg-plane px-4 py-4"
        >
          <ArtifactDetail sessionId={sessionId} artifact={selected} />
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {groups.length === 0 ? (
          <EmptyState />
        ) : (
          <ul className="flex flex-col">
            {groups.map((group) => (
              <ArtifactGroupRow
                key={group.key}
                sessionId={sessionId}
                group={group}
                selectedId={selectedId}
                reportOf={reports.find((r) => groupHoldsReport(group, r)) ?? null}
                onSelect={onSelect}
                onOpenReport={onOpenReport}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

/** 这一组里有没有某份报告的产物。 */
function groupHoldsReport(group: ArtifactGroup, report: LogReportEvent): boolean {
  return group.items.some(
    (item) => item.id === report.document.id || item.id === report.sidecar.id,
  )
}

function EmptyState() {
  return (
    <div className="px-4 py-8 text-center">
      <p className="text-caption text-ink-muted">这个会话还没有产物。</p>
      <p className="mt-2 text-caption text-ink-muted">
        让蒟蒻画张图试试 —— 图会连同它背后的数据一起出现在这里。
      </p>
    </div>
  )
}

/**
 * 产物清单里的**一行 = 一组同名文件**（M6a 起）。
 *
 * ════════════════════════════════════════════════════════════════
 * 为什么要分组
 * ════════════════════════════════════════════════════════════════
 * `mp.save()` 一次写下三份（`.png` / `.pdf` / `.chart.json`），
 * 报告又写两份（`.md` / `.report.json`）。不分组的清单看起来像
 * **文件浏览器**，而这一栏想说的是「这个会话产出了什么」。
 *
 * 分组键是 `nameKey()`，和后端 `outline.py::name_key` 逐字对应 ——
 * 两边不一致的话，图会配不到自己的 caption。
 *
 * ════════════════════════════════════════════════════════════════
 * 报告那一组为什么不一样
 * ════════════════════════════════════════════════════════════════
 * 别的产物点开是「看」，报告点开也是「看」—— 但看的是**整篇文档**，
 * 而不是某个文件。一份报告的意义在于被读，而 `.md` / `.report.json`
 * 两个文件都是它的载体，让用户去分辨要点哪个是本末倒置。
 * 所以报告那一组给的是「读报告」，下载退到次要位置。
 */
function ArtifactGroupRow({
  sessionId,
  group,
  selectedId,
  reportOf,
  onSelect,
  onOpenReport,
}: {
  sessionId: string
  group: ArtifactGroup
  selectedId: string | null
  reportOf: LogReportEvent | null
  onSelect: (artifactId: string | null) => void
  onOpenReport: (report: LogReportEvent) => void
}) {
  // 组的「主产物」：图组里是那张图，报告组里是那个 `.md`。
  // `groupArtifacts` 已经按 图 → 文档 → 数据 → 其他 排过序了。
  const primary = group.items[0]
  const selected = group.items.some((item) => item.id === selectedId)
  const total = group.items.reduce((sum, item) => sum + item.size, 0)

  return (
    <li className="border-b border-hairline last:border-0">
      <div
        className={`flex items-center gap-2 px-4 py-2.5 transition-colors ${
          selected ? 'bg-brand-50' : 'hover:bg-plane'
        }`}
      >
        <span className="shrink-0 rounded-control bg-raised px-1.5 py-0.5 text-caption text-ink-muted">
          {reportOf ? '报告' : KIND_LABEL[primary.kind]}
        </span>
        {reportOf ? (
          <button
            type="button"
            onClick={() => onOpenReport(reportOf)}
            title={group.label}
            className={`min-w-0 flex-1 truncate text-left text-caption transition-colors ${
              selected ? 'text-brand-600' : 'text-ink-secondary hover:text-ink'
            }`}
          >
            {group.label}
          </button>
        ) : (
          <button
            type="button"
            // 再点一次收回查看器 —— 和折叠面板是同一个手势习惯。
            onClick={() => onSelect(selected ? null : primary.id)}
            title={group.label}
            className={`min-w-0 flex-1 truncate text-left text-caption transition-colors ${
              selected ? 'text-brand-600' : 'text-ink-secondary hover:text-ink'
            }`}
          >
            {group.label}
          </button>
        )}
        <span className="shrink-0 font-mono text-caption text-ink-muted">
          {formatBytes(total)}
        </span>
        <a
          href={artifactUrl(sessionId, primary.id, { download: true })}
          className="shrink-0 text-caption text-brand-600 hover:underline"
        >
          下载
        </a>
      </div>

      {/* 组里不止一个文件时，把成员列出来 —— 否则用户会以为
          `.pdf` 和 `.chart.json` 不见了（它们只是被收进了这一行）。 */}
      {group.items.length > 1 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 pb-2.5">
          {group.items.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelect(item.id === selectedId ? null : item.id)}
              className={`font-mono text-caption transition-colors ${
                item.id === selectedId
                  ? 'text-brand-600'
                  : 'text-ink-muted hover:text-ink-secondary'
              }`}
            >
              {extensionOf(item.name).replace(/^\./, '') || '文件'}
            </button>
          ))}
        </div>
      )}
    </li>
  )
}

// ══════════════════════════════════════════════════════ 查看器

function ArtifactDetail({
  sessionId,
  artifact,
}: {
  sessionId: string
  artifact: ArtifactRef
}) {
  const inline = artifactUrl(sessionId, artifact.id)

  if (isChartData(artifact)) {
    return <ChartArtifactView sessionId={sessionId} artifact={artifact} />
  }

  if (artifact.kind === 'image') {
    return (
      <figure className="flex flex-col gap-2">
        {/* 原生 <img>：这是一张本机后端产出的 300 dpi 位图，
            已经是最优形态了。见 parts.tsx 里同一处更长的说明。 */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={inline}
          alt={artifact.name}
          className="w-full rounded-control border border-hairline bg-surface"
        />
        <figcaption className="flex items-center gap-2 text-caption">
          <a
            href={inline}
            target="_blank"
            rel="noreferrer"
            className="text-brand-600 hover:underline"
          >
            在新标签页看原图
          </a>
        </figcaption>
      </figure>
    )
  }

  return (
    <p className="text-caption text-ink-muted">
      这种文件工作台打不开预览，点右边的「下载」取走。
    </p>
  )
}

/**
 * `.chart.json` → 交互图 + 表格视图。
 *
 * ⚠️ 这里的加载**没有**「打开时才取」那一步（工具卡片里那版有），
 * 因为**选中本身就是那个动作** —— 组件只在被选中时才挂载。
 */
function ChartArtifactView({
  sessionId,
  artifact,
}: {
  sessionId: string
  artifact: ArtifactRef
}) {
  const url = artifactUrl(sessionId, artifact.id)
  const [state, setState] = useState<
    | { phase: 'loading' }
    | { phase: 'ready'; spec: ChartSpec }
    | { phase: 'failed'; reason: string }
  >({ phase: 'loading' })

  useEffect(() => {
    // ⚠️ 这个 cancelled 标志防的是**切换产物时的乱序响应**：
    // 用户先点了 A 再点 B，A 的响应后到就会把 B 的图覆盖掉 ——
    // 而界面上看不出任何异常，只是「显示的不是我点的那张」。
    let cancelled = false

    fetch(url)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(
            response.status === 404
              ? '这个文件已经不在磁盘上了。'
              : `取数据失败（HTTP ${response.status}）。`,
          )
        }
        return response.json() as Promise<unknown>
      })
      .then((raw) => {
        if (cancelled) return
        const spec = parseChartSpec(raw)
        if (!spec) {
          setState({
            phase: 'failed',
            reason: '这份数据的格式和工作台对不上，读不出图表。',
          })
          return
        }
        setState({ phase: 'ready', spec })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setState({
          phase: 'failed',
          reason:
            error instanceof Error ? error.message : '读不到这份数据。',
        })
      })

    return () => {
      cancelled = true
    }
  }, [url])

  if (state.phase === 'loading') {
    return <p className="text-caption text-ink-muted">正在读取……</p>
  }
  if (state.phase === 'failed') {
    return (
      <p className="flex items-start gap-2 text-caption text-serious-ink">
        <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-serious" />
        {state.reason}
      </p>
    )
  }
  return <ChartRender spec={state.spec} />
}

function ChartRender({ spec }: { spec: ChartSpec }) {
  const tokens = useChartTokens()
  const [showTable, setShowTable] = useState(false)

  // `tokens` 的身份是稳定的（见 `tokens.ts` 的模块级缓存），
  // 所以这个 memo 只在换数据或换主题时重算。
  const render = useMemo(() => buildChartOption(spec, tokens), [spec, tokens])

  return (
    <div className="flex flex-col gap-3">
      {spec.title && (
        <p className="text-caption font-medium text-ink">{spec.title}</p>
      )}

      {render.ok ? (
        <InteractiveChart option={render.option} />
      ) : (
        // 未知图型 / 空数据 —— 画不出来，但**数据还在**，表格照给。
        // 这里刻意不是一句「加载失败」，因为数据其实好好的。
        <p className="rounded-control border border-hairline bg-surface px-3 py-2.5 text-caption text-ink-secondary">
          {render.reason}
        </p>
      )}

      {/* 说明文字：把「图上看得出来、但看不出来是问题」的情况说出来。
          项目里到处在用的 no-silent-caps 习惯。 */}
      {render.notes.length > 0 && (
        <ul className="flex flex-col gap-1">
          {render.notes.map((note) => (
            <li
              key={note}
              className="flex items-start gap-2 text-caption text-warning-ink"
            >
              <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-warning" />
              {note}
            </li>
          ))}
        </ul>
      )}

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => setShowTable((previous) => !previous)}
          className="rounded-control border border-hairline bg-surface px-3 py-1.5 text-caption text-ink-secondary transition-colors hover:text-ink"
        >
          {showTable ? '看图' : '看数据表'}
        </button>
        {spec.caption && (
          <span className="min-w-0 flex-1 truncate text-caption text-ink-muted">
            {spec.caption}
          </span>
        )}
      </div>

      {showTable && <ChartTableView spec={spec} tokens={tokens} />}
    </div>
  )
}

/**
 * 表格视图 —— 图表之外的**对等**读数方式。
 *
 * 两件事让它不是走过场：
 *   · 数值用等宽数字（`tabular-nums`）对齐，能竖着扫一列
 *   · 短了的系列留空而**不补零** —— 补零是编造数据
 */
function ChartTableView({ spec, tokens }: { spec: ChartSpec; tokens: ChartTokens }) {
  // 表格的数据源和图表是同一份 —— 都由 `buildChartTable` 这一个纯函数
  // 摊平，所以「图上第 3 根柱子」和「表里第 3 行」不可能是两个数。
  //
  // ⚠️ 这里刻意**不**自己再写一遍摊平逻辑。两份实现迟早会漂移，
  //    而「图和表对不上」恰恰是这个产品最不该出的错 ——
  //    它砸的正是「图建立在数据之上」这条全部意义。
  const table = useMemo(() => buildChartTable(spec), [spec])

  return (
    <div className="max-h-72 overflow-auto rounded-control border border-hairline bg-surface">
      <table className="w-full border-collapse text-caption tabular-nums">
        <thead>
          <tr className="border-b border-hairline">
            <th className="sticky left-0 bg-surface px-3 py-2 text-left font-medium text-ink-muted">
              {spec.x_label || '项目'}
            </th>
            {table.columns.map((column, index) => (
              <th
                key={`${column}-${index}`}
                className="px-3 py-2 text-right font-medium whitespace-nowrap text-ink-secondary"
              >
                <span className="inline-flex items-center gap-1.5">
                  {/* 色点让表格和图上哪个系列对得上 —— 但**名字才是身份**，
                      颜色只是辅助（设计系统的硬约束）。 */}
                  <span
                    aria-hidden
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{
                      backgroundColor:
                        tokens.categorical[index % tokens.categorical.length] ||
                        spec.series[index].color,
                    }}
                  />
                  {column}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, rowIndex) => (
            <tr key={table.rowLabels[rowIndex]} className="border-b border-hairline last:border-0">
              <th className="sticky left-0 bg-surface px-3 py-1.5 text-left font-normal whitespace-nowrap text-ink-muted">
                {table.rowLabels[rowIndex]}
              </th>
              {row.map((value, columnIndex) => (
                <td
                  key={table.columns[columnIndex]}
                  className="px-3 py-1.5 text-right text-ink-secondary"
                >
                  {value === null ? (
                    <span className="text-ink-muted">—</span>
                  ) : (
                    formatNumber(value)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function formatNumber(value: number): string {
  // 整数原样，小数最多留 4 位再去掉尾零 —— 权重那种 0.2417 够用，
  // 而 0.30000000000000004 这种浮点尾巴不该给用户看。
  if (Number.isInteger(value)) return String(value)
  return String(Number(value.toFixed(4)))
}
