/**
 * `.chart.json` → ECharts 配置。**纯函数，不碰 React 也不碰 DOM。**
 *
 * ════════════════════════════════════════════════════════════════════
 * 这份数据是谁给的
 * ════════════════════════════════════════════════════════════════════
 * 沙箱里的 `modelforge_plot.save(fig, name, chart={...})` 在存图的同时
 * 写一份 `<名字>.chart.json`，里面是**模型算出来的真实数组**
 * （见 ADR-012）。工作台据此渲染可交互版本 —— 同一个数组，两个视图。
 *
 * 所以这个文件的输入是**模型生成的内容**，不是内部类型。契约只保证
 * 形状（`_build_chart_spec` 校验过 `series` / `data` 非空），
 * 不保证取值合理：`kind` 可以是任何字符串，`categories` 的条数可以
 * 和 `data` 对不上，各系列可以长短不一。下面的每一条容错都对应
 * 一个真会发生的情况，不是防御性编程的洁癖。
 *
 * ════════════════════════════════════════════════════════════════════
 * 五条硬规则：图表永远不能只靠颜色区分系列
 * ════════════════════════════════════════════════════════════════════
 * 这不是审美偏好，是 ADR-006 里由配色校验**算出来**的两条产品约束：
 *
 *   · 亮色模式下有 3 个分类色对比度低于 3:1 → 必须带直接标签或表格视图
 *   · 暗色模式下最差相邻 CVD ΔE = 10.3（危险区）→ 必须启用次级编码
 *
 * 合起来就是上面那句话。落地成五条，写在这个纯函数里
 * （而不是指望每个调用方记得）：
 *
 *   ① 图例在 ≥2 个系列时常在；单个系列不放（标题即说明）
 *   ② 直接标签克制使用：折线在**末点**标系列名，柱状图只在
 *      「柱数 × 系列数 ≤ 12」时才标数值
 *   ③ **表格视图是必需的**（见 `panel.tsx`）——
 *      悬停提示框绝不能是读到数值的唯一途径
 *   ④ 单轴，永不双轴；网格是横向发丝**实线**（虚线会读成「阈值/预测」）
 *   ⑤ 系列数 > 8 时颜色必然重复 —— 照旧循环（和静态图一致），
 *      但要在界面上**说出来**
 */

import type { ChartTokens } from './chart-tokens'
import type { EChartsOption } from './echarts'

// ══════════════════════════════════════════════════════ 契约

/**
 * 工作台**渲染得出来**的图型。
 *
 * ⚠️ 这份清单要和沙箱侧的 `modelforge_plot.CHART_KINDS` 一致 ——
 * `tests/test_chart_contract.py` 盯着这件事。加一种图型时两边一起加。
 */
export const KNOWN_CHART_KINDS = ['bar', 'line', 'scatter', 'pie'] as const

export type ChartKind = (typeof KNOWN_CHART_KINDS)[number]

export function isKnownKind(kind: string): kind is ChartKind {
  return (KNOWN_CHART_KINDS as readonly string[]).includes(kind)
}

export type ChartSeries = {
  name: string
  data: number[]
  /**
   * 静态图用的颜色（**亮色模式**那一套，`mp.save` 按序号取的）。
   *
   * ⚠️ 这里的颜色是给**纸**的：论文印在白纸上，所以它是对的。
   * 屏幕上不直接用它 —— 交互图从 `--chart-cat-*` 实时取色，
   * 因为暗色模式是**重新选步号**而不是把亮色反转。
   * 这一份只在令牌读不到时兜底（见 `seriesColors()`）。
   */
  color: string
}

export type ChartSpec = {
  version: number
  /** ⚠️ 是 `string` 不是 `ChartKind`：生产者**不校验**它。 */
  kind: string
  title: string
  x_label: string
  y_label: string
  series: ChartSeries[]
  /** 可选。条数可能比数据短 —— 模型经常数错。 */
  categories?: string[]
  caption?: string
}

/**
 * 把一个来路不明的 JSON 值**收窄**成 `ChartSpec`；收不了就返回 `null`。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么 `response.json()` 成功还不够
 * ════════════════════════════════════════════════════════════════════
 * 「能解析成 JSON」和「符合这份契约」是两件事。这个文件的内容是
 * **模型的代码**写出来的，它可以是：一个数组、一个旧格式的对象、
 * 一份被人手改过的数据、或者只是缺了 `series` 的半个对象。
 * 直接当 `ChartSpec` 用的话，`spec.series.length` 那一行就会抛，
 * 而错误会冒泡成「面板崩了」——用户完全不知道发生了什么。
 *
 * 这一步同时也是**唯一**需要容忍 `NaN` 的地方：`JSON.parse` 本身
 * 产不出 NaN，但 `1e999` 会解析成 `Infinity`。所以数值要过一遍
 * `Number.isFinite`，而不是只查 `typeof === 'number'`。
 *
 * ⚠️ 它**不修数据，只判断**。缺 `categories` 就当作没有；
 * 但缺 `series` 或者 `series` 全空 —— 那不是「可以补救的残缺」，
 * 是一份读不懂的文件，返回 `null` 让调用方说人话。
 */
export function parseChartSpec(raw: unknown): ChartSpec | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const record = raw as Record<string, unknown>

  if (typeof record.kind !== 'string' || record.kind === '') return null
  if (!Array.isArray(record.series)) return null

  const series: ChartSeries[] = []
  for (const item of record.series) {
    if (!item || typeof item !== 'object') return null
    const entry = item as Record<string, unknown>
    if (!Array.isArray(entry.data)) return null

    // ⚠️ 遇到非有限值是**整份拒绝**，不是把它过滤掉。
    //
    // 过滤看起来更「宽容」，但它会**静默地改变数据的含义**：少了中间一个点，
    // 后面所有的点都往前挪一位，横轴标签和它们就再也对不上了 ——
    // 而图上完全看不出来，只会有人奇怪「第 7 个指标怎么是这个数」。
    //
    // 拒绝的代价是明显的（图表不出现，且说了原因），过滤的代价是隐蔽的。
    // 这个项目一路上都在选前者。
    if (!entry.data.every(isFiniteNumber)) return null
    const data = entry.data
    if (data.length === 0) return null

    series.push({
      name:
        typeof entry.name === 'string' && entry.name !== ''
          ? entry.name
          : `系列${series.length + 1}`,
      data,
      color: typeof entry.color === 'string' ? entry.color : '',
    })
  }
  if (series.length === 0) return null

  const categories = Array.isArray(record.categories)
    ? record.categories.filter((c): c is string => typeof c === 'string')
    : undefined

  return {
    version: typeof record.version === 'number' ? record.version : 1,
    kind: record.kind,
    title: typeof record.title === 'string' ? record.title : '',
    x_label: typeof record.x_label === 'string' ? record.x_label : '',
    y_label: typeof record.y_label === 'string' ? record.y_label : '',
    series,
    categories,
    caption: typeof record.caption === 'string' ? record.caption : undefined,
  }
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** 工作台这边需要的、图表以外的信息（表格视图、说明文字用）。 */
export type ChartTable = {
  /** 列名 —— 每个系列一列。 */
  columns: string[]
  /** 行标签，和 `rows` 一一对应。 */
  rowLabels: string[]
  /** 每行一个系列的值，缺失的位置是 `null`（**不补零**）。 */
  rows: (number | null)[][]
}

/**
 * 渲染结果。
 *
 * 用可辨识联合而不是「返回 null 表示失败」：失败**必须带上原因**，
 * 否则界面上只能显示「出错了」，而用户既不知道哪儿错了、
 * 也不知道该让模型改什么。
 */
export type ChartRender =
  | { ok: true; option: EChartsOption; table: ChartTable; notes: string[] }
  | { ok: false; reason: string; table: ChartTable; notes: string[] }

// ══════════════════════════════════════════════════════ 次级编码

/**
 * 折线的**线型 × 标记**表 —— 和沙箱里 `modelforge_plot.GRAYSCALE_STYLES`
 * 是同一张表（4 种线型 × 8 种标记，按系列序号取）。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么交互图也需要它（屏幕上有颜色啊）
 * ════════════════════════════════════════════════════════════════════
 * 三条理由，一条比一条硬：
 *
 *   ① **和静态图讲同一个故事。** 论文里那张是黑白打印的，靠线型分系列；
 *      屏幕上这张如果只有颜色，用户切换两个视图时看到的是两套不同的
 *      辨认方式，得重新认一遍。
 *   ② **暗色模式最差相邻 CVD ΔE = 10.3**（ADR-006 算出来的），
 *      落在危险区 —— 设计系统的要求就是「必须启用次级编码」。
 *      线型是最自然的那一种。
 *   ③ 系列多到超过 8 个时颜色必然重复，那时线型是**唯一**还能分辨的东西。
 *
 * 不依赖 `aria.decal`（ECharts 的纹理）是因为它默认对柱/饼加满纹理，
 * 而那违反了另一条同样明确的规则：**纹理是选配，不是装饰**——
 * 密集的斜线场在数据图上读起来就是噪声。
 */
const LINE_STYLES: { dash: 'solid' | 'dashed' | 'dotted' | number[]; symbol: string }[] = [
  { dash: 'solid', symbol: 'circle' },
  { dash: 'dashed', symbol: 'rect' },
  // ECharts 没有「点划线」这个预置值，用自定义虚线数组表达同一个意思
  // （matplotlib 那边的 '-.' 就是它）。
  { dash: [6, 2, 1, 2], symbol: 'triangle' },
  { dash: 'dotted', symbol: 'diamond' },
  { dash: 'solid', symbol: 'roundRect' },
  { dash: 'dashed', symbol: 'pin' },
  { dash: [6, 2, 1, 2], symbol: 'arrow' },
  { dash: 'dotted', symbol: 'path://M0.5,0 L0.61,0.35 L1,0.35 L0.68,0.57 L0.79,0.92 L0.5,0.7 L0.21,0.92 L0.32,0.57 L0,0.35 L0.39,0.35 Z' },
]

// ══════════════════════════════════════════════════════ 取色

/**
 * 第 i 个系列的颜色：**先设计令牌，再退回静态图那份**。
 *
 * 两个来源都有存在的理由：
 *   · 令牌 —— 屏幕上的图要跟着亮/暗模式走，而暗色是重新选步号
 *   · 静态图那份 —— 令牌读不到时（服务端、或者任何读失败）的兜底
 *
 * 超过 8 个系列时按 8 取模循环，**和 `matplotlib` 那边的 `% len(COLORS)`
 * 是同一个行为** —— 于是交互图和静态图即便在重复的情况下也仍然对得上。
 */
function seriesColors(tokens: ChartTokens, spec: ChartSpec): string[] {
  return spec.series.map((series, index) => {
    const palette = tokens.categorical
    const fromToken = palette.length > 0 ? palette[index % palette.length] : ''
    return fromToken || series.color
  })
}

/**
 * 第 i 个系列的线型 / 标记。
 *
 * 按 8 取模，和颜色同一个周期 —— 于是「颜色重复」和「线型重复」
 * 总是同时发生，用户的辨认负担不会在某个系列号上突然变差。
 */
function lineStyleAt(index: number): (typeof LINE_STYLES)[number] {
  return LINE_STYLES[index % LINE_STYLES.length]
}

// ══════════════════════════════════════════════════════ 表格

/**
 * 把 spec 摊平成一张表 —— **表格视图的数据源，和图表是同一份数组**。
 *
 * 长短不一的系列**不补零**：补零是在编造数据，
 * 而这张表的全部意义就是「数值是可信的」。短的那一列在对应行留空。
 */
export function buildChartTable(spec: ChartSpec): ChartTable {
  const seriesCount = spec.series.length
  const columns = spec.series.map((s) => s.name)
  const maxLength = maxSeriesLength(spec)

  // 行标签：只有在类别名够用的时候才用它，否则退回序号。
  const usableCategories =
    spec.categories && spec.categories.length >= maxLength ? spec.categories : null

  const rowLabels = Array.from({ length: maxLength }, (_, i) =>
    usableCategories ? usableCategories[i] : String(i + 1),
  )

  const rows = Array.from({ length: maxLength }, (_, row) =>
    Array.from({ length: seriesCount }, (_, col) => {
      const value = spec.series[col].data[row]
      return typeof value === 'number' ? value : null
    }),
  )

  return { columns, rowLabels, rows }
}

function maxSeriesLength(spec: ChartSpec): number {
  return spec.series.reduce((longest, s) => Math.max(longest, s.data.length), 0)
}

/**
 * 类别名能不能用。
 *
 * ⚠️ 单独拎出来是因为**对不上时不能硬用**：模型给了 5 个类别、每列 8 个
 * 数是常事，而 ECharts 遇到这种会**静默地按位置贴标签** ——
 * 于是第 6~8 根柱子挂着空标签，图看起来「就是这样」，没人会发现它是错的。
 */
function usableCategories(spec: ChartSpec): string[] | null {
  const maxLength = maxSeriesLength(spec)
  if (!spec.categories || spec.categories.length < maxLength) return null
  return spec.categories.slice(0, maxLength)
}

// ══════════════════════════════════════════════════════ 主函数

export function buildChartOption(
  spec: ChartSpec,
  tokens: ChartTokens,
): ChartRender {
  const table = buildChartTable(spec)
  const notes = collectNotes(spec)

  if (spec.series.length === 0) {
    // 契约上不可能（`_build_chart_spec` 会拦住空 series），
    // 但真拿到了也不该让面板白屏。
    return { ok: false, reason: '这份数据里没有任何系列。', table, notes }
  }

  if (!isKnownKind(spec.kind)) {
    // 未知图型**不报错**，只是画不出来 —— 数据还在，表格照给。
    // 沙箱那边对同一件事也会打一句警告（见 `_build_chart_spec`）。
    return {
      ok: false,
      reason: `工作台还不认识「${spec.kind}」这种图，只能把数据列出来。`,
      table,
      notes,
    }
  }

  const colors = seriesColors(tokens, spec)
  const common = baseOption(spec, tokens, table, colors)

  switch (spec.kind) {
    case 'bar':
      return { ok: true, option: cartesian(spec, tokens, colors, common, 'bar'), table, notes }
    case 'line':
      return { ok: true, option: cartesian(spec, tokens, colors, common, 'line'), table, notes }
    case 'scatter':
      return { ok: true, option: cartesian(spec, tokens, colors, common, 'scatter'), table, notes }
    case 'pie':
      return { ok: true, option: pie(spec, tokens, colors, common), table, notes }
  }
}

// ══════════════════════════════════════════════════════ 说明文字

/**
 * 把所有「图上看得出来、但看不出来是问题」的情况收集成给用户看的句子。
 *
 * 这是项目里到处在用的 **no-silent-caps** 习惯：宁可说一句，
 * 也不要静默地少给东西 —— 静默的那种，用户会以为是自己的数据就这样。
 */
function collectNotes(spec: ChartSpec): string[] {
  const notes: string[] = []
  const maxLength = maxSeriesLength(spec)

  if (spec.series.length > 8) {
    notes.push(
      `这张图有 ${spec.series.length} 个系列，超过了 8 色的分类色板，` +
        '颜色从第 9 个开始重复。同一张图里请靠表格或直接标签区分。',
    )
  }

  if (spec.categories && spec.categories.length < maxLength) {
    notes.push(
      `类别名有 ${spec.categories.length} 个，但数据有 ${maxLength} 个点，` +
        '对不上，横轴改用了序号。',
    )
  }

  const lengths = new Set(spec.series.map((s) => s.data.length))
  if (lengths.size > 1) {
    notes.push(
      `各系列的数据长度不一致（${[...lengths].sort((a, b) => a - b).join(' / ')} 个点），` +
        '短的那些按实际长度画，没有补零。',
    )
  }

  if (spec.kind === 'pie' && spec.series.length > 1) {
    notes.push(
      `饼图只能画一个系列，这里用了第一个（${spec.series[0].name}），` +
        `另外 ${spec.series.length - 1} 个只在表格里。`,
    )
  }

  // 饼图对负数和零没有定义。ECharts 遇到它们不会报错 ——
  // 只是画出一张缺了一块的图（或者干脆空白），而用户会以为数据就长这样。
  if (spec.kind === 'pie') {
    const first = spec.series[0]
    const bad = first.data.filter((value) => value <= 0).length
    const total = first.data.reduce((sum, value) => sum + value, 0)
    if (bad > 0) {
      notes.push(
        `饼图里出现了 ${bad} 个小于等于 0 的数值。占比图对负数和零没有定义，` +
          '这几片画不出来，看到的形状是不完整的。',
      )
    } else if (total === 0) {
      notes.push('这些数值加起来是 0，画不出占比 —— 图是空的。')
    }
  }

  if (spec.version !== 1) {
    notes.push(`这份数据的格式版本是 ${spec.version}，工作台按版本 1 解读的。`)
  }

  return notes
}

// ══════════════════════════════════════════════════════ 骨架

/**
 * 四种图共用的那部分：标题、无障碍、提示框、图例的取舍。
 *
 * 颜色一律用 `|| undefined`：令牌可能是空串（服务端求值），
 * 而把空串交给 ECharts 会得到一个非法的 `font-family: ;`，
 * 退给 `undefined` 才是「这一项不设置、用它自己的默认」。 */
function baseOption(
  spec: ChartSpec,
  tokens: ChartTokens,
  table: ChartTable,
  colors: string[],
): EChartsOption {
  const seriesCount = spec.series.length
  const isPie = spec.kind === 'pie'

  // ③ 表格视图是必需的 —— 但无障碍描述可以顺手把这件事说出来，
  // 让屏幕阅读器用户知道还有一个能读全数值的地方。
  const ariaDescription =
    `${spec.title || '图表'}，${seriesCount} 个系列，` +
    `共 ${table.rowLabels.length} 个数据点。完整数值见下方数据表。`

  return {
    // 主题切换时图会重画，动画放短一点，避免「换主题之后图慢慢变过去」
    // 那种拖泥带水的感觉。
    animationDuration: 240,
    textStyle: {
      fontFamily: tokens.fontFamily || undefined,
      color: tokens.inkSecondary || undefined,
    },
    aria: { enabled: true, description: ariaDescription },
    // 标签防重叠：宁可少显示一个，也不要两个标签叠在一起谁也读不出来。
    // ⚠️ 这条设置要靠 `LabelLayout` 那个特性才生效（见 `lib/echarts.ts`）——
    //    没注册的话它是个**静默的空操作**。
    labelLayout: { hideOverlap: true },
    title: spec.title
      ? {
          text: spec.title,
          subtext: spec.caption || undefined,
          left: 'center',
          top: 2,
          textStyle: {
            color: tokens.ink || undefined,
            fontSize: 13,
            fontWeight: 'normal',
          },
          subtextStyle: { color: tokens.inkMuted || undefined, fontSize: 11 },
        }
      : undefined,

    // ① 图例：≥2 个系列时常在。单个系列不放 —— 标题已经说明它是什么了。
    //
    // 饼图是个例外：它的每一片本来就有「名字 + 百分比」的直接标签，
    // 再加图例是重复。只有切片多到标签会打架时（>6）才补一个图例，
    // 那时它是「标签被挤掉了」的退路，不是装饰。
    legend:
      (isPie ? table.columns.length > 6 : seriesCount >= 2)
        ? {
            type: 'scroll',
            bottom: 0,
            textStyle: { color: tokens.inkSecondary || undefined, fontSize: 11 },
            // 系列多的时候图例换行会顶掉画布，滚动式图例只占一行。
            pageTextStyle: { color: tokens.inkMuted || undefined },
          }
        : undefined,

    tooltip: isPie
      ? {
          trigger: 'item',
          backgroundColor: tokens.surface || undefined,
          borderColor: tokens.grid || undefined,
          textStyle: { color: tokens.ink || undefined, fontSize: 12 },
        }
      : {
          // 折线/柱状用「轴触发」贴着一整列走 —— 数模的图经常要横向对比
          // 同一类别下各方案的取值，逐个点悬停去凑很费劲。
          trigger: 'axis',
          axisPointer: {
            // 柱状用 shadow（整列高亮），折线/散点用 line（十字准星）
            type: spec.kind === 'bar' ? 'shadow' : 'line',
          },
          backgroundColor: tokens.surface || undefined,
          borderColor: tokens.grid || undefined,
          textStyle: { color: tokens.ink || undefined, fontSize: 12 },
        },

    // ECharts 的全局调色板，也是**一道兜底**。
    //
    // 上面每一处颜色我都显式给了，但「显式给了」这件事靠的是人别漏。
    // 把设计系统这套色同时设成全局调色板，漏掉的地方就会落到我们的
    // 颜色上，而不是落到 **ECharts 6 的新默认主题**上 ——
    // 后者看起来「差不多对」，只是和界面不再是同一套色，
    // 而那种偏差没人会来报 bug。
    //
    // ⚠️ 空字符串要滤掉：`seriesColors` 在令牌和 `series[].color`
    //    都拿不到时会给空串，而把空串放进调色板，ECharts 会画成透明或黑，
    //    那比「用错一套色」还难看出原因。滤掉之后至少会退回它的默认色 ——
    //    而这是**唯一**会用到 ECharts 默认色的路径，所以留一句话在这里。
    color: colors.filter((value) => value !== ''),
  }
}

// ══════════════════════════════════════════════════════ 直角坐标系

function cartesian(
  spec: ChartSpec,
  tokens: ChartTokens,
  colors: string[],
  base: EChartsOption,
  kind: 'bar' | 'line' | 'scatter',
): EChartsOption {
  const labels = usableCategories(spec) ?? undefined
  const maxLength = maxSeriesLength(spec)
  const seriesCount = spec.series.length
  const showLegend = seriesCount >= 2

  // ② 直接标签克制使用。柱状图每根柱子标数值只在图不大的时候做 ——
  //    「每个数据点都标数字」是公认的乱，标了也没人读。
  const showBarValues = kind === 'bar' && maxLength * seriesCount <= 12

  // ② 折线在末点标系列名，让身份不依赖图例和颜色。
  const showEndLabel = kind === 'line' && seriesCount <= 4

  const series = spec.series.map((s, index) => {
    const color = colors[index]
    if (kind === 'bar') {
      return {
        type: 'bar' as const,
        name: s.name,
        data: s.data,
        // 数据端 4px 圆角，锚在基线上（上方两角）
        itemStyle: { color, borderRadius: [4, 4, 0, 0] },
        // 相邻柱之间留出底色缝隙，别让两段填充糊成一块。
        // （ECharts 没有「2px」这种绝对单位，用百分比表达同一个意图。）
        barGap: '20%',
        label: showBarValues
          ? {
              show: true,
              position: 'top' as const,
              color: tokens.inkSecondary || undefined,
              fontSize: 10,
            }
          : { show: false },
        emphasis: { focus: 'series' as const },
      }
    }
    if (kind === 'line') {
      const style = lineStyleAt(index)
      return {
        type: 'line' as const,
        name: s.name,
        data: s.data,
        // 线型和标记按**系列序号**取，让这条线在黑白打印、色觉障碍、
        // 以及系列数超过 8 色板时都还认得出来。见 `LINE_STYLES`。
        symbol: style.symbol,
        symbolSize: 6,
        lineStyle: { width: 2, color, type: style.dash },
        // 标记填充跟线同色，但描一圈底色 —— 叠在一起时能分开。
        itemStyle: { color, borderColor: tokens.surface || undefined, borderWidth: 1 },
        // 点太密时每个点都画标记会糊成一条带子，那时只留线
        // （线型仍然在，辨认能力没丢）。
        showSymbol: maxLength <= 40,
        endLabel: showEndLabel
          ? {
              show: true,
              // 用函数而不是 `'{a}'` 模板：系列名是模型起的中文，
              // 里面万一带了花括号，模板会把它当占位符解析掉。
              formatter: () => s.name,
              color,
              fontSize: 11,
            }
          : { show: false },
        emphasis: { focus: 'series' as const },
      }
    }
    return {
      type: 'scatter' as const,
      name: s.name,
      data: s.data,
      symbolSize: 8,
      // 重叠的点靠一圈底色描边分开 —— 这是「间隙」在散点上的等价物。
      itemStyle: {
        color,
        borderColor: tokens.surface || undefined,
        borderWidth: 2,
      },
      emphasis: { focus: 'series' as const },
    }
  })

  // 轴**内联**在返回的对象字面量里，不先建两个中间变量。
  // 原因是类型：`EChartsOption` 里 xAxis/yAxis 的类型要靠上下文推导，
  // 而先存进一个变量就得手写那份类型 —— 那份类型没有从 `echarts/core`
  // 导出，只能自己编一个近似的，于是校验就失效了。
  return {
    ...base,
    grid: {
      left: 8,
      right: 24,
      // 给标题留位置；标题和副标题占两行时还要再多一点
      top: spec.title ? (spec.caption ? 66 : 44) : 20,
      bottom: showLegend ? 52 : 12,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data:
        labels ??
        Array.from({ length: maxLength }, (_, index) => String(index + 1)),
      name: spec.x_label || undefined,
      nameLocation: 'middle',
      nameGap: 30,
      nameTextStyle: { color: tokens.inkMuted || undefined, fontSize: 11 },
      axisLine: { lineStyle: { color: tokens.axis || undefined } },
      axisTick: { show: false },
      axisLabel: { color: tokens.inkMuted || undefined, fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      name: spec.y_label || undefined,
      nameTextStyle: { color: tokens.inkMuted || undefined, fontSize: 11 },
      // ④ 只留横向网格、发丝、**实线**。虚线会被读成「阈值 / 预测」，
      //    而这里只是网格（ECharts 默认就是实线，写出来是为了别被改坏）。
      splitLine: {
        lineStyle: { color: tokens.grid || undefined, type: 'solid', width: 1 },
      },
      // 纵轴不画基线：横向网格已经足够定位，多一条线只是噪声。
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: tokens.inkMuted || undefined, fontSize: 11 },
    },
    series,
  }
}

// ══════════════════════════════════════════════════════ 饼图

function pie(
  spec: ChartSpec,
  tokens: ChartTokens,
  colors: string[],
  base: EChartsOption,
): EChartsOption {
  const labels = usableCategories(spec)
  const first = spec.series[0]
  const palette = tokens.categorical

  const data = first.data.map((value, index) => {
    const fromToken = palette.length > 0 ? palette[index % palette.length] : ''
    return {
      // 没有类别名时退回序号 —— 和表格视图用的是同一套标签，
      // 于是「图上第 3 片」和「表里第 3 行」指的是同一个东西。
      name: labels ? labels[index] : String(index + 1),
      value,
      itemStyle: { color: fromToken || colors[0] },
    }
  })

  const slices = data.length
  const showLegend = slices > 6

  return {
    ...base,
    // 饼图不用直角坐标系，grid / xAxis / yAxis 一概不给 ——
    // 给了 ECharts 也不报错，只是白白多画一层看不见的东西。
    grid: undefined,
    series: [
      {
        type: 'pie',
        radius: ['0%', '62%'],
        center: ['50%', showLegend ? '46%' : '52%'],
        data,
        // 每一片都直接标名字和百分比 —— 饼图不靠颜色也能读。
        label: {
          show: true,
          formatter: '{b}  {d}%',
          color: tokens.inkSecondary || undefined,
          fontSize: 11,
        },
        labelLine: { lineStyle: { color: tokens.axis || undefined } },
        // 切片之间留出底色缝隙，相邻色块不至于糊在一起。
        itemStyle: {
          borderColor: tokens.surface || undefined,
          borderWidth: 2,
        },
        emphasis: { focus: 'self' as const },
      },
    ],
  }
}
