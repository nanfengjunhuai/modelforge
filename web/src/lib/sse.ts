/**
 * SSE 客户端 —— 手写帧解析。
 *
 * ════════════════════════════════════════════════════════════════════
 * 为什么不用浏览器自带的 EventSource？
 * ════════════════════════════════════════════════════════════════════
 * `EventSource` 是浏览器内置的 SSE 客户端，能自动重连、自动解析帧，
 * 看起来正是我们想要的。但它有一个致命限制：
 *
 *     **只支持 GET，不能带请求体。**
 *
 * 而我们要把整个对话历史发给后端 —— 这天然是 POST 语义。把消息列表塞进
 * query string 既丑陋又有长度上限（URL 一般限制在 2KB～8KB，几轮对话就爆了），
 * 而且内容会完整地留在服务器访问日志里。
 *
 * 所以走 `fetch` + `ReadableStream`：POST 请求体照常发，然后手动读响应流、
 * 手动解析 SSE 帧。代价是要自己写下面这 60 行解析代码 —— 但这 60 行换来的是
 * 完全可控的传输层，而且顺带解决了 EventSource 的另一个问题：
 *
 *     对「模型生成」这种请求来说，**自动重连是有害的**。
 *
 * 普通 SSE（股票行情、通知推送）重连是好事，重连上来接着收就行。
 * 但生成流重连意味着**重新跑一遍模型** —— 用户会看到前半句话被重新吐一遍，
 * 而且白花一份 token。我们宁可断开就是断开，由用户决定要不要重来。
 *
 * EventSource 还缺一个能力：不能发自定义请求头。M6 要加鉴权时，
 * `fetch` 可以直接加 `Authorization`，EventSource 只能退而求其次用 Cookie
 * 或 query 参数（后者会把 token 写进日志）。这个坑现在不痛，将来会痛。
 *
 * ════════════════════════════════════════════════════════════════════
 * 这个文件里最值钱的一个知识点：碎片是分层的
 * ════════════════════════════════════════════════════════════════════
 * M1 处理过一种碎片：模型把工具调用参数一小段一小段地吐出来，要累积。
 * 这里会遇到**另外两种**碎片，而且它们比第一种更隐蔽：
 *
 *   ① **字节碎片** —— 网络给你的不是「字符」，是「字节数组」。
 *      中文在 UTF-8 里占 3 个字节，而分片边界是任意的，完全可能把一个汉字
 *      劈成两半。→ 用 `TextDecoder` 的 `{ stream: true }` 模式兜住。
 *
 *   ② **帧碎片** —— 一次 `read()` 拿到的可能是半帧，也可能是三帧半。
 *      → 把字节解码成文本后先攒进 buffer，再按空行切帧。
 *
 * 三种碎片（JSON 参数 / 字节 / 帧）的共同规律是：
 *     **不要假设一次到达的就是一个完整单位。**
 * 这是所有流式编程的通用心法。
 */

import type { StreamEvent } from './stream-types'

/** 后端地址。静态点号写法，Next.js 才能把 NEXT_PUBLIC_ 变量内联进来。 */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000'

/** 发出去的一条消息，格式与 OpenAI chat 一致（后端 ADR-003 定的内部标准）。 */
export type ChatMessage = {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export type StreamChatOptions = {
  /** 用来中途取消（对应界面上那个「停止」按钮）。 */
  signal?: AbortSignal
  /** 覆盖后端配置里的默认 provider，比如临时切到 'ollama'。 */
  provider?: string
  temperature?: number
}

/** 一帧 SSE 的原始形态，还没做 JSON 解析。 */
export type RawFrame = {
  event: string
  data: string
}

// ══════════════════════════════════════════════════════ 帧解析

/**
 * 把一段「帧文本」解析成 `{ event, data }`。
 *
 * 帧文本长这样（已经从流里切出来了，不含结尾的空行）：
 *
 *     event: text_delta
 *     data: {"type":"text_delta","text":"熵"}
 *
 * 返回 `null` 表示这段不算一帧（纯空白、或者只有注释）。
 */
function parseFrame(raw: string): RawFrame | null {
  let event = 'message' // SSE 规范的默认事件名
  const dataLines: string[] = []

  for (const line of raw.split('\n')) {
    // 空行 —— 跳过（正常情况下不会走到，因为调用方已经按空行切过了）
    // 以冒号开头的是**注释**，规范里规定接收方必须忽略。
    // 服务器常用它做心跳保活（发一个 ": ping" 防止连接被中间层掐断）。
    if (line === '' || line.startsWith(':')) continue

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)

    // ⚠️ 规范里这条很容易漏：冒号后**如果紧跟一个空格**，那个空格是分隔符，
    //    要吃掉。写后端时 `f"data: {json}"` 里那个空格就是它。
    //    不吃掉的话，JSON.parse 会拿到 " {...}" —— 直接抛异常。
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'event') {
      event = value
    } else if (field === 'data') {
      // 规范允许多行 data，接收方用换行拼起来。
      // 我们后端只发一行（JSON 里不会有裸换行），但这里照着规范实现，
      // 免得将来后端换成原始文本时这里静默出错。
      dataLines.push(value)
    }
    // 其它字段（id / retry）我们用不到，忽略即可 ——
    // 它们主要服务于 EventSource 的断线重连，而我们已经决定不要自动重连。
  }

  if (dataLines.length === 0 && event === 'message') return null
  return { event, data: dataLines.join('\n') }
}

/**
 * 把一个字节流切成 SSE 帧，逐个 yield 出来。
 *
 * 这是整个文件的核心。「一次 read 不等于一帧」这件事全在这里处理。
 */
export async function* readFrames(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<RawFrame> {
  // getReader() 是「独占读取」：拿到 reader 之后，这条流就只能由它读了。
  // 用完必须 releaseLock()，否则这条流会一直被占着。
  const reader = body.getReader()

  // ⚠️⚠️ 这一行是整个文件里最容易写错的地方，也是中文项目里最常见的乱码来源。
  //
  // 网络给的是**字节**。中文在 UTF-8 里占 3 个字节，而 TCP 分片的边界是任意的
  // —— 完全可能把「熵」这个字的 3 个字节劈成 2+1 落在相邻两次 read() 里。
  //
  // 如果每块都 `new TextDecoder().decode(chunk)`，被劈开的那个字会解码失败，
  // 变成一个「�」。而且它不一定报错，只是**悄悄**变成乱码。
  //
  // `{ stream: true }` 让 decoder 把不完整的尾巴留在自己的内部缓冲里，
  // 等下一块字节到了再接上。流结束时再调一次 `decode()`（不带参数）
  // 把残留吐干净。
  const decoder = new TextDecoder('utf-8')

  /** 尚未凑成完整帧的尾巴。可能是一个半帧，也可能只是半个换行符。 */
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      // 统一换行符。SSE 规范允许 \r\n、\n、\r 三种行尾，我们后端发的是 \n，
      // 但代理服务器有时会改写。整体替换是安全的：假如 \r 和 \n 不幸被劈到了
      // 两块里，这一轮只会剩下一个孤立的 \r 留在 buffer 里，
      // 下一轮补上 \n 之后就会被这次替换吃掉。
      buffer = buffer.replace(/\r\n/g, '\n')

      // 按**空行**切帧 —— 注意是两个连续换行，不是一个。
      // 用 while 循环而不是 if：一次 read 可能带来好几帧。
      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const raw = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const frame = parseFrame(raw)
        if (frame) yield frame
        sep = buffer.indexOf('\n\n')
      }
    }

    // 收尾两件事：
    //   ① 把 decoder 内部残留的半个字符吐出来
    //   ② 处理最后一帧 —— 万一服务端忘了发结尾的空行，没有这一步最后一帧就丢了
    buffer += decoder.decode()
    const tail = parseFrame(buffer)
    if (tail) yield tail
  } finally {
    // 无论是正常读完、还是被 abort() 打断，都要还回读取权。
    // 已经取消的情况下 releaseLock 可能抛异常，忽略即可 ——
    // 这里没有什么是需要补救的。
    try {
      reader.releaseLock()
    } catch {
      /* 流已被取消，无需处理 */
    }
  }
}

// ══════════════════════════════════════════════════════ 对外接口

/**
 * 发一个 POST 并把响应当作 SSE 流读回来 —— 所有流式端点的公共部分。
 *
 * 抽出来是因为 M4 之后有**四个**流式端点（无状态的 /chat/stream，
 * 以及会话的 messages / decisions / resume）。它们要发的请求体不同，
 * 但「怎么读回来」完全一样，而那部分正是最容易写错的地方。
 *
 * 错误分两层，两边都要处理（后端 chat.py 里有详细解释）：
 *
 *   · **流开始之前** 失败 → 这个函数直接 `throw`（fetch 失败、HTTP 4xx/5xx）。
 *     调用方用一个 try/catch 就能接住。
 *   · **流开始之后** 失败 → 不抛异常，而是 yield 一条 `{ type: 'error' }` 事件。
 *     因为 HTTP 头早就发出去了，改不了状态码，只能在流里面报。
 */
async function* postStream(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })

  if (!response.ok) {
    // 后端在流开始前的失败会带着一句人话放在 detail 里（见 sessions.py 的
    // HTTPException）。读出来拼进错误信息，比只报「409」有用得多 ——
    // 那些 409 的文案本身就是给用户看的（「还有一个问题在等你拍板」）。
    const raw = await response.text()
    throw new Error(describeHttpError(response.status, raw))
  }

  if (!response.body) {
    // 理论上不会发生（fetch 成功就有 body），但类型上是可空的，
    // 而且真发生了的话下面会崩在一个很难懂的地方。提前拦住。
    throw new Error('响应没有 body，无法流式读取')
  }

  for await (const frame of readFrames(response.body)) {
    let parsed: unknown
    try {
      parsed = JSON.parse(frame.data)
    } catch {
      // 单帧坏了不该炸掉整条流 —— 后面的内容还是好的，用户还能接着看。
      // 记一笔到控制台，然后跳过这一帧。
      console.warn('SSE 帧不是合法 JSON，已跳过：', frame.data)
      continue
    }
    yield parsed as StreamEvent
  }
}

/**
 * 把 FastAPI 的错误响应变成一句人话。
 *
 * FastAPI 的错误体会是 `{"detail": "..."}`，而 `detail` 可能是字符串
 * （我们抛的 HTTPException）也可能是**对象数组**（Pydantic 的校验错误）。
 * 直接 `text.slice()` 显示出来的话，用户会看到一坨 JSON。
 */
export function describeHttpError(status: number, raw: string): string {
  const detail = raw.slice(0, 300)
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
      const value = (parsed as { detail: unknown }).detail
      if (typeof value === 'string') {
        return value
      }
      if (Array.isArray(value)) {
        // Pydantic 的校验错误：挑每条里的 msg 拼起来，够用了。
        const messages = value
          .map((item) =>
            item && typeof item === 'object' && 'msg' in item
              ? String((item as { msg: unknown }).msg)
              : '',
          )
          .filter(Boolean)
        if (messages.length) return messages.join('；')
      }
    }
  } catch {
    /* 不是 JSON —— 那就用原始文本 */
  }
  return `后端返回 ${status}：${detail}`
}

// ══════════════════════════════════════════════════════ 四个端点

/**
 * 无状态的流式对话 —— M2 留下的调试端点。
 *
 * 前端**已经不用它了**（M4 之后主路径是会话驱动的）。留着是因为它
 * 在调试提示词时很方便：不建会话、不落盘，改一句 system prompt 就能试。
 */
export async function* streamChat(
  messages: ChatMessage[],
  options: StreamChatOptions = {},
): AsyncGenerator<StreamEvent> {
  yield* postStream(
    '/api/chat/stream',
    // 显式列出要发的字段，而不是把 options 整个展开 ——
    // 免得以后加了前端专用的选项（比如 UI 开关）被顺手发给后端。
    { messages, provider: options.provider, temperature: options.temperature },
    options.signal,
  )
}

/**
 * 往会话里发一条用户消息，开一轮新的生成。
 *
 * ⚠️ 注意这里**只发这一条消息**，不像 M2/M3 那样把整个历史重发一遍。
 * 历史在后端的事件日志里，它会自己投影出来 —— 这正是 M4 的全部意义：
 * 状态归后端所有，所以刷新页面、换台机器、服务重启都不丢。
 */
export async function* streamSessionMessage(
  sessionId: string,
  content: string,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  yield* postStream(`/api/sessions/${sessionId}/messages`, { content }, signal)
}

/** 提交一个决策点的答案，让 Agent 接着往下跑。 */
export async function* streamDecision(
  sessionId: string,
  submission: { callId: string; choice: string; note?: string },
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  yield* postStream(
    `/api/sessions/${sessionId}/decisions`,
    {
      // 后端要 snake_case（Python 那边是 `call_id`）——
      // 跨语言的字段名映射就在这里做，不要让调用方去记。
      call_id: submission.callId,
      choice: submission.choice,
      note: submission.note ?? '',
    },
    signal,
  )
}

/**
 * 按当前历史重开一条流，**不追加任何事件**。
 *
 * 用在「上一轮跑到一半断了」的时候（网络抖动、后端重启）。
 * 因为既然一切都能从事件日志重建，重试就是免费的 ——
 * 这是「状态落盘 + 重建」相对「进程内挂起一个 Future」的核心优势之一。
 */
export async function* streamResume(
  sessionId: string,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  yield* postStream(`/api/sessions/${sessionId}/resume`, undefined, signal)
}
