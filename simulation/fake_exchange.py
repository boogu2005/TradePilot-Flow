"""
FakeExchange - 模拟交易所接口
实现与 exchange.py 完全相同的接口，用于无缝替换
"""
from __future__ import annotations
import asyncio
import time
import uuid
from typing import Optional, Any
from loguru import logger

from .fake_market import FakeMarket
from .fake_orderbook import FakeOrderBook, OrderType, OrderStatus
from .fake_position import FakePositionManager
from .fake_balance import FakeBalance


class FakeExchange:
    """
    模拟交易所

    实现与 exchange.py 完全相同的接口，支持：
    - create_order: 创建订单
    - cancel_order: 取消订单
    - fetch_order: 查询订单
    - fetch_open_orders: 查询未成交订单
    - fetch_positions: 查询仓位
    - fetch_balance: 查询余额
    - fetch_ticker: 查询行情
    """

    def __init__(self, initial_balance: float = 10000.0):
        """
        初始化模拟交易所

        Args:
            initial_balance: 初始余额（USDT）
        """
        self._market = FakeMarket()
        self._orderbook = FakeOrderBook()
        self._positions = FakePositionManager()
        self._balance = FakeBalance(initial_balance)

        # 缓存层（模拟 api_cache）
        self._ticker_cache: dict[str, dict] = {}
        self._position_cache: list[dict] = []
        self._balance_cache: dict = {}
        self._open_orders_cache: list[dict] = []

        # 缓存时间戳
        self._ticker_cache_time: float = 0
        self._position_cache_time: float = 0
        self._balance_cache_time: float = 0
        self._open_orders_cache_time: float = 0

        # 缓存 TTL（秒）
        self._ticker_ttl = 1.0
        self._position_ttl = 2.0
        self._balance_ttl = 5.0
        self._open_orders_ttl = 2.0

        # 市场信息缓存
        self._markets_cache: dict[str, dict] = {}
        self._init_markets()

        logger.info(f"[FakeExchange] 初始化完成，初始余额: {initial_balance} USDT")

    def _init_markets(self) -> None:
        """初始化市场信息"""
        # 模拟常见交易对
        markets = [
            ("BTC/USDT:USDT", "BTC", 0.001, 100000.0),
            ("ETH/USDT:USDT", "ETH", 0.01, 3000.0),
            ("SOL/USDT:USDT", "SOL", 0.1, 150.0),
            ("BNB/USDT:USDT", "BNB", 0.01, 500.0),
            ("XRP/USDT:USDT", "XRP", 1.0, 0.5),
            ("DOGE/USDT:USDT", "DOGE", 10.0, 0.08),
        ]

        for symbol, base, min_amount, initial_price in markets:
            self._markets_cache[symbol] = {
                "symbol": symbol,
                "base": base,
                "quote": "USDT",
                "active": True,
                "precision": {"amount": min_amount, "price": 0.01},
                "limits": {
                    "amount": {"min": min_amount, "max": 1000000},
                    "price": {"min": 0.01, "max": 10000000},
                },
                "contractSize": 1.0,
            }
            self._market.add_market(symbol, initial_price)

    # ========== 行情接口 ==========

    async def fetch_ticker(self, symbol: str) -> dict:
        """
        获取行情（带缓存）

        Args:
            symbol: 交易对

        Returns:
            ticker 数据
        """
        # 检查缓存
        if time.time() - self._ticker_cache_time < self._ticker_ttl:
            if symbol in self._ticker_cache:
                return self._ticker_cache[symbol]

        # 获取最新行情
        ticker = self._market.get_ticker(symbol)
        self._ticker_cache[symbol] = ticker
        self._ticker_cache_time = time.time()

        return ticker

    async def fetch_tickers(self, symbols: Optional[list[str]] = None) -> list[dict]:
        """获取多个行情"""
        if symbols:
            return [await self.fetch_ticker(s) for s in symbols]
        return self._market.get_all_tickers()

    # ========== 余额接口 ==========

    async def fetch_balance(self) -> dict:
        """
        获取账户余额（带缓存）

        Returns:
            余额数据
        """
        # 检查缓存
        if time.time() - self._balance_cache_time < self._balance_ttl:
            if self._balance_cache:
                return self._balance_cache

        # 更新未实现盈亏
        total_unrealized = sum(
            pos.unrealized_pnl for pos in self._positions.get_all_positions()
        )
        self._balance.update_unrealized_pnl(total_unrealized)

        balance = self._balance.get_balance()
        self._balance_cache = {
            "USDT": {
                "free": balance["free"],
                "used": balance["used"],
                "total": balance["total"],
            },
            "info": {
                "totalUnrealizedProfit": str(balance["unrealizedPnl"]),
            },
        }
        self._balance_cache_time = time.time()

        return self._balance_cache

    # ========== 仓位接口 ==========

    async def fetch_positions(self, symbols: Optional[list[str]] = None) -> list[dict]:
        """
        获取仓位（带缓存）

        Args:
            symbols: 可选，指定交易对

        Returns:
            仓位列表
        """
        # 检查缓存
        if time.time() - self._position_cache_time < self._position_ttl:
            if self._position_cache:
                if symbols:
                    return [p for p in self._position_cache if p["symbol"] in symbols]
                return self._position_cache

        # 获取所有仓位
        positions = self._positions.get_all_positions()
        result = [pos.to_dict() for pos in positions]

        self._position_cache = result
        self._position_cache_time = time.time()

        if symbols:
            return [p for p in result if p["symbol"] in symbols]
        return result

    # ========== 订单接口 ==========

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """
        创建订单

        Args:
            symbol: 交易对
            ordertype: 订单类型（market/limit/stop_loss/take_profit/trailing_stop）
            side: "buy" or "sell"
            amount: 数量
            price: 限价单价格
            params: 额外参数

        Returns:
            订单数据
        """
        params = params or {}

        # 解析订单类型
        order_type_map = {
            "market": OrderType.MARKET,
            "limit": OrderType.LIMIT,
            "stop": OrderType.STOP_LOSS,
            "stop_loss": OrderType.STOP_LOSS,
            "stop_market": OrderType.STOP_LOSS,
            "stop_limit": OrderType.STOP_LOSS,
            "take_profit": OrderType.TAKE_PROFIT,
            "take_profit_limit": OrderType.TAKE_PROFIT,
            "trailing": OrderType.TRAILING_STOP,
        }
        order_type = order_type_map.get(order_type.lower(), OrderType.LIMIT)

        # 提取参数
        stop_price = params.get("stopPrice") or params.get("stop_price")
        # 对于 SL/TP 订单，如果没指定 stopPrice，用 price 作为触发价
        if stop_price is None and order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT):
            stop_price = price
        reduce_only = params.get("reduceOnly", False)
        trailing_percent = params.get("trailingPercent") or params.get("trailing_percent")
        client_order_id = params.get("clOrdId") or params.get("clientOrderId")

        # 创建订单
        order = self._orderbook.create_order(
            symbol=symbol,
            order_type=order_type,
            side=side,
            amount=amount,
            price=price,
            stop_price=stop_price,
            reduce_only=reduce_only,
            trailing_percent=trailing_percent,
            client_order_id=client_order_id,
        )

        # 处理开仓/平仓
        ticker = await self.fetch_ticker(symbol)
        current_price = ticker["last"]

        if order_type == OrderType.MARKET:
            # 市价单立即成交
            fill_price = current_price
            self._orderbook.fill_order(order.id, fill_price)

            # 更新仓位和余额
            if not reduce_only:
                # 开仓
                pos_side = "long" if side == "buy" else "short"
                leverage = params.get("leverage", 10)
                margin = (fill_price * amount) / leverage

                if not self._balance.freeze_margin(margin):
                    raise ValueError(f"余额不足: 需要 {margin} USDT, 可用 {self._balance.get_free_balance()} USDT")

                self._positions.open_position(
                    symbol=symbol,
                    side=pos_side,
                    contracts=amount,
                    entry_price=fill_price,
                    leverage=leverage,
                )
            else:
                # 平仓
                pos_side = "long" if side == "sell" else "short"
                position = self._positions.get_position(symbol, pos_side)

                if position:
                    # 计算已实现盈亏
                    if pos_side == "long":
                        realized_pnl = (fill_price - position.entry_price) * amount
                    else:
                        realized_pnl = (position.entry_price - fill_price) * amount

                    self._positions.close_position(symbol, pos_side, amount, fill_price)
                    self._balance.release_margin((fill_price * amount) / position.leverage)
                    self._balance.add_realized_pnl(realized_pnl)
                    self._balance.deduct_fee(order.fee)

            # 失效缓存
            self._invalidate_caches()

        else:
            # 限价单/止损单/止盈单 - 保持 pending 状态
            # 不立即触发，等待 update_price() 调用时检查
            # 只有当价格已经穿过触发价时才立即成交
            should_fill_immediately = False
            fill_price = current_price

            if order_type == OrderType.LIMIT:
                # 限价单：如果当前价格已经穿过限价，立即成交
                if side == "buy" and current_price <= (price or 0):
                    should_fill_immediately = True
                    fill_price = price
                elif side == "sell" and current_price >= (price or float('inf')):
                    should_fill_immediately = True
                    fill_price = price

            # SL/TP/Trailing 保持 pending，等待 update_price() 触发

            if should_fill_immediately:
                # 立即成交
                self._orderbook.fill_order(order.id, fill_price)

                if not reduce_only:
                    # 开仓
                    pos_side = "long" if side == "buy" else "short"
                    leverage = params.get("leverage", 10)
                    margin = (fill_price * amount) / leverage

                    if not self._balance.freeze_margin(margin):
                        raise ValueError(f"余额不足")

                    self._positions.open_position(
                        symbol=symbol,
                        side=pos_side,
                        contracts=amount,
                        entry_price=fill_price,
                        leverage=leverage,
                    )
                else:
                    # 平仓
                    pos_side = "long" if side == "sell" else "short"
                    position = self._positions.get_position(symbol, pos_side)

                    if position:
                        if pos_side == "long":
                            realized_pnl = (fill_price - position.entry_price) * amount
                        else:
                            realized_pnl = (position.entry_price - fill_price) * amount

                        self._positions.close_position(symbol, pos_side, amount, fill_price)
                        self._balance.release_margin((fill_price * amount) / position.leverage)
                        self._balance.add_realized_pnl(realized_pnl)
                        self._balance.deduct_fee(order.fee)

                self._invalidate_caches()

        # 返回订单数据
        return order.to_dict()

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> dict:
        """
        取消订单

        Args:
            order_id: 订单ID
            symbol: 交易对（可选）

        Returns:
            订单数据
        """
        order = self._orderbook.get_order(order_id)
        if not order:
            raise ValueError(f"订单不存在: {order_id}")

        if not self._orderbook.cancel_order(order_id):
            raise ValueError(f"订单无法取消: {order_id}")

        self._invalidate_caches()
        return order.to_dict()

    async def fetch_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[dict]:
        """
        查询订单

        Args:
            order_id: 订单ID
            symbol: 交易对（可选）

        Returns:
            订单数据
        """
        order = self._orderbook.get_order(order_id)
        if order:
            return order.to_dict()

        # 查询历史订单
        for hist_order in self._orderbook.get_all_orders():
            if hist_order.id == order_id:
                return hist_order.to_dict()

        return None

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        """
        查询未成交订单（带缓存）

        Args:
            symbol: 交易对（可选）

        Returns:
            未成交订单列表
        """
        # 检查缓存
        if time.time() - self._open_orders_cache_time < self._open_orders_ttl:
            if self._open_orders_cache:
                if symbol:
                    return [o for o in self._open_orders_cache if o["symbol"] == symbol]
                return self._open_orders_cache

        orders = self._orderbook.get_open_orders(symbol)
        result = [o.to_dict() for o in orders]

        self._open_orders_cache = result
        self._open_orders_cache_time = time.time()

        if symbol:
            return [o for o in result if o["symbol"] == symbol]
        return result

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> dict:
        """
        取消所有订单

        Args:
            symbol: 交易对（可选）

        Returns:
            取消结果
        """
        cancelled = self._orderbook.cancel_all_orders(symbol)
        self._invalidate_caches()
        return {"cancelled": cancelled}

    # ========== 辅助方法 ==========

    def _invalidate_caches(self) -> None:
        """失效所有缓存"""
        self._ticker_cache_time = 0
        self._position_cache_time = 0
        self._balance_cache_time = 0
        self._open_orders_cache_time = 0

    def update_price(self, symbol: str, price: float) -> None:
        """
        更新价格（用于测试）

        Args:
            symbol: 交易对
            price: 新价格
        """
        self._market.move_price(symbol, price)
        self._positions.update_mark_price(symbol, price)
        self._invalidate_caches()

        # 检查是否有订单触发
        triggered = self._orderbook.process_price_update(symbol, price)

        for order in triggered:
            # 处理触发的订单
            if order.order_type == OrderType.LIMIT:
                # 限价单触发 - 开仓
                if not order.reduce_only:
                    pos_side = "long" if order.side == "buy" else "short"
                    leverage = 10  # 默认杠杆
                    margin = (price * order.amount) / leverage

                    if self._balance.freeze_margin(margin):
                        self._positions.open_position(
                            symbol=symbol,
                            side=pos_side,
                            contracts=order.amount,
                            entry_price=price,
                            leverage=leverage,
                        )
                else:
                    # 限价单平仓
                    pos_side = "long" if order.side == "sell" else "short"
                    position = self._positions.get_position(symbol, pos_side)

                    if position:
                        if pos_side == "long":
                            realized_pnl = (price - position.entry_price) * order.amount
                        else:
                            realized_pnl = (position.entry_price - price) * order.amount

                        self._positions.close_position(symbol, pos_side, order.amount, price)
                        self._balance.release_margin((price * order.amount) / position.leverage)
                        self._balance.add_realized_pnl(realized_pnl)
                        self._balance.deduct_fee(order.fee)

            elif order.order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT):
                if order.reduce_only:
                    # 平仓
                    pos_side = "long" if order.side == "sell" else "short"
                    position = self._positions.get_position(order.symbol, pos_side)

                    if position:
                        fill_price = order.stop_price or price
                        if pos_side == "long":
                            realized_pnl = (fill_price - position.entry_price) * order.amount
                        else:
                            realized_pnl = (position.entry_price - fill_price) * order.amount

                        self._positions.close_position(order.symbol, pos_side, order.amount, fill_price)
                        self._balance.release_margin((fill_price * order.amount) / position.leverage)
                        self._balance.add_realized_pnl(realized_pnl)
                        self._balance.deduct_fee(order.fee)

            elif order.order_type == OrderType.TRAILING_STOP:
                if order.reduce_only:
                    # 追踪止损平仓
                    pos_side = "long" if order.side == "sell" else "short"
                    position = self._positions.get_position(order.symbol, pos_side)

                    if position:
                        fill_price = price
                        if pos_side == "long":
                            realized_pnl = (fill_price - position.entry_price) * order.amount
                        else:
                            realized_pnl = (position.entry_price - fill_price) * order.amount

                        self._positions.close_position(order.symbol, pos_side, order.amount, fill_price)
                        self._balance.release_margin((fill_price * order.amount) / position.leverage)
                        self._balance.add_realized_pnl(realized_pnl)
                        self._balance.deduct_fee(order.fee)

    def get_market_info(self, symbol: str) -> Optional[dict]:
        """获取市场信息"""
        return self._markets_cache.get(symbol)

    def reset(self, initial_balance: float = 10000.0) -> None:
        """
        重置交易所状态

        Args:
            initial_balance: 初始余额
        """
        self._orderbook.clear()
        self._positions.clear()
        self._balance.reset(initial_balance)
        self._invalidate_caches()

        # 重置市场价格为初始值
        initial_prices = {
            "BTC/USDT:USDT": 100000.0,
            "ETH/USDT:USDT": 3000.0,
            "SOL/USDT:USDT": 150.0,
            "BNB/USDT:USDT": 500.0,
            "XRP/USDT:USDT": 0.5,
            "DOGE/USDT:USDT": 0.08,
        }
        for symbol, price in initial_prices.items():
            self._market.move_price(symbol, price)

        logger.info(f"[FakeExchange] 重置完成，初始余额: {initial_balance} USDT")
