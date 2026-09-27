"""
TP2 检查器 — 第二止盈，仅运行一次，仅在 TP1_FILLED 状态下。
达到 profit_pct（4%）后平仓剩余仓位的 close_pct%（50%），激活 Trailing。

状态流: OPEN → TP1(2%平30%) → TP1_FILLED → TP2(4%平剩余50%) → PARTIAL_TP → Trailing
"""
from __future__ import annotations

from loguru import logger

from .base import BaseExitChecker
from .models import ExitResult, ExitMode
from core.protection_targets import teacher_tp_prices, tp_price_for, single_teacher_tp


class TP2Checker(BaseExitChecker):
    name = "tp2"

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        return position_state == "tp1_filled"

    async def check(self, trade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        cfg = self.config.get("tp2", {})
        if not cfg.get("enabled", True):
            return None
        teacher_prices = teacher_tp_prices(trade)
        teacher_price = tp_price_for(trade, 2) if teacher_prices else None
        if teacher_prices and teacher_price is None:
            return None

        # 防止在 partial_tp / closed 状态下再次触发 TP2
        pos_state = getattr(trade, 'position_state', None)
        if pos_state in ("partial_tp", "closed"):
            logger.debug(f"[TP2] {trade.pair} 跳过: position_state={pos_state} (TP2 已完成)")
            return None

        # Trailing 已激活 → TP2 已完成
        if getattr(trade, 'trailing_activated', False):
            logger.debug(f"[TP2] {trade.pair} 跳过: trailing_activated=True (Trailing 已激活，TP2 已完成)")
            return None

        current = current_price
        if current is None or current <= 0 or trade.open_rate <= 0:
            return None

        if trade.is_short:
            profit = (trade.open_rate - current) / trade.open_rate
        else:
            profit = (current - trade.open_rate) / trade.open_rate

        target = ((trade.open_rate - teacher_price) / trade.open_rate if trade.is_short
                  else (teacher_price - trade.open_rate) / trade.open_rate) if teacher_price else cfg.get("profit_pct", 0.06)
        reached = (current <= teacher_price if trade.is_short else current >= teacher_price) if teacher_price else profit >= target
        if reached:
            close_pct = 100 if single_teacher_tp(trade) else cfg.get("close_pct", 50)
            logger.info(
                f"[{exchange}] TP2 触发 {trade.pair} profit={profit*100:.2f}% "
                f"target={target*100:.1f}% 平仓剩余{close_pct}% 激活Trailing"
            )
            return ExitResult(
                should_exit=True,
                reason=f"TP2 ({target*100:.1f}%) 已到",
                exit_type="tp2",
                close_pct=close_pct,
                exit_price=current,
                move_sl_to_breakeven=True,  # 信号：激活 Trailing（SL 已在保本价）
            )

        return None
