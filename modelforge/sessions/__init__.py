"""会话状态：事件日志、投影、持久化。

    base.py           —— 七种日志事件的类型 + `SessionStore` / `TurnRecorder` 两个协议
    project.py        —— 纯函数：事件日志 → 对话历史 / 决策列表 / 状态
    sqlite_store.py   —— SQLite 实现 + 把协议落到具体会话的 recorder

分层的关系是单向的：

    api/  ──►  sessions/  ──►  providers/（事件与消息类型）
                     │
                     └──►  config.py

`agents/loop.py` 只依赖 `base.TurnRecorder` 那一个协议，不 import 本包里
任何具体实现 —— 所以换存储不用碰 Agent 循环。见 `base.py` 里那段说明。
"""
