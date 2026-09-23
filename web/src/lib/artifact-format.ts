/**
 * 产物在界面上怎么显示 —— 工具卡片和产物面板共用这一份。
 *
 * 单独拎出来不是为了「结构清晰」，是因为**同一个东西在两个地方
 * 各写一遍，迟早会长得不一样**：工具卡片里写着 63 KB、
 * 面板里写着 64 KB，用户不知道该信哪个。它们在 M5 是同一份代码，
 * M5b 把右侧面板加上之后就成了两处，所以提到这里。
 */

import type { ArtifactKind, ArtifactRef } from './stream-types'

/**
 * 产物的大类 → 中文标签。
 *
 * `image` 那一项其实就是「图片」两个字，但它很少被用到 ——
 * 图片走缩略图那条路，不走这个标签。留着是因为类型要求齐全，
 * 而少写一个键在 `Record<...>` 里是**编译错误**。
 */
export const KIND_LABEL: Record<ArtifactKind, string> = {
  image: '图片',
  data: '数据',
  document: '文档',
  other: '文件',
}

/**
 * 字节数 → 人看得懂的写法。
 *
 * 用 KB / MB（1000 进制）而不是 KiB / MiB：这里显示的是给用户估个大小的，
 * 「这个文件大概多大」比「精确到 1024 的二进制单位」重要得多。
 */
export function formatBytes(size: number): string {
  if (size < 1000) return `${size} B`
  if (size < 1000 * 1000) return `${Math.round(size / 1000)} KB`
  return `${(size / (1000 * 1000)).toFixed(1)} MB`
}

/**
 * 这个产物是不是「图背后的数据」。
 *
 * 约定是 `mp.save()` 写下的 `<名字>.chart.json`（见 ADR-012）——
 * 用扩展名判断而不是 MIME：这类文件是 `application/json`，
 * 而 `.json` 这个后缀才是「它配着一张图」的标志。
 */
export function isChartData(artifact: ArtifactRef): boolean {
  return artifact.name.endsWith('.chart.json')
}

/**
 * 产物的**分组键**：显示名去掉扩展名，再砍掉尾巴上的 `.chart`。
 *
 * 一件事被它解决：**同一个东西的两个视图在清单里是两行**。
 * `mp.save()` 一次会写下 `权重对比.png` + `权重对比.pdf` +
 * `权重对比.chart.json`，报告又会写下 `X.md` + `X.report.json` ——
 * 面板上一张图占三行、一份报告占两行，像个文件浏览器而不是「产物清单」。
 *
 * ⚠️ **不能直接用 `Path(name).stem`**（TS 里没有 Path，用 rsplit）。
 * 那正是这个函数存在的理由：
 *
 *     权重对比.chart.json  →  去一节扩展名后是 `权重对比.chart`
 *     权重对比.png         →  去一节扩展名后是 `权重对比`
 *
 * 两个配不上，于是产物的分组永远分不齐、图也找不到自己的 caption。
 *
 * 后端 `modelforge/reports/outline.py::name_key` 里有一份**逐字等价**的实现
 * （报告生成时用它把 caption 配到图上），两边的规则必须一致 ——
 * `tests/test_report_contract.py` 盯着。
 */
export function nameKey(name: string): string {
  const dot = name.lastIndexOf('.')
  let stem = dot > 0 ? name.slice(0, dot) : name
  if (stem.endsWith('.chart')) stem = stem.slice(0, -'.chart'.length)
  return stem.trim().toLowerCase()
}

/** 一组同名不同格式的产物 —— 界面上显示成**一行**。 */
export type ArtifactGroup = {
  /** 分组键。同组内所有产物的 `nameKey` 都等于它。 */
  key: string
  /** 显示名：取组里第一个产物去掉扩展名（保留原始大小写和中文）。 */
  label: string
  /** 组内成员，已排序（图 → 文档 → 数据 → 其他）。 */
  items: ArtifactRef[]
}

/**
 * 扩展名的显示顺序：图 → 文档 → 数据 → 其他。
 *
 * 为什么要有顺序：`Map` 的插入顺序决定了组内成员的排列，而产物的落盘顺序
 * 是模型的代码决定的（它会先存 PNG 还是 PDF 全看心情）。
 * 没有固定顺序的话，同一张图在不同会话里的清单长得不一样。
 */
const _KIND_ORDER: Record<ArtifactKind, number> = {
  image: 0,
  document: 1,
  data: 2,
  other: 3,
}

/** 扩展名，小写、带点。没有扩展名时返回空串。 */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot).toLowerCase() : ''
}

/**
 * 把产物的平铺列表按 `nameKey` 归组。
 *
 * **纯函数**，没有 I/O —— 所以它能在测试里被穷举，而分组的错法
 * （把两张不同的图并成一组、或者把一张图拆成两组）都是**看得见但说不清**的
 * 那种 bug：用户只会觉得「这清单有点乱」。
 */
export function groupArtifacts(artifacts: ArtifactRef[]): ArtifactGroup[] {
  const buckets = new Map<string, ArtifactRef[]>()
  for (const artifact of artifacts) {
    const key = nameKey(artifact.name)
    const bucket = buckets.get(key)
    if (bucket) bucket.push(artifact)
    else buckets.set(key, [artifact])
  }

  return [...buckets.entries()].map(([key, members]) => {
    // 组内排序：先按大类，同类再按扩展名字母序。
    // 后者是为了让 `.chart.json` 和 `.json` 这种同类的有个稳定次序。
    const items = [...members].sort((a, b) => {
      const byKind = _KIND_ORDER[a.kind] - _KIND_ORDER[b.kind]
      return byKind !== 0 ? byKind : extensionOf(a.name).localeCompare(extensionOf(b.name))
    })
    const first = items[0]
    const dot = first.name.lastIndexOf('.')
    let label = dot > 0 ? first.name.slice(0, dot) : first.name
    // 组里只有 `.chart.json` 一个成员时，去掉一节扩展名还剩个 `.chart`，
    // 显示成「权重对比.chart」很怪 —— 再砍一次。
    if (label.endsWith('.chart')) label = label.slice(0, -'.chart'.length)
    return { key, label, items }
  })
}
