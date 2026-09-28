"""
TP1 检查器 — 第一止盈，仅运行一次，仅在 OPEN 状态下。
达到 profit_pct（2%）后平仓 close_pct%（30%），移动止损到保本价。
不激活 Trailing（Trailing 由 TP2 在 4% 时激活）。
"""
from __future__ import annotations

from loguru import logger

from .base import BaseExitChecker
from .models import ExitResult, ExitMode
from core.protection_targets import single_teacher_tp, teacher_tp_prices, tp_price_for


class TP1Checker(BaseExitChecker):
    name = "tp1"

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        return True  # ExitManager 按状态分派，OPEN 状态仅此 checker

    async def check(self, trade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        cfg = self.config.get("tp1", {})
        if not cfg.get("enabled", True):
            return None

        # 防止在 tp1_filled / partial_tp / closed 状态下再次触发 TP1
        pos_state = getattr(trade, 'position_state', None)
        if pos_state in ("tp1_filled", "partial_tp", "closed"):
            logger.debug(f"[TP1] {trade.pair} 跳过: position_state={pos_state} (TP1 已完成)")
            return None

        # Trailing 已激活 → TP1 已完成（盈利已超过 TP1 目标）
        if getattr(trade, 'trailing_activated', False):
            logger.debug(f"[TP1] {trade.pair} 跳过: trailing_activated=True (Trailing 已激活，TP1 已完成)")
            return None

        # 已设置保本损 → TP1 已完成
        if trade.stop_loss == trade.open_rate and trade.open_rate > 0 and not teacher_tp_prices(trade):
            return None

        current = current_price
        if current is None or current <= 0 or trade.open_rate <= 0:
            return None

        if trade.is_short:
            profit = (trade.open_rate - current) / trade.open_rate
        else:
            profit = (current - trade.open_rate) / trade.open_rate

        teacher_prices = teacher_tp_prices(trade)
        teacher_price = tp_price_for(trade, 1) if teacher_prices else None
        if teacher_prices and teacher_price is None:
            return None
        target = ((trade.open_rate - teacher_price) / trade.open_rate if trade.is_short
                  else (teacher_price - trade.open_rate) / trade.open_rate) if teacher_price else cfg.get("profit_pct", 0.03)
        reached = (current <= teacher_price if trade.is_short else current >= teacher_price) if teacher_price else profit >= target
        if reached:
            close_pct = 100 if single_teacher_tp(trade) else cfg.get("close_pct", 30)
            logger.info(f"[{exchange}] TP1 触发 {trade.pair} profit={profit*100:.2f}% target={target*100:.1f}%")
            return ExitResult(
                should_exit=True,
                reason=f"TP1 ({target*100:.1f}%) 已到",
                exit_type="tp1",
                close_pct=close_pct,
                exit_price=current,
                move_sl_to_breakeven=True,
            )

        return None
