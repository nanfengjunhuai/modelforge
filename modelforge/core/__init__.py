"""核心层：状态机、事件、checkpoint。

这是整个项目的心脏，也是 M1-M4 的主战场。规划中的模块：

    events.py       —— 事件类型定义（agent.thinking / tool.call / interrupt.request ...）
                       前后端共享的事件契约，先定协议再写实现。
    state.py        —— 会话状态的数据模型。因为选定了「状态落盘、点击时重建」，
                       这里的 schema 就是 checkpoint 的格式，必须最早定下来。
    machine.py      —— 状态机本体：拆题 → 选模 → 求解 → 验证 → 成文。
                       每个节点跑完即落盘，中断点则挂起等待用户决策。
    store.py        —— SQLite 持久化：会话 / checkpoint / 产物。
"""
