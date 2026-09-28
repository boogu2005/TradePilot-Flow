"""
最大亏损强制平仓检查器 — 浮亏达到阈值时强制平仓。

规则：
  - BTC / ETH / DOGE / SOL：浮亏 40% 强制平仓
  - 其他币种：浮亏 20% 强制平仓

此检查器作为最终安全网，在所有其他退出检查器之前运行。
当浮亏超过阈值时，立即市价全平，防止更大损失。
"""
from __future__ import annotations

from loguru import logger

from .base import BaseExitChecker
from .models import ExitResult, ExitMode

# 大盘币种 — 浮亏容忍度更高（40%）
_MAJOR_COINS = ("BTC", "ETH", "DOGE", "SOL")

# 默认浮亏阈值
_MAJOR_LOSS_PCT = 0.40   # 40%
_ALT_LOSS_PCT = 0.20     # 20%


def _get_loss_threshold(symbol: str) -> float:
    """
    根据币种返回浮亏阈值。

    匹配规则：检查 symbol 是否包含大盘币种代码前缀。
    如 BTCUSDT / BTC-USDT-SWAP / BTC/USDT:USDT → 匹配 BTC → 40%
    """
    sym_up = symbol.upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")
    for major in _MAJOR_COINS:
        if sym_up.startswith(major):
            return _MAJOR_LOSS_PCT
    return _ALT_LOSS_PCT


class MaxLossChecker(BaseExitChecker):
    """
    最大亏损检查器 — 浮亏超过阈值时强制平仓。

    在所有状态下运行（OPEN / PARTIAL_TP），作为最终安全网。
    """

    name = "max_loss"

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        # 在所有非关闭状态下运行
        return position_state in ("open", "tp1_filled", "partial_tp")

    async def check(self, trade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        if current_price is None or current_price <= 0 or trade.open_rate <= 0:
            return None

        # 计算当前浮亏比例
        if trade.is_short:
            loss_pct = (current_price - trade.open_rate) / trade.open_rate
        else:
            loss_pct = (trade.open_rate - current_price) / trade.open_rate

        # 浮亏为负值时表示盈利，跳过
        if loss_pct <= 0:
            return None

        # 获取该币种的浮亏阈值
        threshold = _get_loss_threshold(trade.pair)

        if loss_pct >= threshold:
            coin_type = "大盘" if threshold >= _MAJOR_LOSS_PCT else "山寨"
            logger.warning(
                f"[{exchange}] 最大亏损触发 {trade.pair} "
                f"浮亏={loss_pct*100:.2f}% 阈值={threshold*100:.0f}% "
                f"类型={coin_type} 当前价={current_price} 开仓价={trade.open_rate}"
            )
            return ExitResult(
                should_exit=True,
                reason=f"最大亏损 {loss_pct*100:.2f}%（阈值{threshold*100:.0f}%）",
                exit_type="max_loss",
                close_pct=100.0,
                exit_price=current_price,
            )

        return None
