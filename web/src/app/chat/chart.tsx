'use client'

/**
 * ECharts 的 React 外壳 —— **只管生命周期，不碰数据**。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么它接的是 `option` 而不是 `.chart.json`
 * ════════════════════════════════════════════════════════════════════
 * 因为 `buildChartOption()` 的结果里除了图，还有**表格和说明文字**，
 * 而它们要渲染在图表**旁边**（见 `panel.tsx`）。让这个组件自己去
 * 解析 spec 的话，同一个纯函数就要被调用两遍，两份结果还有可能
 * 因为入参不同而不一致。所以由调用方算一次，把算好的 `option` 传进来。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么拆成两个 effect
 * ════════════════════════════════════════════════════════════════════
 * 合成一个 `useEffect(..., [option])` 的话，**每次换主题都会
 * 把图表实例销毁重建**（cleanup → dispose → init）。虽然结果是对的，
 * 但那是白白丢掉一个 canvas 再要一个新的。
 *
 * 拆开之后：init 只在挂载时跑一次，`setOption` 负责后续所有的变化 ——
 * 换主题、换数据、换图型都走它。
 *
 * ⚠️ `setOption` 必须带 `{ notMerge: true }`。默认的合并策略是**按位置**
 * 把新 series 盖到旧的上面 —— 从「3 个系列的柱状图」切到「1 个系列的饼图」
 * 时，那两个没人认领的旧系列会**继续留在图上**。这种 bug 看起来像是
 * 数据错了，而不是像是没清干净。
 */

import { useEffect, useMemo, useRef } from 'react'

import type { EChartsOption, EChartsInstance } from '@/lib/echarts'
import { getInstanceByDom, initChart } from '@/lib/echarts'

export default function InteractiveChart({
  option,
  height = 340,
}: {
  option: EChartsOption
  /** 画布高度（像素）。要**包含横轴标签那一带**，不然轴会被裁掉。 */
  height?: number
}) {
  const holder = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsInstance | null>(null)

  // 图表实例是**外部资源**，不是渲染的产物 —— 所以放在 ref 里而不是
  // state 里。放 state 会触发重渲染，而重渲染又会走到下面的 effect，
  // 绕成一个圈。
  useEffect(() => {
    const element = holder.current
    if (!element) return

    // ⚠️ 先确认这个 DOM 上没有**遗留的**实例。
    //
    // 正常路径下不会走到这里（下面的 cleanup 会 dispose）。但开发模式
    // 的 StrictMode 会把挂载跑两遍，而对一个已经有实例的 DOM 再 init，
    // ECharts 会**警告一句然后把旧实例还给你** —— 于是你以为新建了一个，
    // 实际上在操作上一个。这类问题只在热重载频繁的时候偶发，
    // 极难复现，所以用一行把它变成不可能。
    getInstanceByDom(element)?.dispose()

    const instance = initChart(element)
    chart.current = instance

    // ⚠️ 字体还没加载完时量出来的文字宽度是**兜底字体**的宽度。
    // 末点标签靠这个宽度决定它会不会被画布右边缘裁掉 ——
    // 量错了就会出现「刚打开时标签被切了一半，拖一下窗口又好了」。
    // 等字体就绪后补一次 resize，让它按真实宽度重新排版。
    void document.fonts?.ready.then(() => instance.resize())

    // ⚠️ 这个观察者不是可有可无的：折叠侧栏、拖动窗口、切换主题
    // 都会改变容器宽度，而 ECharts 画布**不会自己跟着变** ——
    // 它会保持初始化那一刻的尺寸，表现为图被拉伸或者右边被裁掉。
    //
    // 顺带它还解决了另一个问题：容器第一次被观测到的尺寸如果是 0
    // （父级还没布局好），ECharts 会画出一块空画布；观察者随后
    // 报出真实尺寸，`resize()` 就把它救回来了。
    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(element)

    return () => {
      // 顺序不能反：先停观察，再销毁实例。反过来的话，销毁过程中
      // 元素尺寸变化会让观察者去调一个已经 dispose 的实例。
      observer.disconnect()
      instance.dispose()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    // 挂载那一轮这个 effect 紧跟在 init 后面跑，正好把首屏画出来。
    chart.current?.setOption(option, { notMerge: true })
  }, [option])

  // ⚠️ 用 inline style 而不是 Tailwind 的 `h-*`：高度是个**运行时数值**，
  // 而 Tailwind 只认它自己那套预设值。这也不违反「组件里禁止硬编码色值」
  // —— 那条规矩管的是颜色，不是尺寸。
  const style = useMemo(() => ({ height }), [height])

  return <div ref={holder} style={style} className="w-full" />
}
