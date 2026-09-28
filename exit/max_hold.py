"""
最大持仓时间检查器 — 超过设定小时数强制平仓。
在所有模式下都运行（最高优先级兜底）。
使用 ExitManager 传入的共享 current_price（可选）。
"""
from __future__ import annotations
from datetime import datetime, timezone

from utils.time import ensure_utc

from loguru import logger

from .base import BaseExitChecker
from .models import ExitResult, ExitMode


class MaxHoldChecker(BaseExitChecker):
    name = "max_hold"

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        return True  # 最高优先级，所有模式下都运行（包括 HYBRID）

    async def check(self, trade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        cfg = self.config.get("max_hold", {})
        if not cfg.get("enabled", True):
            return None

        # v6: ensure hours is int (JSON may preserve as int, env override handles conversion)
        try:
            max_hours = float(cfg.get("hours", 168))
        except (ValueError, TypeError):
            max_hours = 168.0

        # 时间基准：优先使用 OKX 确认的实际开仓时间，fallback 到 open_date
        effective_open = trade.effective_open_time
        if not effective_open:
            return None
        effective_open = ensure_utc(effective_open)

        now = datetime.now(timezone.utc)
        elapsed_hours = (now - effective_open).total_seconds() / 3600.0

        if elapsed_hours >= max_hours:
            logger.warning(f"[{exchange}] 最大持仓时间触发 {trade.pair} 已持{elapsed_hours:.1f}h>{max_hours:.0f}h")
            price = current_price or 0
            return ExitResult(
                should_exit=True,
                reason=f"最大持仓时间({elapsed_hours:.1f}h>{max_hours:.0f}h)",
                exit_type="max_hold",
                close_pct=100.0,
                exit_price=price,
            )

        return None
