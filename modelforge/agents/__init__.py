"""Agent 层：每个建模阶段一个 Agent。

规划中的分解（按数学建模的四类经典问题组织）：

    decompose.py  —— 拆题：读懂赛题，抽出已知/未知/目标/约束
    select.py     —— 选模：从候选模型中给方案 + 理由（评价 / 预测 / 优化 / 微分方程）
    solve.py      —— 求解：写代码交给沙箱跑
    verify.py     —— 验证：结果合理吗？灵敏度如何？
    write.py      —— 成文：生成论文级报告

注意：Agent 不直接调用模型，而是通过 ``modelforge.providers`` 拿到统一的流式接口。
"""
