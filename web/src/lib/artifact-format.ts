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
