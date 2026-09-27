"""
FakeMarket - 价格模拟器
模拟市场价格变动，支持多种价格操作方式。
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class MarketData:
    """市场数据"""
    symbol: str
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    mark_price: float = 0.0
    index_price: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_ticker(self) -> dict:
        """转换为 ticker 格式"""
        return {
            "symbol": self.symbol,
            "last": self.last,
            "bid": self.bid,
            "ask": self.ask,
            "mark": self.mark_price,
            "index": self.index_price,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
        }


class FakeMarket:
    """
    价格模拟器

    支持操作：
    - move_price(price): 直接设置价格
    - move_percent(pct): 按百分比变动
    - jump(price): 瞬间跳价
    - tick(): 自动 tick（基于波动率）
    """

    def __init__(self, initial_price: float = 100.0, spread_pct: float = 0.0001):
        """
        初始化价格模拟器

        Args:
            initial_price: 初始价格
            spread_pct: 买卖价差百分比（默认 0.01%）
        """
        self._markets: dict[str, MarketData] = {}
        self._spread_pct = spread_pct
        self._volatility = 0.001  # 默认波动率 0.1%

        # 添加默认市场
        self.add_market("BTC/USDT:USDT", initial_price)
        self.add_market("ETH/USDT:USDT", initial_price * 0.05)
        self.add_market("SOL/USDT:USDT", initial_price * 0.002)

    def add_market(self, symbol: str, price: float) -> None:
        """添加市场"""
        spread = price * self._spread_pct
        self._markets[symbol] = MarketData(
            symbol=symbol,
            last=price,
            bid=price - spread / 2,
            ask=price + spread / 2,
            mark_price=price,
            index_price=price,
            timestamp=time.time(),
        )

    def get_market(self, symbol: str) -> Optional[MarketData]:
        """获取市场数据"""
        return self._markets.get(symbol)

    def move_price(self, symbol: str, price: float) -> MarketData:
        """
        直接设置价格

        Args:
            symbol: 交易对
            price: 新价格

        Returns:
            更新后的市场数据
        """
        market = self._markets.get(symbol)
        if not market:
            raise ValueError(f"Market {symbol} not found")

        spread = price * self._spread_pct
        market.last = price
        market.bid = price - spread / 2
        market.ask = price + spread / 2
        market.mark_price = price
        market.index_price = price
        market.timestamp = time.time()

        return market

    def move_percent(self, symbol: str, pct: float) -> MarketData:
        """
        按百分比变动价格

        Args:
            symbol: 交易对
            pct: 百分比（如 2.0 表示 +2%，-5.0 表示 -5%）

        Returns:
            更新后的市场数据
        """
        market = self._markets.get(symbol)
        if not market:
            raise ValueError(f"Market {symbol} not found")

        new_price = market.last * (1 + pct / 100.0)
        return self.move_price(symbol, new_price)

    def jump(self, symbol: str, price: float) -> MarketData:
        """
        瞬间跳价（用于测试极端情况）

        Args:
            symbol: 交易对
            price: 新价格

        Returns:
            更新后的市场数据
        """
        return self.move_price(symbol, price)

    def tick(self, symbol: str) -> MarketData:
        """
        自动 tick（基于波动率随机变动）

        Args:
            symbol: 交易对

        Returns:
            更新后的市场数据
        """
        import random
        market = self._markets.get(symbol)
        if not market:
            raise ValueError(f"Market {symbol} not found")

        # 随机波动
        change = random.gauss(0, self._volatility)
        new_price = market.last * (1 + change)
        return self.move_price(symbol, new_price)

    def set_volatility(self, volatility: float) -> None:
        """设置波动率"""
        self._volatility = volatility

    def get_ticker(self, symbol: str) -> dict:
        """获取 ticker 数据"""
        market = self._markets.get(symbol)
        if not market:
            raise ValueError(f"Market {symbol} not found")
        return market.to_ticker()

    def get_all_tickers(self) -> list[dict]:
        """获取所有市场的 ticker"""
        return [market.to_ticker() for market in self._markets.values()]

    def get_last_price(self, symbol: str) -> float:
        """获取最新价格"""
        market = self._markets.get(symbol)
        if not market:
            raise ValueError(f"Market {symbol} not found")
        return market.last
