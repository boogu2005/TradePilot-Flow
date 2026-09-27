"""
仓位状态 & 退出模式 & 退出结果 — 状态机核心模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PositionState(str, Enum):
    """
    仓位状态机 — 围绕仓位生命周期设计，不围绕老师信号设计。

    PENDING_ENTRY → OPEN → TP1_FILLED → PARTIAL_TP → CLOSED

    OPEN:       100% 仓位，挂 SL + TP1（2%止盈30%）
    TP1_FILLED: TP1 已成交（平30%），SL 保本，剩余70%，等待 TP2（4%止盈50%）
    PARTIAL_TP: TP2 已成交，剩余 35% 由 Trailing 管理
    """
    PENDING_ENTRY = "pending_entry"    # 已下单，等待 OKX 成交
    OPEN = "open"                     # OKX 100% 仓位，挂 SL + TP1
    TP1_FILLED = "tp1_filled"         # TP1 已成交（平30%），SL 保本，等待 TP2
    PARTIAL_TP_DONE = "partial_tp"    # TP2 已成交，剩余仓位由 Trailing 管理
    CLOSED = "closed"                 # 已完全平仓


class ExitMode(str, Enum):
    """退出模式 — 决定哪些退出策略处于激活状态"""
    AUTO = "auto"                     # 机器人全权管理（默认）
    HYBRID = "hybrid"                 # 老师更新过 SL/TP → 暂停自动退出策略
    CLOSE_PENDING = "close_pending"   # ⚠️ 已废弃 — Close 直接调用 OKX，不再使用此模式


@dataclass
class ExitResult:
    """
    退出检查结果。
    should_exit=True 时由 ExitManager 统一执行，不回调其他 checker。
    """
    should_exit: bool = False
    reason: str = ""                  # 退出原因日志
    exit_type: str = ""               # stoploss / tp1 / trailing / roi / max_hold / teacher_close
    close_pct: float = 100.0          # 平仓比例
    exit_price: Optional[float] = None  # 可指定退出价

    # TP1 专用：是否同时设置保本损
    move_sl_to_breakeven: bool = False
