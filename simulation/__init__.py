"""
Simulation Engine - 模拟交易环境
用于完整测试交易生命周期，不访问真实交易所。

核心组件：
- FakeMarket: 价格模拟
- FakeOrderBook: 订单管理
- FakePosition: 仓位管理
- FakeBalance: 余额管理
- FakeExchange: 交易所接口（兼容 exchange.py）
- FakePositionManager: 快照管理
- SimulationRunner: 场景执行器
- SimulationLog: 可视化日志
- Assertions: 断言检查

使用方式：
    export EXCHANGE_MODE=simulation
    python run_simulation.py
"""
from .fake_market import FakeMarket
from .fake_orderbook import FakeOrderBook
from .fake_position import FakePosition
from .fake_balance import FakeBalance
from .fake_exchange import FakeExchange
from .fake_position_manager import FakePositionManager
from .runner import SimulationRunner
from .logger import SimulationLog
from .assertions import Assertions

# 测试场景
from .scenarios import (
    scenario_market_entry,
    scenario_limit_entry,
    scenario_tp,
    scenario_sl,
    scenario_trailing,
    scenario_watchdog,
    scenario_reconciliation,
    scenario_snapshot,
    scenario_restart,
    scenario_exit_365,
)

__all__ = [
    "FakeMarket",
    "FakeOrderBook",
    "FakePosition",
    "FakeBalance",
    "FakeExchange",
    "FakePositionManager",
    "SimulationRunner",
    "SimulationLog",
    "Assertions",
    # 测试场景
    "scenario_market_entry",
    "scenario_limit_entry",
    "scenario_tp",
    "scenario_sl",
    "scenario_trailing",
    "scenario_watchdog",
    "scenario_reconciliation",
    "scenario_snapshot",
    "scenario_restart",
    "scenario_exit_365",
]
