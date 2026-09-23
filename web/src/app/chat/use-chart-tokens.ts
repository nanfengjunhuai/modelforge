'use client'

/**
 * 订阅设计令牌的 React 那一半（纯读在 `lib/chart-tokens.ts`）。
 *
 * ════════════════════════════════════════════════════════════════════
 * ⚠️ 为什么在**模块求值时**读，而不是在 render 或 effect 里读
 * ════════════════════════════════════════════════════════════════════
 * 三种更直觉的写法都被规则或正确性挡掉了：
 *
 *   `useMemo(() => readChartTokens())`  在 render 里读 DOM。
 *                                       `react-hooks/purity` 盯的就是渲染期的副作用。
 *   `useEffect` + `setState`            被 `react-hooks/set-state-in-effect`
 *                                       直接判错（本项目是 error 级），
 *                                       而且要多渲染一轮。
 *   `getSnapshot` 里现读现缓存         `getSnapshot` 必须**每次返回同一个引用**，
 *                                       否则 React 无限重渲染；而在它里面写
 *                                       模块级缓存又撞上 `react-hooks/globals`
 *                                       （渲染期不许写全局）。两条都是 error。
 *
 * 所以：**模块求值时读一次**（那时候还不存在「渲染期」这回事），
 * 之后靠一个模块级的 matchMedia 监听更新。`getSnapshot` 只读不写，
 * 三个规则都不碰，也永远不会返回新引用。
 *
 * ⚠️ 模块在服务端也会被求值一次，那次没有 `document`，于是拿到一份
 *    空令牌。这**不会**造成水合不匹配 —— 用这个 hook 的地方
 *    （交互图、表格视图）都只在客户端出现：交互图是 `ssr: false` 的，
 *    表格要用户点一下才渲染。
 */

import { useSyncExternalStore } from 'react'

import { EMPTY_TOKENS, readChartTokens, type ChartTokens } from '@/lib/chart-tokens'

const DARK_QUERY = '(prefers-color-scheme: dark)'

function hasDom(): boolean {
  return typeof window !== 'undefined' && typeof document !== 'undefined'
}

let current: ChartTokens = hasDom() ? readChartTokens() : EMPTY_TOKENS

const listeners = new Set<() => void>()

function notify(): void {
  for (const listener of listeners) listener()
}

function refresh(): void {
  current = readChartTokens()
}

if (hasDom() && typeof window.matchMedia === 'function') {
  // ⚠️ 主题一变必须**重新读**，不能只翻一个布尔。
  //
  // 设计系统在暗色模式下是**重新选步号**，不是把亮色反转 ——
  // `--chart-cat-2` 亮 `#1baf7a` / 暗 `#199e70`，这两个值之间没有
  // 任何公式关系。所以只能重新求值。
  window
    .matchMedia(DARK_QUERY)
    .addEventListener('change', () => {
      refresh()
      notify()
    })

  // ⚠️ 上面那次模块求值可能发生在样式表就绪**之前**（脚本先于 CSS 应用），
  // 那样会读到一份空令牌，而且**再也不会有事件来纠正它** ——
  // 用户看到的是「图上的颜色全丢了，刷新一下又好了」。
  // 所以补一次：DOM 就绪后重读一遍，并通知已经挂上的订阅者。
  if (document.readyState === 'loading') {
    document.addEventListener(
      'DOMContentLoaded',
      () => {
        refresh()
        notify()
      },
      { once: true },
    )
  }
}

// 这两个放模块作用域不只是风格问题：`useSyncExternalStore` 在 `subscribe`
// 的身份变化时会退订再重订，而这个项目的 React Compiler **没有开启**
// （`next.config.ts` 是空的），没有编译器帮忙把内联函数稳定下来 ——
// 写成模块级常量是唯一不依赖「记得加 useCallback」的做法。
function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => {
    listeners.delete(onChange)
  }
}

function getSnapshot(): ChartTokens {
  // 只返回缓存好的那个对象。**不新建** —— 新建的话 React 每次比较
  // 都认为变了，直接无限重渲染。
  return current
}

function getServerSnapshot(): ChartTokens {
  return EMPTY_TOKENS
}

/**
 * 当前主题下的图表令牌。系统主题切换时这个 hook 会让组件重渲染，
 * 图**当场**换色，不需要刷新。
 */
export function useChartTokens(): ChartTokens {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
