/**
 * 报告生成的 SSE 客户端 + 进度 reducer。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么传的是一套**自己的**事件类型
 * ════════════════════════════════════════════════════════════════════
 * 帧解析（字节碎片 + 帧碎片两级缓冲那段）是通用的，直接复用 `sse.ts`
 * 的 `readFrames` —— 那 60 行踩过的坑没必要再踩一遍。
 *
 * 但**事件类型**不能复用聊天那套：聊天那边的事件名是 `text_delta` /
 * `finish` / `error`，报告这边叫 `report_start` / `report_done`。
 * 两边混用的话，任何共用一个读取方的代码都会把 A 流的错误当成 B 流的。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么流完了还要以 `document` 为准
 * ════════════════════════════════════════════════════════════════════
 * 过程中前端自己在拼一份「进度版」的文档（正文边流边接），
 * 而 `report_done` 带回来的是后端渲染好的**权威版本**。两者会差一点点：
 *
 *     · 数字核对的结果（`NoteBlock`）是正文写完才知道的
 *     · 正文两端的空白会被 strip
 *
 * 所以收到 `report_done` 就**整个换掉**，不要继续用拼出来的那份。
 * 差的那一点点正是「核对过了」这件事本身 —— 而那是有价值的信息。
 */

import type {
  ReportDocument,
  ReportFrame,
  ReportHandle,
  ReportSection,
  ReportBlock,
} from './report-types'
import { describeHttpError, readFrames } from './sse'

/** 后端地址。静态点号写法，Next.js 才能把 NEXT_PUBLIC_ 变量内联进来。 */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000'

/** 已知的五种帧名。认不出来的帧**跳过而不是报错** —— 见下面的说明。 */
const KNOWN_FRAMES = new Set([
  'report_start',
  'report_text',
  'report_section_done',
  'report_done',
  'report_error',
])

// ══════════════════════════════════════════════════════ 传输

/**
 * 生成一份报告，把帧逐个吐出来。
 *
 * 错误分两层，和别的流式端点一致：
 *   · **流开始之前** 失败 → 直接抛（404 / 409 / 400 都带着后端写的人话）
 *   · **流开始之后** 失败 → 不抛，而是来一帧 `report_error`
 */
export async function* streamReport(
  sessionId: string,
  signal?: AbortSignal,
): AsyncGenerator<ReportFrame> {
  const response = await fetch(`${API_BASE}/api/sessions/${sessionId}/report`, {
    method: 'POST',
    signal,
  })

  if (!response.ok) {
    throw new Error(describeHttpError(response.status, await response.text()))
  }
  if (!response.body) {
    throw new Error('响应没有 body，无法流式读取')
  }

  for await (const frame of readFrames(response.body)) {
    // 认不出来的帧直接跳过，**不炸整条流** —— 后端将来加一种帧（比如
    // 「这一节用到了缓存」），旧前端应该继续把报告读完，而不是白屏。
    // 这和 `sse.ts` 对坏帧的处理是同一条取舍。
    if (!KNOWN_FRAMES.has(frame.event)) {
      console.warn('报告流里出现了不认识的帧，已跳过：', frame.event)
      continue
    }
    let data: unknown
    try {
      data = JSON.parse(frame.data)
    } catch {
      console.warn('报告流的帧不是合法 JSON，已跳过：', frame.data)
      continue
    }
    yield { name: frame.event, data } as ReportFrame
  }
}

// ══════════════════════════════════════════════════════ 进度

/**
 * 一节正在长的样子 —— **界面只认这一种形状**。
 *
 * 为什么不直接渲染 `ReportSection`：流的过程中正文是一段段来的
 * （`report_text`），而成品里它已经变成了一个 `ProseBlock`。
 * 两种形态渲染两套代码的话，「边流边看」和「看成品」迟早会长得不一样 ——
 * 而用户会以为是自己看错了。这里统一成 draft，
 * 到手的成品也**先转成 draft 再渲染**（见 `toDraft`）。
 */
export type SectionDraft = {
  title: string
  /** 正文。流的时候一段段往上接，成品里从 `ProseBlock` 取出来。 */
  prose: string
  /** 程序块：决策记录 / 图 / 代码。**骨架一发下来就有** —— 图不必等正文。 */
  blocks: ReportBlock[]
  /** 程序插的说明：数字核对的结果。和正文分开，因为**它不该被当成论文的一部分**。 */
  notes: string[]
  /** 这一节的正文写完了（不管成不成功）。 */
  done: boolean
  /** 失败原因。null = 正常。 */
  error: string | null
}

export type ReportProgress = {
  /** 有没有在跑。false 且 `document === null` 且 `error !== null` 就是失败了。 */
  running: boolean
  title: string
  sections: SectionDraft[]
  /** 面向用户的错误文案。 */
  error: string | null
  /**
   * 后端渲染好的权威版本。**到了就整份换掉**，别再用 `sections` 里拼的那份。
   *
   * 两者会差一点点（数字核对的结论是正文写完才知道的）。见文件头的说明。
   */
  document: ReportDocument | null
  /** 落盘之后拿到的句柄 —— `page.tsx` 用它把新报告插进产物清单。 */
  report: ReportHandle | null
}

export function emptyProgress(): ReportProgress {
  return {
    running: false,
    title: '',
    sections: [],
    error: null,
    document: null,
    report: null,
  }
}

export function startProgress(): ReportProgress {
  return { ...emptyProgress(), running: true }
}

/**
 * 把一帧折进进度 —— **纯函数**，和 `chat-state.applyEvent` 同一个套路。
 *
 * 抽成纯函数不是为了「函数式好看」，是因为流的中间态最容易出岔子
 * （某一节的 index 越界、done 之后又来 text），而纯函数能被穷举测试。
 */
export function applyReportFrame(
  progress: ReportProgress,
  frame: ReportFrame,
): ReportProgress {
  switch (frame.name) {
    case 'report_start':
      return {
        ...progress,
        running: true,
        title: frame.data.title,
        sections: frame.data.sections.map((section) => ({
          title: section.title,
          prose: '',
          // 骨架里只有程序块 —— 正文是后面一点点流过来的。
          blocks: section.blocks,
          notes: [],
          done: false,
          error: null,
        })),
      }

    case 'report_text': {
      const { index, delta } = frame.data
      return {
        ...progress,
        sections: patchSection(progress.sections, index, (section) => ({
          ...section,
          prose: section.prose + delta,
        })),
      }
    }

    case 'report_section_done': {
      const { index, ok, message } = frame.data
      return {
        ...progress,
        sections: patchSection(progress.sections, index, (section) => ({
          ...section,
          done: true,
          error: ok ? null : (message ?? '这一节没能写出来。'),
        })),
      }
    }

    case 'report_done':
      // 以权威版本为准：把拼出来的那份整个换掉。
      return progressFromDocument(frame.data.document, frame.data.report)

    case 'report_error':
      return { ...progress, running: false, error: frame.data.message }
  }
}

/**
 * 一份**已经写好的**文档 → 进度。
 *
 * 用在两处：`report_done` 收到权威版本时，以及**刷新之后重新打开**
 * 一份旧报告时（从 `.report.json` 读回来的是同样的 `ReportDocument`）。
 *
 * 两条路共用这一个转换，所以「刚生成的报告」和「昨天生成的报告」
 * 在界面上长得一模一样 —— 而不是一个能悬停、另一个布局不同。
 */
export function progressFromDocument(
  document: ReportDocument,
  report: ReportHandle | null = null,
): ReportProgress {
  return {
    running: false,
    title: document.title,
    sections: document.sections.map(toDraft),
    error: null,
    document,
    report,
  }
}

/**
 * 成品的一节 → 界面用的 draft。
 *
 * ⚠️ **三种块分开**：正文进 `prose`，说明进 `notes`，其余（决策 / 图 / 代码）
 * 留在 `blocks`。不分开的话，界面就得在一个数组里按 `kind` 分支三次，
 * 而「正文该占多大、说明该用什么颜色」这些事本来是布局决定的，不是块类型决定的。
 */
function toDraft(section: ReportSection): SectionDraft {
  const prose: string[] = []
  const notes: string[] = []
  const blocks: ReportBlock[] = []

  for (const block of section.blocks) {
    if (block.kind === 'prose') prose.push(block.text)
    else if (block.kind === 'note') notes.push(block.text)
    else blocks.push(block)
  }

  return {
    title: section.title,
    prose: prose.join('\n\n'),
    blocks,
    notes,
    done: true,
    error: null,
  }
}

/**
 * 给第 `index` 节打补丁。**越界时原样返回**（引用不变）。
 *
 * 越界不是理论上的：后端发 `report_start` 之前的那一帧如果丢了
 * （网络抖动导致整帧没到），后面每一帧的 index 都指向一个不存在的节。
 * 那种时候静默跳过远好过抛异常 —— 用户至少还能看到已经流出来的部分。
 */
function patchSection(
  sections: SectionDraft[],
  index: number,
  fn: (section: SectionDraft) => SectionDraft,
): SectionDraft[] {
  if (index < 0 || index >= sections.length) return sections
  return sections.map((section, i) => (i === index ? fn(section) : section))
}
