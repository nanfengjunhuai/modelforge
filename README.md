# ModelForge · 模型工坊

> 人在环中的数学建模 Agent 工作台。

输入一道数学建模赛题，多个 Agent 协作推进——拆题、选模、求解、验证、成文。
但**每个关键决策点它会停下来问你**：怎么拆题、选哪个模型、参数怎么定。你拍板，它执行，全程可见。

[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![Next.js](https://img.shields.io/badge/Next.js-16-black)](https://nextjs.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 为什么不是「一键出论文」

全自动方案（输入赛题 → 输出论文）实现上就是一条长 prompt 串起代码执行，
看起来震撼，但**没有解决 Agent 工程真正困难的部分**。

ModelForge 选择做难的那一半：

- **中断与恢复** — Agent 在决策点挂起，状态落盘；你拍板后从 checkpoint 重建上下文继续跑。
  服务重启不丢进度，支持多会话并行。
- **可观测** — 每一步的推理流、工具调用、代码执行结果都实时推给前端，不是黑盒。
- **安全执行** — LLM 生成的求解代码在**独立于后端的环境**中运行，带资源限制与超时。

## 架构

```
┌────────────────────────────────────────────────────┐
│  Next.js (TypeScript) —— 前端工作台                 │
│   · Agent 思考流（SSE）   · 候选方案选择卡片          │
│   · 交互式图表            · 产物面板（代码 / 报告）   │
└──────────────────┬─────────────────────────────────┘
                   │  SSE 事件流 / REST
┌──────────────────▼─────────────────────────────────┐
│  FastAPI —— 编排层                                  │
│   · 状态机：拆题 → 选模 → 求解 → 验证 → 成文         │
│   · HITL 中断 / 恢复      · 事件总线                │
│   · Provider 抽象层（DeepSeek / 通义 / Ollama / …）  │
└──────────────────┬─────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────┐
│  沙箱执行 + 产物存储                                 │
│   · 独立运行时执行 LLM 生成的求解代码                 │
│   · SQLite：会话 / checkpoint / 产物                │
└────────────────────────────────────────────────────┘
```

## 快速开始

> 正在开发中。以下命令针对 Python 3.12 + Node 22。

```bash
git clone https://github.com/nanfengjunhuai/modelforge.git
cd modelforge

# 后端
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -e ".[dev]"

# 前端
cd web && npm install && cd ..

# 配置模型（复制后填入你的 API Key）
cp .env.example .env
```

```bash
# 后端（在项目根目录，:8000）
.venv/Scripts/python -m uvicorn modelforge.main:app --reload --port 8000

# 前端（另一个终端，:3000）
cd web && npm run dev
```

然后打开 <http://localhost:3000/chat>。

验证流式输出是真的在流（而不是攒完一起发）：

```bash
.venv/Scripts/python scripts/smoke_stream.py "用三句话解释什么是熵权法"
```

## 开发路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 环境搭建、骨架跑通 | ✅ |
| — | 设计系统（UI 与图表共用一套色彩体系） | ✅ |
| M1 | Provider 抽象层 | ✅ |
| M2 | SSE 流式协议、流式对话界面 | ✅ |
| M3 | 工具调用、沙箱执行 | ⬅ 进行中 |
| M4 | HITL 中断 / 恢复、状态持久化 | |
| M5 | 完整工作台 UI、交互图表 | |
| M6 | 报告生成、部署、文档 | |

架构决策记录（每个决策的「为什么」）在 [`docs/roadmap.md`](docs/roadmap.md)。

## 许可证

MIT
