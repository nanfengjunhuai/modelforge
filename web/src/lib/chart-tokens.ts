/**
 * 把 `globals.css` 里的设计令牌读进 JS —— 给画布用的那一半。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么需要这一层
 * ════════════════════════════════════════════════════════════════════
 * Tailwind 的工具类（`bg-surface` / `text-ink-muted`）背后是 CSS 变量，
 * 浏览器自己会解析。但 **ECharts 画在 canvas 上，`var(--x)` 在那里
 * 是一个没有任何意义的字符串** —— 它要的是 `#2a78d6` 这样的字面量。
 *
 * 于是就有了这条岔路：组件走 CSS 变量、图表走 JS 读出来的值。
 * 岔路本身不可怕，可怕的是它变成**第二份颜色定义** ——
 * 那正是 ADR-006 要防的事。所以这里一条颜色都不写字面量：
 * 全部 `getComputedStyle` 从 `:root` 读，`globals.css` 仍然是唯一真相源。
 *
 * ⚠️ **这个文件是纯的**（没有 React、没有订阅），和 `src/lib/` 里
 *    其他模块一样。订阅那半边在 `app/chat/use-chart-tokens.ts` ——
 *    「框架无关的逻辑放 lib、视图和 hook 放路由目录」是这个项目
 *    一直守着的分界。
 */

/**
 * 从设计系统里读出来的一组颜色 + 字体。
 *
 * ⚠️ **每个字段都可能是空字符串**（读不到、或者调用方在服务端跑到这里）。
 * 消费方必须容忍：`chart-spec.ts` 在取到空串时回退到 `.chart.json`
 * 里自带的那份颜色。这样「读不到」只有一条处理路径，不需要两套逻辑。
 */
export type ChartTokens = {
  /** 8 槽分类色。固定顺序，和 `--chart-cat-1..8` 一一对应。 */
  categorical: string[]
  /** 画布底色 —— 悬停提示框、散点描边用它和背景「断开」。 */
  surface: string
  /** 网格线（发丝级）。 */
  grid: string
  /** 坐标轴基线。 */
  axis: string
  /** 正文墨色，提示框文字用。 */
  ink: string
  /** 次级墨色，轴标签、图例文字用。 */
  inkSecondary: string
  /** 弱化墨色。 */
  inkMuted: string
  /** 字体栈，交给 canvas 用。 */
  fontFamily: string
}

/** 空令牌。读不到任何东西时的返回值 —— **不是**一份硬编码的亮色配色。 */
export const EMPTY_TOKENS: ChartTokens = {
  categorical: [],
  surface: '',
  grid: '',
  axis: '',
  ink: '',
  inkSecondary: '',
  inkMuted: '',
  fontFamily: '',
}

/**
 * 令牌在 CSS 里的名字。
 *
 * ⚠️ 注意这里**混着两个前缀**，而且是有意的：
 *    `--chart-*` 是坐标轴/网格/底色（图表的 chrome）
 *    `--mf-ink*`  是文字（全站共用一套墨色，没有单独的 `--chart-ink`）
 * 换个名字之前先想清楚 —— `--mf-grid` 和 `--chart-grid` 现在的值是
 * 一样的，写成哪个都能跑，所以写错了**不会报错**，只会等哪天其中一份
 * 被改的时候静默分叉。
 */
const CSS_VARS = {
  surface: '--chart-surface',
  grid: '--chart-grid',
  axis: '--chart-axis',
  ink: '--mf-ink',
  inkSecondary: '--mf-ink-secondary',
  inkMuted: '--mf-ink-muted',
} as const

/** 分类色板有几个槽。不能靠数 CSS 里有几行 —— 那是另一份会漂移的事实。 */
export const CATEGORICAL_SLOTS = 8

/**
 * 从当前文档里读一遍令牌。**读不到就返回空串，不返回兜底色值。**
 *
 * 什么时候会是空的：服务端（没有 `document`）、CSS 还没应用、
 * 或者将来有人把 `--chart-cat-*` 从 `:root` 挪进某个作用域选择器里。
 * 这三种情况都该由调用方**看得见地**降级，而不是被一个假的默认值盖过去。
 */
export function readChartTokens(): ChartTokens {
  if (typeof document === 'undefined') return EMPTY_TOKENS

  const root = getComputedStyle(document.documentElement)
  const read = (name: string): string => root.getPropertyValue(name).trim()

  // 字体栈要从 <body> 上读 —— `font-family` 是声明在 `body` 上的
  // （见 globals.css 第 4 节），`:root` 上没有这一条，在那里读是空串。
  //
  // ⚠️ 这是个**很容易写错且不会报错**的地方：读错了 canvas 就用
  //    ECharts 的默认字体，中文照样显示（系统会兜底），
  //    只是整套图和工作台的观感对不上，没人会来报这个 bug。
  const fontFamily =
    typeof document.body === 'undefined'
      ? ''
      : getComputedStyle(document.body).fontFamily.trim()

  return {
    categorical: Array.from({ length: CATEGORICAL_SLOTS }, (_, index) =>
      read(`--chart-cat-${index + 1}`),
    ),
    surface: read(CSS_VARS.surface),
    grid: read(CSS_VARS.grid),
    axis: read(CSS_VARS.axis),
    ink: read(CSS_VARS.ink),
    inkSecondary: read(CSS_VARS.inkSecondary),
    inkMuted: read(CSS_VARS.inkMuted),
    fontFamily,
  }
}
