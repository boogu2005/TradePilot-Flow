"""
FakeBalance - 余额模拟器
管理账户余额、保证金、已实现/未实现盈亏
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class BalanceData:
    """余额数据"""
    currency: str = "USDT"
    total: float = 0.0
    free: float = 0.0  # 可用余额
    used: float = 0.0  # 冻结保证金
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "currency": self.currency,
            "total": self.total,
            "free": self.free,
            "used": self.used,
            "unrealizedPnl": self.unrealized_pnl,
            "realizedPnl": self.realized_pnl,
            "timestamp": self.timestamp,
        }


class FakeBalance:
    """
    余额管理器

    管理账户余额，支持：
    - 充值/提现
    - 开仓冻结保证金
    - 平仓释放保证金
    - 更新未实现盈亏
    - 记录已实现盈亏
    """

    def __init__(self, initial_balance: float = 10000.0):
        """
        初始化余额管理器

        Args:
            initial_balance: 初始余额（USDT）
        """
        self._balance = BalanceData(
            currency="USDT",
            total=initial_balance,
            free=initial_balance,
            used=0.0,
        )

    def get_balance(self) -> dict:
        """获取余额"""
        return self._balance.to_dict()

    def get_free_balance(self) -> float:
        """获取可用余额"""
        return self._balance.free

    def get_total_balance(self) -> float:
        """获取总余额"""
        return self._balance.total

    def deposit(self, amount: float) -> None:
        """
        充值

        Args:
            amount: 充值金额
        """
        if amount <= 0:
            raise ValueError("充值金额必须大于0")

        self._balance.total += amount
        self._balance.free += amount
        self._balance.timestamp = time.time()

    def withdraw(self, amount: float) -> bool:
        """
        提现

        Args:
            amount: 提现金额

        Returns:
            是否成功
        """
        if amount <= 0:
            raise ValueError("提现金额必须大于0")

        if amount > self._balance.free:
            return False

        self._balance.total -= amount
        self._balance.free -= amount
        self._balance.timestamp = time.time()
        return True

    def freeze_margin(self, amount: float) -> bool:
        """
        冻结保证金（开仓时调用）

        Args:
            amount: 冻结金额

        Returns:
            是否成功
        """
        if amount <= 0:
            raise ValueError("冻结金额必须大于0")

        if amount > self._balance.free:
            return False

        self._balance.free -= amount
        self._balance.used += amount
        self._balance.timestamp = time.time()
        return True

    def release_margin(self, amount: float) -> None:
        """
        释放保证金（平仓时调用）

        Args:
            amount: 释放金额
        """
        if amount <= 0:
            raise ValueError("释放金额必须大于0")

        if amount > self._balance.used:
            amount = self._balance.used

        self._balance.free += amount
        self._balance.used -= amount
        self._balance.timestamp = time.time()

    def update_unrealized_pnl(self, pnl: float) -> None:
        """
        更新未实现盈亏

        Args:
            pnl: 未实现盈亏
        """
        self._balance.unrealized_pnl = pnl
        self._balance.total = self._balance.free + self._balance.used + pnl
        self._balance.timestamp = time.time()

    def add_realized_pnl(self, pnl: float) -> None:
        """
        添加已实现盈亏（平仓时调用）

        Args:
            pnl: 已实现盈亏
        """
        self._balance.realized_pnl += pnl
        self._balance.free += pnl
        self._balance.total += pnl
        self._balance.timestamp = time.time()

    def deduct_fee(self, fee: float) -> None:
        """
        扣除手续费

        Args:
            fee: 手续费金额
        """
        if fee <= 0:
            return

        self._balance.free -= fee
        self._balance.total -= fee
        self._balance.timestamp = time.time()

    def reset(self, initial_balance: float = 10000.0) -> None:
        """
        重置余额

        Args:
            initial_balance: 初始余额
        """
        self._balance = BalanceData(
            currency="USDT",
            total=initial_balance,
            free=initial_balance,
            used=0.0,
        )
