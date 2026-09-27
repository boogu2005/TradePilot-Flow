"""
动态锁利追踪止损检查器。
盈利达到 +6%（TP2 触发后）启动，按阶梯锁定利润，只向收紧方向移动。
锁利间隔 = 6%：每涨 1% SL 从开仓价上移 1%，即需要回调 6% 才平仓。

阶梯规则：
  profit=6% -> SL=0%（保本）
  profit=7% -> SL=1%
  profit=8% -> SL=2%
  profit=9% -> SL=3%
  profit=10% -> SL=4%
  公式: sl_profit_pct = max(0, highest_profit_pct - 0.06)

P0-1 状态持久化：
  使用数据库字段（非 signal_meta）存储 Trailing 状态：
  - trade.trailing_activated: 是否已激活
  - trade.trailing_highest_profit_pct: 最高盈利百分比
  - trade.trailing_highest_price: 最高价格
  - trade.trailing_current_sl: 当前 Trailing SL 价格
  - trade.trailing_last_sl_update_at: 最后 SL 更新时间

  每次状态变化立即 commit，确保程序重启后能恢复。
"""
from __future__ import annotations

from datetime import datetime, timezone
from loguru import logger

from .base import BaseExitChecker
from .models import ExitResult, ExitMode


class TrailingChecker(BaseExitChecker):
    name = "trailing"

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        return True  # ExitManager 按状态分派，PARTIAL_TP 状态仅此 checker

    async def check(
        self, trade, exchange: str, session,
        current_price: float | None = None,
    ) -> ExitResult | None:
        cfg = self.config.get("trailing", {})
        if not cfg.get("enabled", True):
            return None

        activate_pct = cfg.get("activate_profit_pct", 0.06)  # +6% 激活（TP2 触发后）

        current = current_price
        if current is None or current <= 0 or trade.open_rate <= 0:
            return None

        # ---- 计算当前盈利百分比 ----
        if trade.is_short:
            profit_pct = (trade.open_rate - current) / trade.open_rate
        else:
            profit_pct = (current - trade.open_rate) / trade.open_rate

        # ---- P0-1: 从数据库字段读取 Trailing 状态（非 signal_meta）----
        # 程序重启后，这些字段会从数据库恢复
        highest_profit_pct = trade.trailing_highest_profit_pct or 0.0

        # 未激活 且 未达到阈值 -> 跳过
        if not trade.trailing_activated:
            if profit_pct < activate_pct:
                return None
            # 首次激活：记录当前盈利并立即 commit
            highest_profit_pct = profit_pct
            trade.trailing_activated = True
            trade.trailing_highest_profit_pct = highest_profit_pct
            trade.trailing_highest_price = current
            trade.trailing_last_sl_update_at = datetime.now(timezone.utc)
            session.commit()  # P0-1: 立即持久化
            logger.info(
                f"[{exchange}] 追踪止损激活 {trade.pair} "
                f"profit={profit_pct*100:.2f}%"
            )
        else:
            # 已激活：更新历史最高盈利（如果有变化则 commit）
            if profit_pct > highest_profit_pct:
                highest_profit_pct = profit_pct
                trade.trailing_highest_profit_pct = highest_profit_pct
                trade.trailing_highest_price = current
                trade.trailing_last_sl_update_at = datetime.now(timezone.utc)
                session.commit()  # P0-1: 立即持久化

        # ---- 计算动态锁利止损价格 ----
        # sl_profit_pct = max(0, highest_profit_pct - 0.06)
        # 锁利间隔 6%：每涨 1%，SL 从开仓价上移 1%，回调 6% 才触发
        lock_gap = cfg.get("lock_gap_pct", 0.06)
        sl_profit_pct = max(0.0, highest_profit_pct - lock_gap)

        if trade.is_short:
            trailing_sl_price = trade.open_rate * (1 - sl_profit_pct)
        else:
            trailing_sl_price = trade.open_rate * (1 + sl_profit_pct)

        # ---- 检查止损是否被触发 ----
        if trade.is_short:
            # 空单：当前价反弹超过止损价 -> SL 被触发
            if trailing_sl_price < current:
                logger.info(
                    f"[{exchange}] 追踪止损触发 {trade.pair} "
                    f"最高盈利={highest_profit_pct*100:.2f}% "
                    f"当前盈利={profit_pct*100:.2f}% "
                    f"SL价格={trailing_sl_price:.4f} 当前价={current:.4f}"
                )
                return ExitResult(
                    should_exit=True,
                    reason=(
                        f"追踪止损(最高+{highest_profit_pct*100:.1f}% "
                        f"锁利{sl_profit_pct*100:.1f}%)"
                    ),
                    exit_type="trailing",
                    close_pct=100.0,
                    exit_price=current,
                )
        else:
            # 多单：当前价跌破止损价 -> SL 被触发
            if trailing_sl_price > current:
                logger.info(
                    f"[{exchange}] 追踪止损触发 {trade.pair} "
                    f"最高盈利={highest_profit_pct*100:.2f}% "
                    f"当前盈利={profit_pct*100:.2f}% "
                    f"SL价格={trailing_sl_price:.4f} 当前价={current:.4f}"
                )
                return ExitResult(
                    should_exit=True,
                    reason=(
                        f"追踪止损(最高+{highest_profit_pct*100:.1f}% "
                        f"锁利{sl_profit_pct*100:.1f}%)"
                    ),
                    exit_type="trailing",
                    close_pct=100.0,
                    exit_price=current,
                )

        # ---- 未触发：仅更新数据库 stop_loss，不碰交易所 SL ----
        # 交易所上的原始 SL（基于入场价）保留作为最终保障。
        # 仓位退出由本地 StopLossChecker 根据 trade.stop_loss 判断。
        # SL 硬限制：只允许向收紧方向移动
        #   多单 -> 只上移 (higher price = tighter)
        #   空单 -> 只下移 (lower price = tighter)
        old_sl = trade.stop_loss

        if trade.is_short:
            # 空单：SL 只能下移（更低 = 更紧）
            if old_sl == 0:
                new_sl = trailing_sl_price
            else:
                new_sl = min(old_sl, trailing_sl_price)  # 取更小值
            if new_sl < old_sl or old_sl == 0:
                trade.stop_loss = new_sl
                trade.trailing_current_sl = new_sl
                trade.trailing_last_sl_update_at = datetime.now(timezone.utc)
                session.commit()
        else:
            # 多单：SL 只能上移（更高 = 更紧）
            if old_sl == 0:
                new_sl = trailing_sl_price
            else:
                new_sl = max(old_sl, trailing_sl_price)  # 取更大值（硬限制）
            if new_sl > old_sl or old_sl == 0:
                trade.stop_loss = new_sl
                trade.trailing_current_sl = new_sl
                trade.trailing_last_sl_update_at = datetime.now(timezone.utc)
                session.commit()

        return None
