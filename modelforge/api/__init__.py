"""HTTP 接口层。

每个模块暴露一个 ``router``，由 ``modelforge.main`` 统一挂载到 ``/api`` 前缀下。

规划中的路由：
    health.py    —— 系统健康检查（已有）
    sessions.py  —— 会话的创建 / 查询 / 恢复（M2）
    stream.py    —— SSE 事件流（M2）
    decisions.py —— HITL 决策点：提交用户的选择（M4）
"""
