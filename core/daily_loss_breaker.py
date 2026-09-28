"""
Daily Loss Breaker — equity-based daily max loss circuit breaker.

双通道权益校验（双重保障）:
  Channel 1 (WS):  BalanceTracker 每 5s 评估，数据来自 WebSocket 实时推送
  Channel 2 (REST): 每 60s 主动调用 OKX REST API 查询权益，绕过缓存

熔断判断: 任一通道检测到日亏损达到阈值 → 触发熔断
触发后: 只阻断新开仓，不强制平仓已有仓位

Reset: UTC 00:00 自动重置

核心原则:
  - 熔断判断只依赖 WS 推送和主动 REST 查询的权益数据
  - 绝不依赖数据库中的余额/权益字段做熔断判断
  - 数据库仅存储配置（max_loss_pct），不作为熔断决策的数据源

References:
  - DeepAlpha risk_manager.py — daily PnL reset pattern
  - Alpha Engine risk_controls.py — multi-tier drawdown breaker with guards
  - profitus_maximus — 23:55 UTC forced flattening (we do NOT force-close)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from loguru import logger

L = logger.bind(module="daily_loss_breaker")


def _get_balance_tracker():
    """Lazy import BalanceTracker singleton — overridable for testing."""
    from core.balance_tracker import balance_tracker
    return balance_tracker


class DailyLossBreaker:
    """
    Daily max loss circuit breaker — dual channel equity verification.

    Channel 1 — WS (每 5s):
        equity 来自 BalanceTracker (WebSocket 实时推送)
        evaluate() 同步读取，轻量无 IO

    Channel 2 — REST (每 60s):
        equity 来自 OKX REST API fetch_balance（绕过缓存）
        evaluate_rest() 异步执行，主动校验

    熔断逻辑:
        daily_loss = day_start_equity - current_equity
        tripped when daily_loss / day_start_equity >= max_loss_pct

    任一通道触发 → 设 _tripped = True → 当日禁止新开仓

    Key behaviors:
      - 含未实现盈亏（unrealized PnL）
      - 只阻断新开仓 — 绝不强制平仓
      - UTC 00:00 自动重置
      - 权益 ≤ 0 时跳过检查（数据异常守卫）
      - 绝不依赖数据库余额字段做熔断判断
    """

    def __init__(self, max_loss_pct: float = 0.10):
        self.max_loss_pct: float = max_loss_pct
        self._day_start_equity: float = 0.0
        self._current_date: str = ""
        self._tripped: bool = False
        self._trip_reason: str = ""
        self._trip_time: float = 0.0

        # —— Channel 2: REST 主动校验状态 ——
        self._last_rest_evaluation: float = 0.0
        self._rest_evaluation_interval: float = 60.0  # 每 60s 主动 REST
        self._rest_equity: float = 0.0                # 最近一次 REST 查询的权益
        self._rest_trip_source: str = ""              # "ws" | "rest" — 记录触发来源

    # ————————————————————————————————————————————————
    # Channel 1: WS 权益 (同步, 每 5s)
    # ————————————————————————————————————————————————

    @staticmethod
    def _current_equity() -> float:
        """Channel 1: 从 BalanceTracker (WebSocket) 获取当前账户权益。"""
        return _get_balance_tracker().equity

    # ————————————————————————————————————————————————
    # Channel 2: REST 权益 (异步, 每 60s, 绕过缓存)
    # ————————————————————————————————————————————————

    async def _fetch_equity_rest(self) -> float:
        """
        Channel 2: 主动通过 REST API 查询 OKX 账户权益。

        绕过 balance_cache，直接拿到最新的交易所数据。
        返回 -1.0 表示查询失败（由调用方守卫处理）。
        """
        from exchange_engine.exchange import fetch_balance, balance_cache

        # 先失效缓存，确保不会拿到过期数据
        balance_cache.invalidate("balance:okx")

        try:
            bal = await fetch_balance(exchange="okx")
        except Exception as e:
            L.warning(f"[DailyLossBreaker] REST fetch_balance 失败: {e}")
            return -1.0

        total = bal.get("total", 0) or 0
        unrealized = bal.get("unrealized_pnl", 0) or 0
        equity = total + unrealized

        L.debug(
            f"[DailyLossBreaker] REST 权益: total={total:.2f} "
            f"unrealized_pnl={unrealized:.2f} equity={equity:.2f}"
        )
        return equity

    async def evaluate_rest(self) -> tuple[bool, str]:
        """
        Channel 2: REST 主动校验 — 每 60s 查询一次 OKX 权益。

        独立于 WS 通道，作为双重保障：
          - 即使 WS 断连/数据滞后，REST 通道也能在 60s 内触发熔断
          - 与 WS 通道并行判断，任一触发即熔断

        Returns:
            (should_block, reason)
        """
        # 1. 日期边界检测
        self._check_day_reset()

        # 2. 已熔断则跳过
        if self._tripped:
            return True, self._trip_reason

        # 3. 频率控制：60s 内不重复查询
        now = time.time()
        if now - self._last_rest_evaluation < self._rest_evaluation_interval:
            return False, ""

        self._last_rest_evaluation = now

        # 4. REST 查询权益
        equity = await self._fetch_equity_rest()

        if equity <= 0:
            L.warning("[DailyLossBreaker] REST 权益查询失败或异常(≤0)，本次跳过，保留 WS 通道继续监控")
            return False, "REST权益查询异常，跳过"

        self._rest_equity = equity

        # 5. 初始化起始权益（REST 通道首次运行或 WS 尚未初始化时）
        if self._day_start_equity <= 0:
            self._day_start_equity = equity
            L.info(
                f"[DailyLossBreaker] REST 通道初始化起始权益: "
                f"{self._day_start_equity:.2f}U"
            )
            return False, "起始权益已记录(REST)"

        # 6. 计算日亏损
        daily_loss = self._day_start_equity - equity
        daily_loss_pct = daily_loss / self._day_start_equity

        # 7. 阈值判断 — REST 通道独立触发熔断
        if daily_loss_pct >= self.max_loss_pct:
            self._tripped = True
            self._trip_time = time.time()
            self._rest_trip_source = "rest"
            self._trip_reason = (
                f"单日熔断触发(REST主动校验): "
                f"起始权益={self._day_start_equity:.2f}U, "
                f"当前权益(REST)={equity:.2f}U, "
                f"亏损={daily_loss:.2f}U ({daily_loss_pct*100:.1f}%), "
                f"阈值={self.max_loss_pct*100:.0f}%"
            )
            L.warning(f"[DailyLossBreaker] ⛔ {self._trip_reason}")
            L.warning(
                f"[DailyLossBreaker] 今日暂停新开仓（REST通道触发），"
                f"已有仓位继续由ExitManager管理"
            )
            return True, self._trip_reason

        # 8. 交叉验证：REST vs WS 权益偏差告警
        ws_equity = self._current_equity()
        if ws_equity > 0 and equity > 0:
            diff_pct = abs(ws_equity - equity) / equity
            if diff_pct > 0.02:  # 超过 2% 偏差
                L.warning(
                    f"[DailyLossBreaker] ⚠️ WS/REST 权益偏差过大: "
                    f"WS={ws_equity:.2f}U REST={equity:.2f}U "
                    f"偏差={diff_pct*100:.1f}% — 请检查 WS 连接"
                )

        return False, ""

    def needs_rest_evaluation(self) -> bool:
        """Channel 2 是否需要执行 REST 主动校验。"""
        if self._tripped:
            return False
        return (time.time() - self._last_rest_evaluation) >= self._rest_evaluation_interval

    # ————————————————————————————————————————————————
    # 日期重置（两个通道共用）
    # ————————————————————————————————————————————————

    def _check_day_reset(self) -> None:
        """检测日期变化，自动重置熔断状态 + 捕获起始权益。"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            prev_date = self._current_date
            prev_tripped = self._tripped
            self._current_date = today
            self._tripped = False
            self._trip_reason = ""
            self._rest_trip_source = ""
            # 重置 REST 通道状态，新交易日立即执行首次 REST 校验
            self._last_rest_evaluation = 0.0
            self._rest_equity = 0.0

            equity = self._current_equity()
            if equity > 0:
                self._day_start_equity = equity
                L.info(
                    f"[DailyLossBreaker] 新交易日 {today}: "
                    f"起始权益={equity:.2f}U, "
                    f"昨日熔断={'是' if prev_tripped else '否'}"
                )
            elif prev_date:
                L.warning(
                    f"[DailyLossBreaker] {today}: WS 权益数据不可用(equity={equity}), "
                    f"前日起始权益={self._day_start_equity:.2f}U 已失效, "
                    f"清零等待 WS 恢复或 REST 通道重新初始化"
                )
                self._day_start_equity = 0.0

    # ————————————————————————————————————————————————
    # Channel 1: evaluate (WS 同步, 每 5s)
    # ————————————————————————————————————————————————

    def evaluate(self) -> tuple[bool, str]:
        """
        Channel 1: WS 通道评估 — 基于 BalanceTracker (WebSocket) 权益。

        每 5s 由 order_monitor 调用，同步无 IO。

        Returns:
            (should_block, reason)
            - should_block=True  → block new position opening
            - reason             → human-readable reason string
        """
        # 1. Check for day boundary reset
        self._check_day_reset()

        # 2. If already tripped today, keep blocking
        if self._tripped:
            return True, self._trip_reason

        # 3. Guard: skip when equity data is invalid
        current_equity = self._current_equity()

        if current_equity <= 0:
            return False, "权益数据异常(≤0)，跳过熔断检查"

        # 4. Guard: initialize start equity on first run
        if self._day_start_equity <= 0:
            self._day_start_equity = current_equity
            L.info(
                f"[DailyLossBreaker] WS 通道初始化起始权益: "
                f"{self._day_start_equity:.2f}U"
            )
            return False, "起始权益已记录"

        # 5. Calculate daily loss (equity decline from start of day)
        daily_loss = self._day_start_equity - current_equity
        daily_loss_pct = daily_loss / self._day_start_equity

        # 6. Check against threshold
        if daily_loss_pct >= self.max_loss_pct:
            self._tripped = True
            self._trip_time = time.time()
            self._rest_trip_source = "ws"
            self._trip_reason = (
                f"单日熔断触发(WS): "
                f"起始权益={self._day_start_equity:.2f}U, "
                f"当前权益={current_equity:.2f}U, "
                f"亏损={daily_loss:.2f}U ({daily_loss_pct*100:.1f}%), "
                f"阈值={self.max_loss_pct*100:.0f}%"
            )
            L.warning(f"[DailyLossBreaker] ⛔ {self._trip_reason}")
            L.warning(
                f"[DailyLossBreaker] 今日暂停新开仓（WS通道触发），"
                f"已有仓位继续由ExitManager管理"
            )
            return True, self._trip_reason

        return False, ""

    # ————————————————————————————————————————————————
    # Status & management
    # ————————————————————————————————————————————————

    def status(self) -> dict:
        """Return current breaker status snapshot (both channels)."""
        self._check_day_reset()
        current_equity = self._current_equity()

        # 用更可靠的权益值做展示: REST > WS
        display_equity = (
            self._rest_equity if self._rest_equity > 0 else current_equity
        )
        daily_loss = (
            max(0.0, self._day_start_equity - display_equity)
            if self._day_start_equity > 0
            else 0.0
        )
        daily_loss_pct = (
            round(daily_loss / self._day_start_equity * 100, 2)
            if self._day_start_equity > 0
            else 0.0
        )

        return {
            "tripped": self._tripped,
            "trip_source": self._rest_trip_source,
            "day_start_equity": round(self._day_start_equity, 2),
            "current_equity_ws": round(current_equity, 2),
            "current_equity_rest": round(self._rest_equity, 2),
            "daily_loss": round(daily_loss, 2),
            "daily_loss_pct": daily_loss_pct,
            "max_loss_pct": round(self.max_loss_pct * 100, 1),
            "trip_reason": self._trip_reason,
            "current_date": self._current_date,
            "last_rest_evaluation_ago": (
                round(time.time() - self._last_rest_evaluation, 1)
                if self._last_rest_evaluation > 0
                else None
            ),
            "rest_interval_s": self._rest_evaluation_interval,
        }

    def force_reset(self) -> None:
        """
        Force-reset the breaker for the current day.

        Use case: manual override after verifying the trip was a false positive
        or after resolving the issue that caused the trip.
        """
        self._tripped = False
        self._trip_reason = ""
        self._rest_trip_source = ""
        self._day_start_equity = self._current_equity()
        # 重置 REST 通道，下次 evaluate_rest() 立即执行
        self._last_rest_evaluation = 0.0
        self._rest_equity = 0.0
        L.info(
            f"[DailyLossBreaker] 手动重置: "
            f"新起始权益={self._day_start_equity:.2f}U"
        )

    @property
    def is_tripped(self) -> bool:
        """Check if breaker is currently tripped (without side effects)."""
        self._check_day_reset()
        return self._tripped


# Global singleton
daily_loss_breaker = DailyLossBreaker()
