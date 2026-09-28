"""
FakeOrderBook - 订单簿模拟器
管理所有订单的生命周期：pending, filled, cancelled, rejected
"""
from __future__ import annotations
import uuid
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field


class OrderStatus(str, Enum):
    """订单状态"""
    PENDING = "pending"      # 等待成交
    FILLED = "filled"        # 已成交
    CANCELLED = "cancelled"  # 已取消
    REJECTED = "rejected"    # 已拒绝
    PARTIAL = "partial"      # 部分成交


class OrderType(str, Enum):
    """订单类型"""
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"


@dataclass
class FakeOrder:
    """模拟订单"""
    id: str
    symbol: str
    order_type: OrderType
    side: str  # "buy" or "sell"
    amount: float
    price: Optional[float] = None  # limit price
    stop_price: Optional[float] = None  # trigger price
    reduce_only: bool = False
    status: OrderStatus = OrderStatus.PENDING
    filled_amount: float = 0.0
    filled_price: float = 0.0
    fee: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    client_order_id: Optional[str] = None

    # Trailing stop specific
    trailing_percent: Optional[float] = None
    highest_price: float = 0.0  # for long trailing
    lowest_price: float = float('inf')  # for short trailing
    activated: bool = False

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "type": self.order_type.value,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "stopPrice": self.stop_price,
            "reduceOnly": self.reduce_only,
            "status": self.status.value,
            "filled": self.filled_amount,
            "filledPrice": self.filled_price,
            "fee": self.fee,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "clientOrderId": self.client_order_id,
            "trailingPercent": self.trailing_percent,
            "highestPrice": self.highest_price if self.highest_price > 0 else None,
            "lowestPrice": self.lowest_price if self.lowest_price < float('inf') else None,
            "activated": self.activated,
        }


class FakeOrderBook:
    """
    订单簿模拟器

    管理所有订单的生命周期，支持：
    - Market: 立即成交
    - Limit: 等待价格触发
    - StopLoss: 触发价到达时全部平仓
    - TakeProfit: 触发价到达时部分/全部平仓
    - Trailing: 激活后自动移动止损价
    """

    def __init__(self):
        self._orders: dict[str, FakeOrder] = {}
        self._order_history: list[FakeOrder] = []
        self._fee_rate = 0.0004  # 0.04% fee

    def create_order(
        self,
        symbol: str,
        order_type: OrderType,
        side: str,
        amount: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        trailing_percent: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> FakeOrder:
        """
        创建订单

        Args:
            symbol: 交易对
            order_type: 订单类型
            side: "buy" or "sell"
            amount: 数量
            price: 限价单价格
            stop_price: 止损/止盈触发价
            reduce_only: 是否只减仓
            trailing_percent: 追踪止损百分比
            client_order_id: 客户端订单ID

        Returns:
            创建的订单
        """
        order_id = f"sim_{uuid.uuid4().hex[:16]}"

        order = FakeOrder(
            id=order_id,
            symbol=symbol,
            order_type=order_type,
            side=side,
            amount=amount,
            price=price,
            stop_price=stop_price,
            reduce_only=reduce_only,
            trailing_percent=trailing_percent,
            client_order_id=client_order_id or f"bot{uuid.uuid4().hex[:18]}",
        )

        self._orders[order_id] = order
        return order

    def get_order(self, order_id: str) -> Optional[FakeOrder]:
        """获取订单"""
        return self._orders.get(order_id)

    def cancel_order(self, order_id: str) -> bool:
        """
        取消订单

        Args:
            order_id: 订单ID

        Returns:
            是否成功取消
        """
        order = self._orders.get(order_id)
        if not order:
            return False

        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            return False

        order.status = OrderStatus.CANCELLED
        order.updated_at = time.time()
        self._move_to_history(order)
        return True

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        取消所有订单

        Args:
            symbol: 可选，只取消指定交易对的订单

        Returns:
            取消的订单数量
        """
        cancelled = 0
        order_ids = list(self._orders.keys())

        for order_id in order_ids:
            order = self._orders.get(order_id)
            if not order:
                continue
            if symbol and order.symbol != symbol:
                continue
            if self.cancel_order(order_id):
                cancelled += 1

        return cancelled

    def get_open_orders(self, symbol: Optional[str] = None) -> list[FakeOrder]:
        """
        获取未成交订单

        Args:
            symbol: 可选，只返回指定交易对的订单

        Returns:
            未成交订单列表
        """
        orders = []
        for order in self._orders.values():
            if order.status != OrderStatus.PENDING:
                continue
            if symbol and order.symbol != symbol:
                continue
            orders.append(order)
        return orders

    def get_filled_orders(self, symbol: Optional[str] = None) -> list[FakeOrder]:
        """获取已成交订单"""
        orders = []
        for order in self._order_history:
            if order.status != OrderStatus.FILLED:
                continue
            if symbol and order.symbol != symbol:
                continue
            orders.append(order)
        return orders

    def fill_order(self, order_id: str, fill_price: float, fill_amount: Optional[float] = None) -> bool:
        """
        成交订单

        Args:
            order_id: 订单ID
            fill_price: 成交价格
            fill_amount: 成交数量（可选，默认全部）

        Returns:
            是否成功成交
        """
        order = self._orders.get(order_id)
        if not order:
            return False

        if order.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            return False

        amount_to_fill = fill_amount or order.amount
        if amount_to_fill > order.amount - order.filled_amount:
            amount_to_fill = order.amount - order.filled_amount

        order.filled_amount += amount_to_fill
        order.filled_price = fill_price
        order.fee = fill_price * amount_to_fill * self._fee_rate
        order.updated_at = time.time()

        if order.filled_amount >= order.amount:
            order.status = OrderStatus.FILLED
            self._move_to_history(order)
        else:
            order.status = OrderStatus.PARTIAL

        return True

    def reject_order(self, order_id: str, reason: str = "") -> bool:
        """
        拒绝订单

        Args:
            order_id: 订单ID
            reason: 拒绝原因

        Returns:
            是否成功拒绝
        """
        order = self._orders.get(order_id)
        if not order:
            return False

        if order.status != OrderStatus.PENDING:
            return False

        order.status = OrderStatus.REJECTED
        order.updated_at = time.time()
        self._move_to_history(order)
        return True

    def _move_to_history(self, order: FakeOrder) -> None:
        """将订单移到历史记录"""
        self._order_history.append(order)
        del self._orders[order.id]

    def process_price_update(self, symbol: str, price: float) -> list[FakeOrder]:
        """
        处理价格更新，检查是否有订单触发

        Args:
            symbol: 交易对
            price: 新价格

        Returns:
            触发的订单列表
        """
        triggered = []

        for order in list(self._orders.values()):
            if order.symbol != symbol:
                continue
            if order.status != OrderStatus.PENDING:
                continue

            should_fill = False
            fill_price = price

            # Market order - always fill
            if order.order_type == OrderType.MARKET:
                should_fill = True

            # Limit order - fill when price crosses limit
            elif order.order_type == OrderType.LIMIT:
                if order.side == "buy" and price <= order.price:
                    should_fill = True
                    fill_price = order.price
                elif order.side == "sell" and price >= order.price:
                    should_fill = True
                    fill_price = order.price

            # Stop loss - fill when price hits stop
            elif order.order_type == OrderType.STOP_LOSS:
                if order.stop_price is not None:
                    if order.side == "sell" and price <= order.stop_price:
                        should_fill = True
                        fill_price = order.stop_price
                    elif order.side == "buy" and price >= order.stop_price:
                        should_fill = True
                        fill_price = order.stop_price

            # Take profit - fill when price hits target
            elif order.order_type == OrderType.TAKE_PROFIT:
                if order.stop_price is not None:
                    if order.side == "sell" and price >= order.stop_price:
                        should_fill = True
                        fill_price = order.stop_price
                    elif order.side == "buy" and price <= order.stop_price:
                        should_fill = True
                        fill_price = order.stop_price

            # Trailing stop - check activation and trigger
            elif order.order_type == OrderType.TRAILING_STOP:
                if self._process_trailing_stop(order, price):
                    should_fill = True
                    fill_price = price

            if should_fill:
                self.fill_order(order.id, fill_price)
                triggered.append(order)

        return triggered

    def _process_trailing_stop(self, order: FakeOrder, price: float) -> bool:
        """
        处理追踪止损

        Returns:
            是否触发
        """
        if not order.trailing_percent:
            return False

        # Initialize tracking
        if not order.activated:
            # For long: activate when price goes up
            # For short: activate when price goes down
            order.activated = True
            order.highest_price = price
            order.lowest_price = price

        # Update highest/lowest
        if price > order.highest_price:
            order.highest_price = price
        if price < order.lowest_price:
            order.lowest_price = price

        # Calculate trailing stop price
        if order.side == "sell":  # Long position trailing stop
            stop_price = order.highest_price * (1 - order.trailing_percent / 100.0)
            if price <= stop_price:
                return True
        else:  # Short position trailing stop
            stop_price = order.lowest_price * (1 + order.trailing_percent / 100.0)
            if price >= stop_price:
                return True

        return False

    def get_all_orders(self) -> list[FakeOrder]:
        """获取所有订单（包括历史）"""
        return list(self._orders.values()) + self._order_history

    def clear(self) -> None:
        """清空所有订单"""
        self._orders.clear()
        self._order_history.clear()
