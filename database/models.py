"""
Trade & Order 数据模型 — 直接复刻 Freqtrade persistence/trade_model.py 的结构。
Freqtrade 的生产级设计：Trade 管理完整生命周期，Order 追踪每一笔订单。

v2 数据库重构：
- 新增 TelegramMessage：所有 Telegram 消息存档
- 新增 ReplyMapping：Reply → Signal → Trade 直接映射
- 新增 UpdateHistory：老师修改 SL/TP 历史
- 新增 CloseHistory：每次平仓记录
- 新增 RecoveryLog：协调器恢复 Trade 记录
- 新增 ReconcilerLog：协调器操作审计
- 新增 Notification：机器人通知
- 新增 ErrorLog：异常日志
- 新增 DailyStatistics：每日统计
- 新增 AuditLog：数据库修改审计
- 新增 AiQueryCache：AI 查询缓存
- SignalLog 扩展 tg_msg_id, reply_to_msg_id, chat_id, teacher, signal_status
- Trade 扩展 telegram_message_id, reply_message_id, teacher
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isclose
from typing import ClassVar, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, JSON,
    UniqueConstraint, Enum, func, select, Date,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

MATH_CLOSE_PREC = 1e-8
NON_OPEN_EXCHANGE_STATES = {"canceled", "cancelled", "closed", "expired", "rejected"}
CANCELED_EXCHANGE_STATES = {"canceled", "cancelled"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _today() -> date:
    return date.today()


class Base(DeclarativeBase):
    pass


class Order(Base):
    """
    订单模型 — 复刻 Freqtrade Order。
    一个 Trade 可以有多个 Order（入场、加仓、止盈、止损）。
    """
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("ft_pair", "order_id", name="uq_order_pair_order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ft_trade_id: Mapped[int] = mapped_column(Integer, ForeignKey("trades.id"), index=True)

    ft_order_side: Mapped[str] = mapped_column(String(25), nullable=False)  # buy/sell/stoploss
    ft_pair: Mapped[str] = mapped_column(String(25), nullable=False)
    ft_is_open: Mapped[bool] = mapped_column(default=True, index=True)
    ft_amount: Mapped[float] = mapped_column(Float(), nullable=False)
    ft_price: Mapped[float] = mapped_column(Float(), nullable=False)
    ft_cancel_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    order_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    order_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    side: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    average: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    filled: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    remaining: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    cost: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    stop_price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    order_date: Mapped[datetime] = mapped_column(nullable=True, default=_utc_now)
    order_filled_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    order_update_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    ft_fee_base: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    ft_order_tag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ft_order_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # entry/stoploss/tp/trailing_stop/roi_exit/manual_close
    ft_order_pnl: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """OKX 平仓订单返回的已实现盈亏（info.pnl）。交易所计算的真实金额，含手续费影响。"""
    expire_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    trade = relationship("Trade", back_populates="orders")

    @property
    def safe_price(self) -> float:
        return self.average or self.price or self.stop_price or self.ft_price

    @property
    def safe_filled(self) -> float:
        return self.filled if self.filled is not None else 0.0

    @property
    def safe_remaining(self) -> float:
        return self.remaining if self.remaining is not None else self.ft_amount - self.safe_filled

    @property
    def safe_amount_after_fee(self) -> float:
        return self.safe_filled - (self.ft_fee_base or 0.0)

    @property
    def stake_amount(self) -> float:
        return float(self.safe_amount_after_fee * self.safe_price / (self.trade.leverage or 1.0))

    def update_from_ccxt(self, order: dict):
        """从 CCXT 订单对象更新状态 — 复刻 Freqtrade Order.update_from_ccxt_object()"""
        if self.order_id != str(order.get("id", "")):
            return
        self.status = order.get("status", self.status)
        self.symbol = order.get("symbol", self.symbol)
        self.order_type = order.get("type", self.order_type)
        self.side = order.get("side", self.side)
        self.price = order.get("price", self.price)
        self.amount = order.get("amount", self.amount)
        self.filled = order.get("filled", self.filled)
        self.average = order.get("average", self.average)
        self.remaining = order.get("remaining", self.remaining)
        self.cost = order.get("cost", self.cost)
        self.stop_price = order.get("stopPrice", self.stop_price)

        # 从 OKX 订单响应中提取交易所计算的真实盈亏
        # OKX API v5 在成交后的订单数据中包含 info.pnl 字段
        try:
            order_info = order.get("info", {})
            if isinstance(order_info, dict):
                raw_pnl = order_info.get("pnl")
                if raw_pnl is not None:
                    self.ft_order_pnl = float(raw_pnl)
        except (ValueError, TypeError):
            pass

        if self.status in NON_OPEN_EXCHANGE_STATES:
            self.ft_is_open = False
            if self.safe_filled > 0 and not self.order_filled_date:
                ts = order.get("lastTradeTimestamp") or order.get("timestamp")
                if ts:
                    from datetime import datetime as dt
                    self.order_filled_date = dt.fromtimestamp(ts / 1000, tz=timezone.utc)

    @staticmethod
    def parse_from_ccxt(order: dict, pair: str, side: str) -> Order:
        """从 CCXT 订单创建 Order 对象 — 复刻 Freqtrade Order.parse_from_ccxt_object()"""
        o = Order(
            order_id=str(order["id"]),
            ft_order_side=side,
            ft_pair=pair,
            ft_amount=order.get("amount") or 0.0,
            ft_price=order.get("price") or 0.0,
        )
        o.update_from_ccxt(order)
        return o


class Trade(Base):
    """
    交易模型 — 复刻 Freqtrade Trade。
    管理完整的交易生命周期：入场 → 持仓 → 平仓。

    v2 新增字段:
    - telegram_message_id: Trade 来源的 Telegram 消息 ID
    - reply_message_id: 老师对此 Trade 的最后回复消息 ID
    - teacher: 老师名称
    - margin: 保证金
    - quantity: 合约数量（amount 的别名）
    """
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    orders: Mapped[list[Order]] = relationship("Order", order_by="Order.id",
                                                 cascade="all, delete-orphan", lazy="selectin")

    exchange: Mapped[str] = mapped_column(String(25), nullable=False, default="okx")
    pair: Mapped[str] = mapped_column(String(25), nullable=False, index=True)
    base_currency: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    stake_currency: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    is_open: Mapped[bool] = mapped_column(default=True, index=True)
    fee_open: Mapped[float] = mapped_column(Float(), default=0.0)
    fee_open_cost: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    fee_open_currency: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    fee_close: Mapped[float] = mapped_column(Float(), default=0.0)
    fee_close_cost: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    fee_close_currency: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    open_rate: Mapped[float] = mapped_column(Float(), default=0.0)
    open_rate_requested: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    close_rate: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    close_rate_requested: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    realized_profit: Mapped[float] = mapped_column(Float(), default=0.0)
    close_profit: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    close_profit_abs: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    okx_pnl_ratio: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """OKX 仓位历史里的 pnlRatio（净盈亏/保证金），收益率的唯一真相源；平仓后由回填任务写入。"""
    stake_amount: Mapped[float] = mapped_column(Float(), default=0.0)
    max_stake_amount: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    amount: Mapped[float] = mapped_column(Float(), default=0.0)
    amount_requested: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    close_contracts: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """平仓时的合约数量。amount 在 close() 中被清零，
       此字段保留平仓瞬间的合约数，用于交叉验证盈亏计算。"""
    open_date: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)
    opened_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    close_date: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # 止损追踪 — Freqtrade 核心设计
    stop_loss: Mapped[float] = mapped_column(Float(), default=0.0)
    stop_loss_pct: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    initial_stop_loss: Mapped[Optional[float]] = mapped_column(Float(), default=0.0)
    initial_stop_loss_pct: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    is_stop_loss_trailing: Mapped[bool] = mapped_column(default=False)
    max_rate: Mapped[Optional[float]] = mapped_column(Float(), default=0.0)
    min_rate: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    close_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_chat_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    source_group_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exit_order_status: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    strategy: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    enter_tag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    timeframe: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 合约 & 杠杆
    is_short: Mapped[bool] = mapped_column(default=False)
    leverage: Mapped[float] = mapped_column(Float(), default=1.0)
    liquidation_price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    trading_mode: Mapped[str] = mapped_column(String(25), default="futures")
    amount_precision: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    price_precision: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    precision_mode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    contract_size: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    funding_fees: Mapped[Optional[float]] = mapped_column(Float(), default=0.0)
    signal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # 额外的信号元数据
    signal_meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ———— v2 新增字段 ————
    telegram_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """Trade 来源的原始 Telegram 消息 ID。用于 Reply 直接定位。"""

    reply_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """老师对此 Trade 的最后回复消息 ID。用于追溯最新回复。"""

    teacher: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    """老师名称。来自 Telegram 消息的 sender_name。"""

    margin: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """保证金。= stake_amount（为兼容性添加的别名）。"""

    quantity: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """合约数量。amount 的别名，便于理解。"""

    # ———— 状态机字段 ————
    exit_mode: Mapped[str] = mapped_column(String(25), nullable=False, default="auto")
    position_state: Mapped[str] = mapped_column(String(25), nullable=False, default="pending_entry")

    # ———— TP1 追踪字段 ————
    tp1_price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    tp2_price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    tp3_price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    tp1_filled_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    high_water_mark: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)

    # ———— Trailing 状态持久化字段 ————
    trailing_activated: Mapped[bool] = mapped_column(default=False)
    trailing_highest_profit_pct: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    trailing_highest_price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    trailing_current_sl: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    trailing_last_sl_update_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # ———— 退出类型 ————
    exit_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)

    # ———— Protection State Machine (v3, DEPRECATED in v5) ————
    # v5: OKX REST is the ONLY source of truth for "does SL/TP exist?"
    # Database NEVER stores actual protection state — only desired state (target prices).
    # These fields are kept for backward compatibility only.
    protection_state: Mapped[str] = mapped_column(String(25), nullable=False, default="NORMAL", index=True)
    """DEPRECATED v5: No longer used for SL/TP lifecycle tracking."""

    # ———— Algo ID Storage (v3, REFERENCE ONLY in v5) ————
    # v5: These are reference IDs only — they do NOT indicate that SL/TP "exists".
    # OKX REST API is the sole determinant of existence.
    sl_algo_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """Reference: OKX algoId for SL (for tracking, NOT existence proof)."""

    tp1_algo_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """Reference: OKX algoId for TP1."""

    tp2_algo_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    tp3_algo_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # ———— Repair Control (v3, SIMPLIFIED in v5) ————
    last_repair_time: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    """Last time a repair was attempted. Used for cooldown (min 60s)."""

    repair_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Number of repair retries. Max 3, then manual intervention required."""

    repair_lock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Mutex: only one repair process per trade at any time."""

    verify_after: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    """DEPRECATED v5: No longer used. Verification is REST-based on-demand."""

    exchange_ack_time: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    """DEPRECATED v5: No longer used."""

    # ———— Freqtrade 风格的属性 ————

    @property
    def effective_open_time(self) -> datetime:
        return self.opened_at or self.open_date

    @property
    def entry_side(self) -> str:
        return "sell" if self.is_short else "buy"

    @property
    def exit_side(self) -> str:
        return "buy" if self.is_short else "sell"

    @property
    def open_orders(self) -> list[Order]:
        if self.orders is None:
            return []
        return [o for o in self.orders if o.ft_is_open and o.ft_order_side != "stoploss"]

    @property
    def open_sl_orders(self) -> list[Order]:
        if self.orders is None:
            return []
        return [o for o in self.orders if o.ft_order_side == "stoploss" and o.ft_is_open]

    @property
    def has_open_orders(self) -> bool:
        if self.orders is None:
            return False
        return bool([o for o in self.orders if o.ft_order_side != "stoploss" and o.ft_is_open])

    @property
    def has_open_position(self) -> bool:
        return self.amount > 0

    @property
    def safe_close_rate(self) -> float:
        return self.close_rate or self.close_rate_requested or 0.0

    @property
    def nr_of_successful_entries(self) -> int:
        if self.orders is None:
            return 0
        filled = [o for o in self.orders
                  if o.ft_order_side == self.entry_side
                  and not o.ft_is_open
                  and o.filled
                  and o.status in NON_OPEN_EXCHANGE_STATES]
        return len(filled)

    def adjust_stop_loss(self, current_price: float, stoploss_pct: float, initial: bool = False):
        if initial and self.stop_loss != 0:
            return
        if self.is_short:
            new_loss = float(current_price * (1 + abs(stoploss_pct)))
        else:
            new_loss = float(current_price * (1 - abs(stoploss_pct)))
        if self.initial_stop_loss_pct is None:
            self.stop_loss = new_loss
            self.initial_stop_loss = new_loss
            self.stop_loss_pct = -abs(stoploss_pct)
            self.initial_stop_loss_pct = -abs(stoploss_pct)
            return
        higher = new_loss > self.stop_loss
        lower = new_loss < self.stop_loss
        if (higher and not self.is_short) or (lower and self.is_short):
            self.is_stop_loss_trailing = True
            self.stop_loss = new_loss
            self.stop_loss_pct = -abs(stoploss_pct)

    def calc_profit_ratio(self, rate: float) -> float:
        if self.open_rate == 0:
            return 0.0
        close_val = rate * self.amount
        open_val = self.open_rate * self.amount
        if self.is_short:
            return float((open_val - close_val) / open_val)
        else:
            return float((close_val - open_val) / open_val)

    def update_order(self, order: dict):
        if self.orders is None:
            return
        for o in self.orders:
            if o.order_id == order.get("id"):
                o.update_from_ccxt(order)
                break

    def select_order_by_id(self, order_id: str) -> Order | None:
        if self.orders is None:
            return None
        for o in self.orders:
            if o.order_id == order_id:
                return o
        return None

    def update_trade(self, order: Order) -> None:
        if order.status == "open" or order.safe_price is None:
            return
        if order.ft_order_side == self.entry_side:
            self.open_rate = order.safe_price
            self.amount = order.safe_amount_after_fee
            self.recalc_trade_from_orders()
            if self.amount > 0 and not self.is_open:
                self.is_open = True
        elif order.ft_order_side == self.exit_side:
            if self.is_open:
                self.recalc_trade_from_orders()
        elif order.ft_order_side == "stoploss" and order.status not in ("open",):
            self.close_rate_requested = self.stop_loss
            self.exit_reason = "stoploss_on_exchange"
        if order.ft_order_side != self.entry_side:
            if isclose(order.safe_amount_after_fee, self.amount, abs_tol=MATH_CLOSE_PREC) or \
               order.safe_amount_after_fee >= self.amount:
                # 优先使用 OKX 订单中返回的真实盈亏（ft_order_pnl 在 update_from_ccxt 中提取）
                self.close(order.safe_price, okx_realized_pnl=order.ft_order_pnl)

    def close(self, rate: float, okx_realized_pnl: float | None = None) -> None:
        """
        平仓并记录盈亏。

        Args:
            rate: 平仓成交价
            okx_realized_pnl: OKX 返回的真实已实现盈亏（USDT）。如果提供，直接使用 OKX 的
                             计算结果作为唯一真相源，不再用公式推算。
                             参考: OKX API v5 平仓订单响应中的 info.pnl 字段。
        """
        self.close_rate = rate
        self.close_date = _utc_now()

        if okx_realized_pnl is not None:
            # ✅ OKX 真相源：直接使用交易所返回的真实盈亏
            self.close_profit_abs = okx_realized_pnl
            self.realized_profit = okx_realized_pnl
            # 反算 close_profit 比率（用于统计显示）
            if self.stake_amount > 0 and (self.leverage or 1.0) > 0:
                self.close_profit = okx_realized_pnl / (self.stake_amount * (self.leverage or 1.0))
            else:
                self.close_profit = 0.0
        elif self.open_rate > 0 and self.stake_amount > 0:
            # ⚠️ 回退方案：无 OKX 数据时用公式估算
            # 注意：此路径为近似值，无法包含手续费和滑点的影响
            if self.is_short:
                profit_ratio = (self.open_rate - rate) / self.open_rate
            else:
                profit_ratio = (rate - self.open_rate) / self.open_rate
            # close_profit_abs = profit_ratio * position_value
            # position_value = stake_amount * leverage
            self.close_profit_abs = profit_ratio * self.stake_amount * (self.leverage or 1.0)
            self.close_profit = profit_ratio
            self.realized_profit = self.close_profit_abs

        self.is_open = False
        # 保留平仓时的合约数量，在 recalc 前保存（recalc 会修改 amount）
        self.close_contracts = self.amount
        self.amount = 0.0
        self.exit_order_status = "closed"
        # 确保状态机到达最终态（所有平仓路径的统一保证）
        self.position_state = "closed"
        # 统一记录 CloseHistory（任何平仓路径都不会遗漏）
        self._record_close_history()
        # recalc_trade_from_orders 会覆盖以上值（如果 order fill 数据可用）
        self.recalc_trade_from_orders(is_closing=True)

    def _record_close_history(self) -> None:
        """在 close() 内部调用，确保所有平仓路径都写入 CloseHistory。"""
        try:
            from database.models import CloseHistory  # noqa: F811 - deferred import
            close_type_map = {
                "stoploss_on_exchange": "sl", "signal_close": "teacher",
                "signal": "teacher", "emergency_exit": "emergency",
                "reconciliation_removed": "reconciliation", "okx_no_position": "reconciliation",
                "entry_expired": "expired", "entry_cancelled": "cancelled",
                "manual_close": "manual", "duplicate_cleanup": "reconciliation",
                "duplicate_trade_cleanup": "reconciliation",
            }
            ch_type = close_type_map.get(self.exit_reason or "", self.exit_reason or "manual")
            ch = CloseHistory(
                trade_id=self.id,
                close_type=ch_type,
                price=self.close_rate or 0,
                pnl=self.close_profit_abs or self.realized_profit or 0,
                exit_reason=self.exit_reason or "",
            )
            # CloseHistory 需要 session — 通过 object_session 获取
            from sqlalchemy.orm import object_session
            sess = object_session(self)
            if sess is not None:
                sess.add(ch)
                sess.flush()
        except Exception:
            pass  # 非致命：不影响平仓主流程

    def recalc_trade_from_orders(self, is_closing: bool = False):
        if self.orders is None:
            return
        current_amount = 0.0
        current_stake = 0.0
        total_stake = 0.0
        avg_price = 0.0
        close_profit_abs = 0.0
        max_stake = 0.0
        for o in self.orders:
            if o.ft_is_open or not o.filled:
                continue
            tmp_amt = o.safe_amount_after_fee
            tmp_price = o.safe_price
            is_exit = o.ft_order_side != self.entry_side
            if tmp_amt > 0 and tmp_price > 0:
                if is_exit:
                    current_amount -= tmp_amt
                    current_stake -= tmp_price * tmp_amt
                    prof = self.calc_profit_ratio(tmp_price) * o.stake_amount * (self.leverage or 1.0)
                    close_profit_abs += prof
                else:
                    current_amount += tmp_amt
                    current_stake += tmp_price * tmp_amt
                    total_stake += tmp_price * tmp_amt
                    max_stake += tmp_price * tmp_amt
                if current_amount > 0 and not is_exit:
                    avg_price = current_stake / current_amount
        if current_amount > 0:
            self.open_rate = avg_price
            self.amount = current_amount
            self.stake_amount = current_stake / (self.leverage or 1.0)
            self.max_stake_amount = max_stake / (self.leverage or 1.0)
        elif is_closing and total_stake > 0:
            self.close_profit_abs = close_profit_abs
            self.close_profit = close_profit_abs / total_stake if total_stake > 0 else 0.0
            self.realized_profit = close_profit_abs

    # ———— 数据库操作 ————

    @staticmethod
    def get_open_trades(session: Session) -> list[Trade]:
        return session.execute(
            select(Trade).where(Trade.is_open.is_(True))
        ).scalars().all()

    @staticmethod
    def get_active_trades(session: Session) -> list[Trade]:
        return session.execute(
            select(Trade).where(
                (Trade.is_open.is_(True)) |
                ((Trade.is_open.is_(False)) & (Trade.amount == 0) & (Trade.close_date.is_(None)))
            )
        ).scalars().all()

    @staticmethod
    def get_open_trade_count(session: Session) -> int:
        return session.scalar(
            select(func.count(Trade.id)).where(
                (Trade.is_open.is_(True)) |
                ((Trade.is_open.is_(False)) & (Trade.amount == 0) & (Trade.close_date.is_(None)))
            )
        ) or 0

    @staticmethod
    def find_by_telegram_message_id(session: Session, msg_id: int) -> Optional["Trade"]:
        """通过 Telegram 消息 ID 查找 Trade（O(1) 索引查询）"""
        return session.execute(
            select(Trade).where(Trade.telegram_message_id == msg_id)
        ).scalar_one_or_none()

    @staticmethod
    def find_by_signal_id(session: Session, signal_id: str) -> Optional["Trade"]:
        """通过 Signal ID 查找 Trade"""
        return session.execute(
            select(Trade).where(Trade.signal_id == signal_id)
        ).scalar_one_or_none()


class SignalLog(Base):
    """
    信号处理日志 — 追踪每条 Telegram 消息的解析结果。

    v2 新增字段:
    - tg_msg_id: Telegram 消息 ID
    - reply_to_msg_id: 回复的原始消息 ID
    - chat_id: 群组 ID
    - teacher: 老师名称
    - signal_status: 信号状态 (received/parsed/executed/ignored/failed)
    """
    __tablename__ = "signal_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True, default=_uid)
    tg_group_id: Mapped[Optional[str]] = mapped_column(String(32))
    tg_group_title: Mapped[Optional[str]] = mapped_column(String(255))
    tg_sender_name: Mapped[Optional[str]] = mapped_column(String(255))
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    direction: Mapped[Optional[str]] = mapped_column(String(8))
    pair: Mapped[Optional[str]] = mapped_column(String(32))
    parsed_json: Mapped[Optional[dict]] = mapped_column(JSON)
    is_trading_signal: Mapped[bool] = mapped_column(default=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_utc_now)

    # ———— v2 新增字段 ————
    tg_msg_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """Telegram 消息 ID。可以按此字段直接查询原始消息。"""

    reply_to_msg_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """回复的原始消息 ID。用于追溯回复链。"""

    chat_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    """群组 ID（tg_group_id 的别名）。"""

    teacher: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    """老师名称。"""

    signal_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, default="received")
    """信号状态: received/parsed/executed/ignored/failed"""


# ═══════════════════════════════════════════════════════════════════════════
# v2 新增表
# ═══════════════════════════════════════════════════════════════════════════


class TelegramMessage(Base):
    """
    所有 Telegram 消息的持久化存档。

    每一条从目标群收到的消息都会写入此表。
    用于：消息回溯、回复链追踪、老师行为分析。
    """
    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_message_id", name="uq_tg_msg_chat_msg_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    """Telegram 消息 ID。"""

    reply_to_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """回复的原始消息 ID（如果这条消息是回复）。"""

    chat_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """群组 ID。"""

    chat_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """群组名称。"""

    sender_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    """发送者 ID。"""

    sender_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    """发送者名称。"""

    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """消息文本（可能被截断）。"""

    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """原始消息文本（完整）。"""

    message_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, default="chat")
    """消息类型: signal/update/close/chat/system。"""

    is_edited: Mapped[bool] = mapped_column(default=False)
    """是否被编辑过。"""

    is_deleted: Mapped[bool] = mapped_column(default=False)
    """是否被删除。"""

    receive_time: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)
    """接收时间。"""


class ReplyMapping(Base):
    """
    Reply → Signal → Trade 映射表。

    老师发一条开仓信号消息。
    映射记录：
      telegram_message_id = 开仓信号的消息 ID
      signal_id = SignalLog.signal_id
      trade_id = Trade.id

    以后老师回复这条消息（改止损/平仓等）：
      signal.reply_to_msg_id → ReplyMapping.telegram_message_id
      → sqlalchemy O(1) 查询 → ReplyMapping.trade_id
      → Trade 100% 命中

    这是整个 Reply 匹配系统的核心表。
    """
    __tablename__ = "reply_mapping"
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_message_id", name="uq_reply_msg_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    """原始开仓信号的 Telegram 消息 ID。作为查询主键。"""

    signal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    """SignalLog.signal_id。通过此字段关联解析结果。"""

    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """Trade.id (FK → trades.id)。直接定位 Trade。"""

    teacher: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    """老师名称。"""

    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    """交易对（如 DOGEUSDT）。"""

    chat_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    """群组 ID。"""

    signal_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, default="new")
    """信号类型: new/update/close。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)
    """记录创建时间。"""


class UpdateHistory(Base):
    """
    老师修改 SL/TP 的历史记录。

    老师每次回复修改止损/止盈，都会记录一条。
    用于：审计老师行为、分析修改频率、问题追溯。
    """
    __tablename__ = "update_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("trades.id"), nullable=True, index=True)
    """关联的 Trade ID。"""

    telegram_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """触发修改的 Telegram 消息 ID。"""

    reply_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    """被回复的原始消息 ID（开仓信号）。"""

    update_type: Mapped[str] = mapped_column(String(32), nullable=False)
    """修改类型: sl/tp/sl_and_tp/add_tp/remove_tp。"""

    old_sl: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """修改前的 SL 价格。"""

    new_sl: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """修改后的 SL 价格。"""

    old_tp: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """修改前的 TP 列表（JSON 字符串）。"""

    new_tp: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """修改后的 TP 列表（JSON 字符串）。"""

    operator: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """操作人（老师名称）。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)


class CloseHistory(Base):
    """
    每次平仓记录。

    无论是老师平仓、系统 TP/SL、还是协调器恢复，都记录。
    用于：平仓原因分析、盈亏统计、交易复盘。
    """
    __tablename__ = "close_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("trades.id"), nullable=True, index=True)
    """关联的 Trade ID。"""

    close_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """平仓类型: manual/teacher/tp1/tp2/tp3/sl/trailing/roi/max_hold/liquidation/reconciliation。"""

    price: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """平仓价格。"""

    pnl: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """盈亏金额。"""

    exit_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """退出原因（详细）。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)


class RecoveryLog(Base):
    """
    协调器恢复 Trade 的记录。

    当协调器发现 DB 与 OKX 不一致时，自动恢复 Trade。
    此表记录每次恢复操作，用于审计和问题追踪。
    """
    __tablename__ = "recovery_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """交易对。"""

    direction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    """方向: long/short。"""

    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    """恢复原因: db_missing/okx_found/ordertype_mismatch/position_mismatch。"""

    db_trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    """恢复前 DB 中的 Trade ID（如果有）。"""

    okx_contracts: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """OKX 实际持仓数量。"""

    rebuilt: Mapped[bool] = mapped_column(default=False)
    """是否重建了 Trade。"""

    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """详细描述。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)


class ReconcilerLog(Base):
    """
    协调器操作审计。

    协调器每次检查/修复 Trade 都会记录。
    用于：监控协调器行为、诊断自动化问题。
    """
    __tablename__ = "reconciler_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """关联的 Trade ID（如果有）。"""

    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """操作类型: check_sl/check_tp/remove_orphan/recover_trade/close_trade/sync_position/repair_order。"""

    result: Mapped[str] = mapped_column(String(16), nullable=False)
    """结果: success/skip/fail。"""

    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """操作详情。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)


class Notification(Base):
    """
    机器人通知记录。

    所有需要通知用户的系统事件都会写入此表。
    用于：用户通知、事件推送、消息历史。
    """
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    """通知类型: open/sl/tp/close/error/recovery/warning/info。"""

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    """通知标题。"""

    message: Mapped[str] = mapped_column(Text, nullable=False)
    """通知内容。"""

    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """关联的 Trade ID（如果有）。"""

    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")
    """级别: info/warning/error/success。"""

    is_read: Mapped[bool] = mapped_column(default=False)
    """是否已读。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)


class ErrorLog(Base):
    """
    异常记录 — 用于 AI 自动分析 Bug。

    程序捕获到异常时记录。
    包含：异常类型、消息、堆栈、上下文。
    以后 AI（Claude）可以直接查询此表进行故障诊断。
    """
    __tablename__ = "error_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exception_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    """异常类型: ValueError/KeyError/ConnectionError 等。"""

    message: Mapped[str] = mapped_column(Text, nullable=False)
    """异常消息。"""

    traceback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """完整堆栈跟踪（用于 AI 分析）。"""

    function: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    """出错的函数名。"""

    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """关联的 Trade ID（如果有）。"""

    signal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    """关联的 Signal ID（如果有）。"""

    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    """关联的交易对（如果有）。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now, index=True)


class DailyStatistics(Base):
    """
    每日统计。

    自动汇总每日交易数据。
    用于：收益曲线、日报告、排行榜。
    """
    __tablename__ = "daily_statistics"
    __table_args__ = (
        UniqueConstraint("date", name="uq_daily_stats_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    """日期。"""

    profit: Mapped[float] = mapped_column(Float(), default=0.0)
    """今日盈利总额。"""

    loss: Mapped[float] = mapped_column(Float(), default=0.0)
    """今日亏损总额。"""

    net_profit: Mapped[float] = mapped_column(Float(), default=0.0)
    """今日净利。"""

    fees: Mapped[float] = mapped_column(Float(), default=0.0)
    """今日手续费。"""

    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    """今日交易数。"""

    win_count: Mapped[int] = mapped_column(Integer, default=0)
    """今日获胜数。"""

    loss_count: Mapped[int] = mapped_column(Integer, default=0)
    """今日亏损数。"""

    win_rate: Mapped[float] = mapped_column(Float(), default=0.0)
    """今日胜率。"""

    volume: Mapped[float] = mapped_column(Float(), default=0.0)
    """今日交易量。"""

    max_drawdown: Mapped[float] = mapped_column(Float(), default=0.0)
    """今日最大回撤。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now, onupdate=_utc_now)


class AuditLog(Base):
    """
    数据库修改审计。

    任何重要的数据库修改操作都会记录。
    用于：问题追溯、数据恢复、安全审计。
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    """被修改的表名。"""

    record_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """被修改的记录 ID。"""

    field: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    """被修改的字段名。"""

    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """旧值。"""

    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """新值。"""

    operation: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    """操作类型: INSERT/UPDATE/DELETE。"""

    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """修改原因（如 reconciliation_removed / teacher_update）。"""

    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    """关联的 Trade ID（如果有）。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)


class AiQueryCache(Base):
    """
    AI 查询缓存。

    Claude/DeepSeek 查询统计信息时，结果会缓存一段时间。
    避免重复扫描大量数据。
    缓存键 = 查询参数签名。
    """
    __tablename__ = "ai_query_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    """缓存键。如 'teacher:BOOGU:30d:BTC'。"""

    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    """缓存的查询结果。"""

    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    """过期时间。超时后应当重新查询。"""

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utc_now, onupdate=_utc_now)


# ═══════════════════════════════════════════════════════════════════════════
# AI 管理后台增强表（不影响交易逻辑）
# ═══════════════════════════════════════════════════════════════════════════


class Execution(Base):
    """
    真实成交记录 — 记录每一次交易所确认的成交。
    用于统计：真实成交价、滑点、手续费、部分成交。
    """
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer, ForeignKey("trades.id"), index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    exchange_execution_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    price: Mapped[float] = mapped_column(Float(), nullable=False)
    amount: Mapped[float] = mapped_column(Float(), nullable=False)
    fee: Mapped[float] = mapped_column(Float(), default=0.0)
    side: Mapped[str] = mapped_column(String(25), nullable=False)  # buy/sell
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    @staticmethod
    def create_from_ccxt(trade_id: int, order_id: Optional[int], execution: dict) -> "Execution":
        return Execution(
            trade_id=trade_id,
            order_id=order_id,
            exchange_execution_id=str(execution.get("id", "")),
            price=float(execution.get("price", 0)),
            amount=float(execution.get("amount", 0)),
            fee=float(execution.get("fee", {}).get("cost", 0)),
            side=execution.get("side", "buy"),
            time=datetime.fromtimestamp(
                execution.get("timestamp", _utc_now().timestamp()) / 1000,
                tz=timezone.utc
            ) if execution.get("timestamp") else _utc_now(),
            details=execution,
        )


class TeacherMessage(Base):
    """
    老师 Telegram 消息存档 — 保存老师的所有消息（不仅是交易信号）。
    用于：消息回溯、上下文分析、老师行为分析。
    """
    __tablename__ = "teacher_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(32), index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    teacher: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(255))
    message_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    is_signal: Mapped[bool] = mapped_column(default=False, index=True)
    signal_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    message_type: Mapped[Optional[str]] = mapped_column(String(32))
    parsed_content: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_teacher_msg_chat_msg_id"),
    )


class TeacherStat(Base):
    """
    老师统计缓存表 — 自动维护，无需每次扫描 trades。

    v2 新增字段:
    - roi: 收益率
    - avg_hold_hours: 平均持仓时间（小时）
    - max_drawdown: 最大回撤
    """
    __tablename__ = "teacher_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teacher: Mapped[str] = mapped_column(String(255), index=True, unique=True)
    group_name: Mapped[Optional[str]] = mapped_column(String(255))
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float(), default=0.0)
    total_profit: Mapped[float] = mapped_column(Float(), default=0.0)
    average_profit: Mapped[float] = mapped_column(Float(), default=0.0)
    average_loss: Mapped[float] = mapped_column(Float(), default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float(), default=0.0)
    total_volume: Mapped[float] = mapped_column(Float(), default=0.0)
    last_trade_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ———— v2 新增字段 ————
    roi: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """收益率（总利润/总投入）。"""

    avg_hold_hours: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """平均持仓时间（小时）。"""

    max_drawdown: Mapped[Optional[float]] = mapped_column(Float(), nullable=True)
    """最大回撤（百分比）。"""

    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    @staticmethod
    def calculate_from_trades(trades: list["Trade"]) -> dict:
        if not trades:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_profit": 0.0,
                "average_profit": 0.0,
                "average_loss": 0.0,
                "profit_factor": 0.0,
                "total_volume": 0.0,
                "last_trade_time": None,
            }

        closed_trades = [t for t in trades if not t.is_open and t.close_date]

        def _pnl(t: Trade) -> float:
            return t.realized_profit or t.close_profit_abs or 0.0

        wins = [t for t in closed_trades if _pnl(t) > 0]
        losses = [t for t in closed_trades if _pnl(t) < 0]

        total_profit = sum(_pnl(t) for t in closed_trades)
        total_volume = sum(t.stake_amount * t.leverage for t in closed_trades)

        avg_profit = sum(_pnl(t) for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(abs(_pnl(t)) for t in losses) / len(losses) if losses else 0.0

        gross_profit = sum(_pnl(t) for t in wins)
        gross_loss = sum(abs(_pnl(t)) for t in losses)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

        last_trade = max(closed_trades, key=lambda t: t.close_date) if closed_trades else None

        return {
            "total_trades": len(closed_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed_trades) if closed_trades else 0.0,
            "total_profit": total_profit,
            "average_profit": avg_profit,
            "average_loss": avg_loss,
            "profit_factor": profit_factor,
            "total_volume": total_volume,
            "last_trade_time": last_trade.close_date if last_trade else None,
        }


class AccountSnapshot(Base):
    """
    账户权益快照 — 定时记录账户状态。
    用于：收益曲线、资金分析、风险评估。
    """
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now, index=True)
    balance: Mapped[float] = mapped_column(Float(), default=0.0)
    equity: Mapped[float] = mapped_column(Float(), default=0.0)
    available: Mapped[float] = mapped_column(Float(), default=0.0)
    margin: Mapped[float] = mapped_column(Float(), default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float(), default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float(), default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    @staticmethod
    def create_from_balance(balance_data: dict, positions_count: int = 0) -> "AccountSnapshot":
        return AccountSnapshot(
            time=_utc_now(),
            balance=float(balance_data.get("balance", 0)),
            equity=float(balance_data.get("equity", 0)),
            available=float(balance_data.get("available", 0)),
            margin=float(balance_data.get("margin", 0)),
            unrealized_pnl=float(balance_data.get("unrealized_pnl", 0)),
            realized_pnl=float(balance_data.get("realized_pnl", 0)),
            open_positions=positions_count,
            details=balance_data,
        )


class SystemEvent(Base):
    """
    系统事件日志 — 记录所有重要系统事件。
    用于：故障诊断、性能分析、运行监控。
    """
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    @staticmethod
    def log(level: str, module: str, event_type: str, message: str,
            trade_id: Optional[int] = None, symbol: Optional[str] = None,
            details: Optional[dict] = None) -> "SystemEvent":
        return SystemEvent(
            level=level.lower(),
            module=module,
            event_type=event_type,
            message=message,
            trade_id=trade_id,
            symbol=symbol,
            details=details,
        )


class TeacherMonthRatioSnapshot(Base):
    """
    老师月度仓位档位快照 — 每月 1 号从 Dashboard 90 天收益率排行榜拉取一次。

    当月所有跟单开仓读取本表锁定比例（整个自然月不变），
    历史月份保留用于追溯「某月某老师为什么是某档仓位」。

    设计约定：
    - teacher_key 为归一化标识（@username 小写），teacher_name 为快照时排行榜原串，
      老师改名后仍可按 @username 匹配到当月档位；
    - 同一老师同月仅一行（UNIQUE(snapshot_month, teacher_key)），重复生成走先删后插；
    - 本表不应被每日清理任务删除（cleanup_service 只清理固定几张历史表）。
    """
    __tablename__ = "teacher_month_ratio_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_month", "teacher_key", name="uq_snapshot_month_teacher"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # 'YYYY-MM'（本地月）
    teacher_key: Mapped[str] = mapped_column(String(120), nullable=False)  # 归一化 @username 小写
    teacher_name: Mapped[str] = mapped_column(String(255), nullable=False)  # 排行榜原串（可展示）
    snapshot_rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 快照时的榜单名次
    position_ratio: Mapped[float] = mapped_column(Float, nullable=False)  # 0.07 / 0.04 / 0.01
    snapshot_timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now
    )


# Diagnostic-agent records are deliberately separate from trading state.  A
# checkpoint describes an investigation, never the current exchange state.
class DiagnosticIncidentRecord(Base):
    __tablename__ = "diagnostic_incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    correlation_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recovery_steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    checkpoint: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class DiagnosticApprovalRecord(Base):
    __tablename__ = "diagnostic_approvals"
    __table_args__ = (
        UniqueConstraint("incident_id", "plan_version", "plan_digest", "decision", name="uq_diag_approval"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    object_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    modified_parameters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class DiagnosticExecutionRecord(Base):
    __tablename__ = "diagnostic_executions"

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    verification: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
