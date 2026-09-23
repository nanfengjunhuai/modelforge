/**
 * 报告的 TypeScript 镜像 —— 后端 `modelforge/reports/blocks.py` 的对应物。
 *
 * ════════════════════════════════════════════════════════════════════
 * 这份类型描述的是一份**报告**，不是一次对话
 * ════════════════════════════════════════════════════════════════════
 * 一份报告是一串**类型化的块**。块的种类决定了这一段是谁写的：
 *
 *     prose      模型写的正文         ← 报告里唯一由模型生成的东西
 *     decision   用户当时拍板的记录   ┐
 *     figure     一张图               ├ 全部由程序从事件日志投影，模型碰不到
 *     code       一次代码执行         ┘
 *     note       程序插的一句说明
 *
 * 这个区分不是分类癖 ——**它是这份报告可不可信的全部依据**。
 * 界面上两者的样式明显不同，用户应该一眼看出「这句是模型在发挥，
 * 那句是我自己的过程记录」。
 *
 * ════════════════════════════════════════════════════════════════════
 * ⚠️ 和后端是手动同步的
 * ════════════════════════════════════════════════════════════════════
 * 防线是 `tests/test_report_contract.py`：它**真的调用**后端那个联合，
 * 把产出的 kind 集合和字段名集合拿来和这个文件比对。
 *
 * 不同步的症状是安静的：把 `artifact_id` 写成 `artifactId`，
 * Python 全绿、tsc 全绿，只是图**永远不出现** —— 而界面上看起来
 * 就像「这份报告本来就没有图」。
 */

import type { LogReportEvent } from './log-types'

// ══════════════════════════════════════════════════════ 五种块

/** 模型写的正文段落 —— 报告里唯一由模型生成的东西。 */
export type ProseBlock = {
  kind: 'prose'
  text: string
}

/**
 * 用户在某个分岔口拍板的记录。
 *
 * **报告里最值钱的一块。** 它带着当时的候选、用户选的那个、以及用户
 * 写的备注 —— 「你当时为什么选 AHP」比「你选了 AHP」重要得多，
 * 而快照式的状态存储答不上来这个问题。
 */
export type DecisionBlock = {
  kind: 'decision'
  /** 这个决策发生在日志里的第几步。 */
  seq: number
  question: string
  options: string[]
  /** 用户选的那个。**一定是已答复的** —— 没答的决策不进报告。 */
  choice: string
  /** 用户拍板时写的备注。空串表示他没写。 */
  note: string
}

/** 一张图 —— 引用产物，不复制内容。 */
export type FigureBlock = {
  kind: 'figure'
  /** 渲染/下载走 `/api/artifacts/{session}/{id}`。 */
  artifact_id: string
  /** 产物显示名（可能是中文）。 */
  name: string
  /**
   * 模型画图时自己写的那句话（`mp.save(..., caption=...)`）。
   * 空串表示读不到 —— 那张图就只剩文件名。不抛异常：少一句说明，
   * 远没有「整份报告生成不出来」严重。
   */
  caption: string
}

/** 一次代码执行 —— 建立与求解那一节的证据。 */
export type CodeBlock = {
  kind: 'code'
  seq: number
  /** 跑没跑通。失败的那次也留着。 */
  ok: boolean
  code: string
  /** **截断过的**输出。界面和导出的 .md 共用同一份。 */
  stdout: string
}

/**
 * 程序插进来的一句说明，不是正文。
 *
 * 目前两个用途：号码核对（这一段的正文里有几个数字在过程记录里找不到
 * 出处）、以及「这一节的正文没生成出来」。
 *
 * 措辞是**刻意弱**的 —— 说「找不到出处」，不说「模型编造」。
 * 它确实会误报派生值（百分比、差值、四舍五入），
 * 而一个动不动喊狼来了的提示比没有提示更糟。
 */
export type NoteBlock = {
  kind: 'note'
  text: string
}

export type ReportBlock =
  | ProseBlock
  | DecisionBlock
  | FigureBlock
  | CodeBlock
  | NoteBlock

/**
 * 五种块的 kind 取值 —— 和后端 `reports/blocks.py` 的 `BLOCK_KINDS` 一一对应。
 *
 * ⚠️ 用 `as const` 数组而不是从上面的联合里推：跨语言的契约测试要拿它
 * 和后端的 frozenset 比对，从联合里反推需要 TS 的类型运算，正则抠不出来。
 */
export const BLOCK_KINDS = ['prose', 'decision', 'figure', 'code', 'note'] as const

// ══════════════════════════════════════════════════════ 文档

/** 报告的一节。 */
export type ReportSection = {
  title: string
  blocks: ReportBlock[]
}

/**
 * 一整份报告 —— 这就是 `.report.json` 里存的东西。
 *
 * 和 `.md` 的关系是「同一份数据的两个视图」（ADR-012 在图表上定的形状）：
 * Markdown 给人（可下载、能粘进 Word），这个结构给程序（能区分哪块是谁写的）。
 * 两边由同一个后端对象渲染出来，所以**不可能对不上**。
 */
export type ReportDocument = {
  /** 格式版本。这份结构会被存进磁盘，所以旧文件可能躺在用户机器上。 */
  version: number
  title: string
  session_id: string
  sections: ReportSection[]
  /** 文档级的说明。目前用来如实记下「哪一节的正文没生成出来」。 */
  notes: string[]
}

// ══════════════════════════════════════════════════════ 流事件

/**
 * 报告生成流的五种帧。
 *
 * ⚠️ **事件名统一带 `report_` 前缀，和聊天那套刻意分开。**
 * 后端 `format_sse` / `_sse` 直接拿事件名当 SSE 的 `event:` 字段，而聊天那边
 * 已经有 `error` / `finish` 了。报告流如果也叫 `error`，任何共用一个读取方的
 * 代码都会把两个流的错误混到一起。
 *
 * 帧的**名字**在 SSE 的 `event:` 行里，**载荷**在 `data:` 行里 ——
 * 所以这里是一个 `{ name, data }` 的判别联合，而不是把 name 塞进载荷。
 * `chat-state.applyEvent` 那边是反过来的（载荷自带 `type` 判别子），
 * 因为那边的载荷是 Pydantic 事件。两种都对，别硬凑成一种。
 */
export type ReportFrame =
  | { name: 'report_start'; data: ReportStart }
  | { name: 'report_text'; data: { index: number; delta: string } }
  | {
      name: 'report_section_done'
      data: { index: number; ok: boolean; message?: string }
    }
  | { name: 'report_done'; data: { document: ReportDocument; report: ReportHandle } }
  | { name: 'report_error'; data: { message: string } }

/** 报告一开始就发过来的骨架：节标题 + 程序块（正文此时还是空的）。 */
export type ReportStart = {
  title: string
  sections: ReportSkeletonSection[]
}

export type ReportSkeletonSection = {
  title: string
  blocks: ReportBlock[]
}

/**
 * 一份已落盘的报告的句柄。
 *
 * ⚠️ **它就是后端那条 `LogReport` 日志事件，不是另一个东西** ——
 * `report_done` 帧里带回来的正是 `LogReport.model_dump()`。
 * 所以这里直接别名过去，而不是另写一份结构相同的类型：
 * 后者要手工保持两处同步，而它们**本来就该永远是同一个形状**。
 *
 * 换个名字只是因为在这个文件的语境里，「句柄」比「日志事件」读起来顺 ——
 * 这一层关心的是「刚拿到的这份报告」，不是「日志里那种记录」。
 */
export type ReportHandle = LogReportEvent
