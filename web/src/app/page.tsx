'use client'

// ════════════════════════════════════════════════════════════════════════
//  ModelForge 自检页 —— 验证前后端管道是否通畅。
//
//  注意这个文件里「没有任何硬编码的色值和字号」——全部走 globals.css 里
//  定义的设计令牌（bg-surface / text-ink-muted / text-display / rounded-card…）。
//  这是设计系统的意义：改配色只需要动 globals.css 一个文件，
//  不用翻遍所有组件。M2 之后新建的每个组件都照此办理。
// ════════════════════════════════════════════════════════════════════════

import { useEffect, useState } from 'react'

// ─────────────────────────────────────────────────────────── 类型

/**
 * 页面所处的阶段。
 *
 * 用一个联合类型而不是三个布尔值（isLoading / isError / hasData）：
 * 三个布尔值能组合出 8 种情况，其中 4 种逻辑上荒谬（比如「又加载又有数据又报错」）。
 * 用联合类型，非法状态从源头就不存在——与 Pydantic 的 Literal 是同一个思路。
 */
type Status = 'idle' | 'loading' | 'success' | 'error'

/** 后端 GET /api/health 的返回结构，字段名与 modelforge/api/health.py 一一对应。 */
type HealthInfo = {
  status: string
  service: string
  version: string
  default_provider: string
}

// ─────────────────────────────────────────────────────────── 常量

/**
 * 后端地址。必须用 process.env.XXX 这种静态点号写法——Next.js 在构建期
 * 把 NEXT_PUBLIC_ 开头的变量内联进 JS。动态取值（process.env[变量名]）
 * 无法内联，浏览器里会拿到 undefined。
 *
 * 留环境变量出口是因为部署后前后端不在同一台机器，localhost 会失效（见 M6）。
 */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000'

// ─────────────────────────────────────────────────────────── 组件

export default function Home() {
  // ══════════════ 状态 ══════════════
  //
  // 初始值必须是 'idle' 而不是 'loading'：
  // 组件刚渲染的那一瞬间 useEffect 还没执行，请求根本没发出去。
  // 如果这里写 'success'，后端挂掉时页面会先闪一下「成功」再跳到「失败」。
  const [status, setStatus] = useState<Status>('idle')
  const [info, setInfo] = useState<HealthInfo | null>(null)

  // ══════════════ 副作用 ══════════════
  //
  // 三个 setStatus 画出了这个组件的完整生命线：
  //     idle ──loading──> loading ──┬─成功─> success
  //                                 └─失败─> error
  // 状态是手动推进的——React 不会读心，你不说「我在等」，
  // 它就一直以为是 idle，那几百毫秒里页面什么都不显示。
  useEffect(() => {
    setStatus('loading')

    fetch(`${API_BASE}/api/health`)
      .then((res) => res.json())
      .then((data: HealthInfo) => {
        setInfo(data)
        setStatus('success')
      })
      .catch((err) => {
        // 原始错误只进控制台，绝不进页面文案。
        // 用户看不懂 TypeError，也不需要看懂；他们需要的是「下一步做什么」。
        // M3 接入 LLM 后这条规矩升级成安全红线：模型报错常夹带 API Key 片段。
        console.error('健康检查失败:', err)
        setStatus('error')
      })
  }, [])

  // ══════════════ 渲染 ══════════════
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-12 px-6 py-16">
      {/* ──────── 标题区 ──────── */}
      <header className="max-w-2xl text-center">
        <h1 className="bg-gradient-to-br from-ink to-ink-muted bg-clip-text text-display font-bold tracking-tight text-transparent">
          ModelForge
        </h1>
        <p className="mt-5 text-lead text-ink-secondary">
          模型工坊 · 人在环中的数学建模 Agent 工作台
        </p>
      </header>

      {/* ──────── 状态卡片 ──────── */}
      <section className="w-full max-w-lg rounded-card border border-hairline bg-surface p-10 shadow-sm">
        {/* ① idle —— 组件刚挂载，useEffect 还没跑，存在时间以毫秒计 */}
        {status === 'idle' && (
          <p className="text-center text-body text-ink-muted">准备中……</p>
        )}

        {/* ② loading —— 请求在路上。
            这个转圈是纯 CSS 画的（animate-spin 让边框不完整的圆环匀速旋转），
            零图片、零第三方组件。 */}
        {status === 'loading' && (
          <div className="flex items-center justify-center gap-4">
            <span className="h-6 w-6 shrink-0 animate-spin rounded-full border-[3px] border-grid border-t-brand-500" />
            <p className="text-lead font-medium text-ink">您的宝agent正在赶来……</p>
          </div>
        )}

        {/* ③ success —— 拿到数据了。
            注意 `&& info` 这个额外判断：info 的类型是 HealthInfo | null，
            TypeScript 要你先排除 null 才允许访问 .service。
            这不是啰嗦，是在编译期就拦住「数据还没到却去读它」这类崩溃。 */}
        {status === 'success' && info && (
          <div>
            <p className="flex items-center gap-3 text-lead font-semibold text-good-ink">
              <span className="h-3 w-3 shrink-0 rounded-full bg-good" />
              一切就绪 · 您的宝agent已就位
            </p>

            {/* 状态色永远配「圆点 + 文字」，不靠颜色单独传达信息——
                这对色觉障碍用户是必需的，也是我们配色校验的硬性结论。 */}
            <dl className="mt-8">
              {[
                { label: '服务', value: info.service },
                { label: '版本', value: info.version },
                { label: '当前模型', value: info.default_provider },
              ].map(({ label, value }) => (
                <div
                  key={label}
                  className="flex items-baseline justify-between gap-6 border-b border-hairline py-4 last:border-0"
                >
                  <dt className="text-body text-ink-secondary">{label}</dt>
                  <dd className="font-mono text-body font-medium capitalize text-ink">
                    {value}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        )}

        {/* ④ error —— 出事了。
            文案遵守定下的两条规矩：
              · 不承诺代码做不到的事（所以不写「正在为您检查」）
              · 给出下一步动作，而不是只陈述失败 */}
        {status === 'error' && (
          <div>
            <p className="flex items-center gap-3 text-lead font-semibold text-critical-ink">
              <span className="h-3 w-3 shrink-0 rounded-full bg-critical" />
              后端失联了
            </p>
            <p className="mt-4 text-body text-ink-secondary">
              检查一下 uvicorn 是不是没开？
            </p>
            <code className="mt-6 block overflow-x-auto rounded-control bg-plane px-5 py-4 text-caption leading-relaxed text-ink-secondary">
              .venv/Scripts/python -m uvicorn modelforge.main:app --reload --port 8000
            </code>
          </div>
        )}
      </section>

      {/* ──────── 页脚 ──────── */}
      <footer className="text-body text-ink-muted">
        <a
          href={`${API_BASE}/api/docs`}
          target="_blank"
          rel="noopener noreferrer"
          className="underline decoration-hairline underline-offset-4 transition-colors hover:text-ink-secondary"
        >
          查看 API 文档
        </a>
        <span className="mx-3">·</span>
        <span>M0 · 骨架验证</span>
      </footer>
    </main>
  )
}
