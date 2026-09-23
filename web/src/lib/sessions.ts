/**
 * 会话的 REST 接口 —— 对应后端 `modelforge/api/sessions.py`。
 *
 * 流式的那几个端点（messages / decisions / resume）在 `sse.ts` 里，
 * 因为它们要一边收一边处理。这里放的是**一问一答**式的那些：
 * 建会话、拿详情、删会话。
 *
 * ════════════════════════════════════════════════════════════════════
 * 这个文件里唯一值得展开讲的是 `getSession`
 * ════════════════════════════════════════════════════════════════════
 * 它是「刷新页面不丢」这条产品承诺在客户端的落点。
 *
 * 在没有它之前（M2/M3），对话历史活在 React 的 state 里 —— 刷新一下
 * 就全没了，因为那个 state 只存在于这一页的内存中。现在历史在后端的
 * 事件日志里，前端启动时把它拉回来重建界面即可。
 *
 * 注意拉回来的是**事件**而不是渲染好的消息。这是因为界面要的不只是
 * 对话气泡：工具卡片、决策卡片、时间线都要。把「事件 → 视图」这段
 * 逻辑放在前端的纯函数里（`chat-state.ts`），实时路径和回放路径就能
 * 共用同一份实现 —— 否则「续流的输出接到哪个气泡里」这件事会有两个
 * 版本，而它们迟早会不一致。
 */

import type { Session, SessionDetail } from './log-types'

/** 后端地址。静态点号写法，Next.js 才能把 NEXT_PUBLIC_ 变量内联进来。 */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000'

/**
 * 把 HTTP 错误变成一句能直接给用户看的话。
 *
 * 后端的 `HTTPException` 会把 detail 写成**给用户看的人话**
 * （比如「还有一个问题在等你拍板，先回答它再发新消息」），
 * 所以这里把它原样取出来是最有价值的做法 —— 比报「409」有用得多。
 */
async function failure(response: Response): Promise<Error> {
  const raw = await response.text()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
      const detail = (parsed as { detail: unknown }).detail
      if (typeof detail === 'string') return new Error(detail)
    }
  } catch {
    /* 不是 JSON，用原始文本 */
  }
  return new Error(`后端返回 ${response.status}：${raw.slice(0, 200)}`)
}

export async function createSession(): Promise<Session> {
  const response = await fetch(`${API_BASE}/api/sessions`, { method: 'POST' })
  if (!response.ok) throw await failure(response)
  return (await response.json()) as Session
}

/** 拿会话的全部状态 —— 界面靠它重建。 */
export async function getSession(sessionId: string): Promise<SessionDetail> {
  const response = await fetch(`${API_BASE}/api/sessions/${sessionId}`)
  if (!response.ok) throw await failure(response)
  return (await response.json()) as SessionDetail
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/sessions/${sessionId}`, {
    method: 'DELETE',
  })
  if (!response.ok) throw await failure(response)
}

/** 列出最近的会话。M4 还不做界面，但接口先留着（M5 的会话列表要用）。 */
export async function listSessions(): Promise<Session[]> {
  const response = await fetch(`${API_BASE}/api/sessions`)
  if (!response.ok) throw await failure(response)
  return (await response.json()) as Session[]
}
