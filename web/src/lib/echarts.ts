/**
 * ECharts 的**按需构建** —— 只装载这个工作台真正会用的那几种图。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么不 `import * as echarts from 'echarts'`
 * ════════════════════════════════════════════════════════════════════
 * 完整包是 60 MB 的 npm 包，打出来近 900 KB。而我们要的只有
 * 柱 / 折线 / 散点 / 饼四种图加几个组件 —— 按需注册之后大约 100 KB。
 *
 * 这和项目里「手写 SSE 解析而不用 `EventSource`」是同一条取舍：
 * 不为了少写十几行，把一个用不完的东西整个拖进来。
 *
 * ════════════════════════════════════════════════════════════════════
 * ⚠️ ECharts 6 的两处「不注册就没有，而且不报错」
 * ════════════════════════════════════════════════════════════════════
 * 按需构建里**没有**任何东西是白送的。v6 尤其：
 *
 *   · **`GridComponent` 不再被折线/柱状图自动带上** —— 不注册它，
 *     直角坐标系画不出来，而报错信息很难指向「你少注册了一个组件」。
 *   · **`AriaComponent` 必须手动注册**，不注册的话 `aria: {enabled: true}`
 *     是个静默的空操作 —— 无障碍描述永远不会出现，也没有任何提示。
 *
 * 这两条都是「缺了不报错、只是不对」那一类，所以写在文件顶部而不是
 * 散在代码里。
 *
 * ════════════════════════════════════════════════════════════════════
 * ⚠️ ECharts 6 换了默认主题
 * ════════════════════════════════════════════════════════════════════
 * 配色重做了，图例默认位置也从顶部挪到了画布底部。我们每一处颜色
 * 都显式给（见 `chart-spec.ts`），所以不受它影响 —— 但**漏给一处**
 * 就会静默地用上 v6 的新默认色，而它看起来「差不多对」，
 * 只是和界面不再是同一套色彩体系（ADR-006 正是要防这个）。
 *
 * 还有一处 v6 的变化是**帮我们的**：它默认开启了标签防溢出
 * （`grid.outerBoundsMode`），轴标签不会再被画布边缘裁掉。
 */

import { BarChart, LineChart, PieChart, ScatterChart } from 'echarts/charts'
import type {
  BarSeriesOption,
  LineSeriesOption,
  PieSeriesOption,
  ScatterSeriesOption,
} from 'echarts/charts'
import {
  AriaComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import type {
  AriaComponentOption,
  GridComponentOption,
  LegendComponentOption,
  TitleComponentOption,
  TooltipComponentOption,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import type { ComposeOption } from 'echarts/core'
// ⚠️ `LabelLayout` 是 `echarts/features` 里的**独立特性**，不是组件。
// 不注册它，所有标签的排版规则都是**静默失效的空设置** ——
// `endLabel` 会顶着画布右边缘被裁掉、`hideOverlap` 不生效，
// 而 console 里一个字都不会说。
//
// 我们恰恰重度依赖标签：折线的末点标签、柱状图的数值标签，
// 都是「不靠颜色区分系列」那条硬约束的落地方式。所以它是必需品。
import { LabelLayout } from 'echarts/features'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LabelLayout,
  // 四种图型 —— 对应 `modelforge_plot.CHART_KINDS`，两边由
  // `tests/test_chart_contract.py` 盯着一致。
  BarChart,
  LineChart,
  ScatterChart,
  PieChart,
  // 组件。Grid 和 Aria 是 v6 里**必须显式注册**的那两个，别删。
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  AriaComponent,
  // 渲染器：按需构建里一个渲染器都不带，必须自己挑一个。
  // 选 Canvas 而不是 SVG —— 数模的图点数多，Canvas 更稳。
  CanvasRenderer,
])

/**
 * 这个项目会出现的 option 的联合类型。
 *
 * 用 `ComposeOption` 从上面注册过的东西里**推导**出来，而不是手写一份 ——
 * 于是「注册了但类型里没有」和「类型里有但没注册」两种不同步都不可能发生。
 */
export type EChartsOption = ComposeOption<
  | BarSeriesOption
  | LineSeriesOption
  | ScatterSeriesOption
  | PieSeriesOption
  | GridComponentOption
  | TooltipComponentOption
  | LegendComponentOption
  | TitleComponentOption
  | AriaComponentOption
>

/** 图表实例。`InteractiveChart` 的 ref 用这个类型。 */
export type EChartsInstance = echarts.EChartsType

export const initChart = echarts.init

/**
 * 这个 DOM 上已经挂着的实例（没有就返回 undefined）。
 *
 * 用途只有一个：`init` 之前确认它上面没有遗留实例 —— 对一个已经有实例的
 * 容器再 `init`，ECharts **不报错**，只警告一句然后把旧实例还给你。
 */
export const getInstanceByDom = echarts.getInstanceByDom
