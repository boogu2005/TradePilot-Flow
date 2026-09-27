"""
ROI checker — minimum-profit bailout for long-held positions.

Rules:
  0-48 hours    disabled (no exit)
  48-72 hours   if profit_pct < threshold (1%) -> exit
  72+ hours     if profit_pct < threshold (0%) -> exit

Config format: {minutes_from_open: min_profit_ratio, ...}
  e.g. {2880: 0.01, 4320: 0.00}
    2880 min (48h) -> minimum profit 1% — exit below this
    4320 min (72h) -> minimum profit 0% — exit below this (loss)
"""
from __future__ import annotations
from datetime import datetime, timezone

from utils.time import ensure_utc

from loguru import logger

from .base import BaseExitChecker
from .models import ExitResult, ExitMode


class ROIChecker(BaseExitChecker):
    name = "roi"

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        return True  # ExitManager 按状态分派，PARTIAL_TP 状态仅此 checker

    async def check(self, trade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        cfg = self.config.get("roi", {})
        if not cfg.get("enabled", True):
            return None

        if not trade.open_date:
            return None

        # 时间基准：优先使用 OKX 确认的实际开仓时间，fallback 到 open_date
        effective_open = trade.effective_open_time
        if effective_open:
            effective_open = ensure_utc(effective_open)

        now = datetime.now(timezone.utc)
        elapsed_hours = (now - effective_open).total_seconds() / 3600.0 if effective_open else 0

        # 0-48 hours: disabled
        if elapsed_hours < 48:
            return None

        current = current_price
        if current is None or current <= 0 or trade.open_rate <= 0:
            return None

        if trade.is_short:
            profit_pct = (trade.open_rate - current) / trade.open_rate
        else:
            profit_pct = (current - trade.open_rate) / trade.open_rate

        # Read rules from config: {minutes: min_profit_ratio}
        # v6: JSON keys are strings — convert to int for numeric comparison
        raw_rules = cfg.get("rules", {"2880": 0.01, "4320": 0.00})
        rules: dict[int, float] = {}
        for k, v in raw_rules.items():
            try:
                rules[int(k)] = float(v)
            except (ValueError, TypeError):
                logger.warning(f"[ROI] 跳过无效规则: {k}={v}")
        elapsed_minutes = elapsed_hours * 60.0

        # Find the applicable threshold: greatest minutes key <= elapsed_minutes
        threshold = None
        applicable_minutes = None
        for minutes in sorted(rules.keys()):
            if elapsed_minutes >= minutes:
                threshold = rules[minutes]
                applicable_minutes = minutes
            else:
                break

        if threshold is None:
            return None

        # Exit if profit is below the minimum threshold
        if profit_pct < threshold:
            logger.info(
                f"[{exchange}] ROI exit {trade.pair} "
                f"held={elapsed_hours:.1f}h profit={profit_pct*100:.2f}% "
                f"below threshold {threshold*100:.2f}% ",
            )
            return ExitResult(
                should_exit=True,
                reason=f"ROI(hold={elapsed_hours:.1f}h profit={profit_pct*100:.1f}%<{threshold*100:.1f}%)",
                exit_type="roi",
                close_pct=100.0,
                exit_price=current,
            )

        return None
