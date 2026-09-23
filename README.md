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

## 沙箱：能挡什么，挡不住什么

模型生成的代码是不可信的，所以它跑在一个独立的 Python 环境里
（**不是**后端自己的 `.venv` —— 否则 `os.environ["DEEPSEEK_API_KEY"]` 一行就能把密钥带走）。

**挡得住**：读走后端密钥 · 误删项目文件 · 死循环卡死后端 · 输出撑爆上下文

**挡不住**：用绝对路径读任意文件 · 发网络请求 · 把 CPU 跑满（超时能杀，但杀之前那几秒是实打实占着的）

**这是「防误伤」级别，不是「防蓄意攻击」级别。** 对「用户自己跑自己模型的输出」
这个场景够用，但**如果要做成多用户在线服务，这套实现必须整个换成容器或微虚拟机**。

之所以把这段写在 README 而不是只写在代码里，是因为**知道边界的弱点是工程的一部分**；
把它藏起来才是问题。执行器本身是一个协议（`sandbox/base.py`），
换隔离方式不需要改上层任何代码。

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
# 沙箱环境（跑模型生成的代码用的，和后端环境隔离 —— 见 ADR-005）
.venv/Scripts/python scripts/setup_sandbox.py

# 后端（在项目根目录，:8000）
.venv/Scripts/python -m uvicorn modelforge.main:app --reload --port 8000

# 前端（另一个终端，:3000）
cd web && npm run dev
```

然后打开 <http://localhost:3000/chat>，问它一道要算的题，比如：

> 用 numpy 算 1 到 100 的平方和，再开平方根

你会看到它调用 `run_python`、代码在沙箱里真的跑起来、输出回填给它，
最后给出结论。整个过程在界面上是摊开的，不是黑盒。

命令行验证流式输出（而不是攒完一起发）：

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
| M3 | 工具调用、沙箱执行 | ✅ |
| M4 | HITL 中断 / 恢复、状态持久化 | ⬅ 进行中 |
| M5 | 完整工作台 UI、交互图表 | |
| M6 | 报告生成、部署、文档 | |

架构决策记录（每个决策的「为什么」）在 [`docs/roadmap.md`](docs/roadmap.md)。

## 许可证

MIT
