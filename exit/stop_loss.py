"""
默认止损检查器 — 固定百分比止损。
只在 AUTO 模式下且老师没有设置自定义止损时运行。
使用 ExitManager 传入的共享 current_price。
"""
from __future__ import annotations

from loguru import logger

from .base import BaseExitChecker
from .models import ExitResult, ExitMode
# 不再直接 import exchange — 使用传入的 current_price


class StopLossChecker(BaseExitChecker):
    name = "default_stoploss"

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        # 双保险：ExitManager 按状态分派，但保留状态保护防止绕过
        return position_state in ("open", "tp1_filled", "partial_tp")

    async def check(self, trade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        # 老师设置了自定义止损 → 跳过默认止损
        if trade.stop_loss <= 0:
            return None

        # 使用传入的 current_price（由 ExitManager 统一 fetch）
        if current_price is None or current_price <= 0:
            return None

        triggered = (
            (trade.is_short and current_price >= trade.stop_loss) or
            (not trade.is_short and current_price <= trade.stop_loss)
        )
        if triggered:
            # 正确归因：Trailing 激活时 SL 由 TrailingChecker 设定，标记为 trailing 退出
            is_trailing = (
                hasattr(trade, 'trailing_activated') and trade.trailing_activated
            )
            exit_type = "trailing" if is_trailing else "stoploss"
            label = "追踪止损" if is_trailing else "默认止损"
            logger.info(f"[{exchange}] {label}触发 {trade.pair} SL={trade.stop_loss} 当前={current_price}")
            return ExitResult(
                should_exit=True,
                reason=f"{label} {trade.stop_loss:.5g}",
                exit_type=exit_type,
                close_pct=100.0,
                exit_price=current_price,
            )
        return None
