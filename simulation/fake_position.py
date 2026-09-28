"""
FakePosition - 仓位模拟器
管理交易仓位的生命周期
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class FakePosition:
    """模拟仓位"""
    symbol: str
    side: str  # "long" or "short"
    contracts: float
    entry_price: float
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    leverage: int = 10
    margin_mode: str = "isolated"  # "isolated" or "cross"
    liquidation_price: Optional[float] = None
    margin: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "contracts": self.contracts,
            "entryPrice": self.entry_price,
            "markPrice": self.mark_price,
            "unrealizedPnl": self.unrealized_pnl,
            "realizedPnl": self.realized_pnl,
            "leverage": self.leverage,
            "marginMode": self.margin_mode,
            "liquidationPrice": self.liquidation_price,
            "margin": self.margin,
            "timestamp": self.updated_at,
        }

    def update_mark_price(self, price: float) -> None:
        """更新标记价格并计算未实现盈亏"""
        self.mark_price = price
        self.updated_at = time.time()

        # 计算未实现盈亏
        if self.side == "long":
            self.unrealized_pnl = (price - self.entry_price) * self.contracts
        else:  # short
            self.unrealized_pnl = (self.entry_price - price) * self.contracts

        # 计算强平价格（简化计算）
        if self.margin > 0:
            maintenance_margin_rate = 0.005  # 0.5% 维持保证金率
            if self.side == "long":
                self.liquidation_price = self.entry_price * (1 - 1 / self.leverage + maintenance_margin_rate)
            else:
                self.liquidation_price = self.entry_price * (1 + 1 / self.leverage - maintenance_margin_rate)


class FakePositionManager:
    """
    仓位管理器

    管理所有交易仓位，支持：
    - 开仓
    - 平仓
    - 部分平仓
    - 更新标记价格
    """

    def __init__(self):
        self._positions: dict[str, FakePosition] = {}  # key: "symbol:side"

    def _get_key(self, symbol: str, side: str) -> str:
        """生成仓位键"""
        return f"{symbol}:{side}"

    def open_position(
        self,
        symbol: str,
        side: str,
        contracts: float,
        entry_price: float,
        leverage: int = 10,
        margin_mode: str = "isolated",
    ) -> FakePosition:
        """
        开仓

        Args:
            symbol: 交易对
            side: "long" or "short"
            contracts: 合约数量
            entry_price: 入场价格
            leverage: 杠杆
            margin_mode: 保证金模式

        Returns:
            创建的仓位
        """
        key = self._get_key(symbol, side)

        # 检查是否已有仓位
        if key in self._positions:
            existing = self._positions[key]
            # 加仓：计算平均入场价
            total_contracts = existing.contracts + contracts
            avg_price = (
                (existing.entry_price * existing.contracts + entry_price * contracts)
                / total_contracts
            )
            existing.contracts = total_contracts
            existing.entry_price = avg_price
            existing.mark_price = entry_price
            existing.margin = (entry_price * contracts) / leverage
            existing.updated_at = time.time()
            existing.update_mark_price(entry_price)
            return existing

        # 新开仓
        margin = (entry_price * contracts) / leverage
        position = FakePosition(
            symbol=symbol,
            side=side,
            contracts=contracts,
            entry_price=entry_price,
            mark_price=entry_price,
            leverage=leverage,
            margin_mode=margin_mode,
            margin=margin,
        )
        position.update_mark_price(entry_price)

        self._positions[key] = position
        return position

    def close_position(
        self,
        symbol: str,
        side: str,
        contracts: float,
        exit_price: float,
    ) -> Optional[FakePosition]:
        """
        平仓

        Args:
            symbol: 交易对
            side: 仓位方向
            contracts: 平仓数量
            exit_price: 平仓价格

        Returns:
            更新后的仓位（如果完全平仓则返回 None）
        """
        key = self._get_key(symbol, side)
        position = self._positions.get(key)

        if not position:
            return None

        if contracts >= position.contracts:
            # 完全平仓
            # 计算已实现盈亏
            if side == "long":
                realized = (exit_price - position.entry_price) * position.contracts
            else:
                realized = (position.entry_price - exit_price) * position.contracts

            position.realized_pnl += realized
            del self._positions[key]
            return None
        else:
            # 部分平仓
            if side == "long":
                realized = (exit_price - position.entry_price) * contracts
            else:
                realized = (position.entry_price - exit_price) * contracts

            position.realized_pnl += realized
            position.contracts -= contracts
            position.margin -= (exit_price * contracts) / position.leverage
            position.updated_at = time.time()
            position.update_mark_price(exit_price)
            return position

    def get_position(self, symbol: str, side: Optional[str] = None) -> Optional[FakePosition]:
        """
        获取仓位

        Args:
            symbol: 交易对
            side: 可选，指定方向

        Returns:
            仓位对象
        """
        if side:
            key = self._get_key(symbol, side)
            return self._positions.get(key)
        else:
            # 尝试 long 和 short
            for s in ["long", "short"]:
                key = self._get_key(symbol, s)
                if key in self._positions:
                    return self._positions[key]
        return None

    def get_all_positions(self) -> list[FakePosition]:
        """获取所有仓位"""
        return list(self._positions.values())

    def update_mark_price(self, symbol: str, price: float) -> None:
        """
        更新标记价格

        Args:
            symbol: 交易对
            price: 新价格
        """
        for side in ["long", "short"]:
            key = self._get_key(symbol, side)
            if key in self._positions:
                self._positions[key].update_mark_price(price)

    def has_position(self, symbol: str) -> bool:
        """检查是否有仓位"""
        for side in ["long", "short"]:
            key = self._get_key(symbol, side)
            if key in self._positions:
                return True
        return False

    def clear(self) -> None:
        """清空所有仓位"""
        self._positions.clear()
