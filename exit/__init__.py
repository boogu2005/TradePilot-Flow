"""
退出管理器 — 统一仓位退出决策。
所有退出逻辑通过 ExitManager 链式调用，按优先级只有一个模块能返回退出信号。

状态机: PENDING_ENTRY → OPEN → TP1_FILLED → PARTIAL_TP → CLOSED
  OPEN:       TP1 (2%平30%) + StopLoss + ROI + MaxHold
  TP1_FILLED: TP2 (4%平剩余50%) + StopLoss + ROI + MaxHold
  PARTIAL_TP: Trailing + StopLoss + ROI + MaxHold
"""
