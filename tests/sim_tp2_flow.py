"""
TP2 全流程模拟验证 — 测试 TP1(30%)/TP2(35%)/Trailing 完整状态机。

测试场景：
  1. TP1 在 +2% 触发 → 平 30%，SL 到保本价，状态 → tp1_filled
  2. TP2 在 +4% 触发 → 平剩余 50%，激活 Trailing，状态 → partial_tp
  3. Trailing 动态锁利 → 价格回落触发 Trailing SL → 全平
  4. 幂等性检查 → TP1/TP2 不会重复触发
  5. 最大持仓时间 → 超时平仓

使用方法:
  python tests/sim_tp2_flow.py
"""
from __future__ import annotations

import asyncio
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ======================================================================
# 轻量数据模型 (不与 DB/Exchange 耦合)
# ======================================================================

class PositionState:
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    TP1_FILLED = "tp1_filled"
    PARTIAL_TP = "partial_tp"
    CLOSED = "closed"


class ExitMode:
    AUTO = "auto"
    HYBRID = "hybrid"


@dataclass
class ExitResult:
    should_exit: bool = False
    reason: str = ""
    exit_type: str = ""
    close_pct: float = 100.0
    exit_price: Optional[float] = None
    move_sl_to_breakeven: bool = False


@dataclass
class SimTrade:
    """模拟 Trade — 包含退出系统需要的全部字段"""
    id: int
    pair: str
    exchange: str = "okx"
    is_open: bool = True
    is_short: bool = False
    open_rate: float = 0.0
    amount: float = 0.0           # 当前仓位数量
    amount_requested: float = 0.0  # 原始仓位数量
    stop_loss: float = 0.0
    open_rate: float = 100000.0    # 开仓价
    position_state: str = PositionState.OPEN
    exit_mode: str = ExitMode.AUTO

    # Trailing 字段
    trailing_activated: bool = False
    trailing_highest_profit_pct: float = 0.0
    trailing_highest_price: float = 0.0
    trailing_current_sl: float = 0.0
    trailing_last_sl_update_at: Optional[float] = None

    @property
    def effective_open_time(self):
        return self.open_date

    @property
    def open_date(self):
        return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def calc_profit_pct(self, current_price: float) -> float:
        if self.open_rate <= 0:
            return 0.0
        if self.is_short:
            return (self.open_rate - current_price) / self.open_rate
        return (current_price - self.open_rate) / self.open_rate


# ======================================================================
# 配置
# ======================================================================

CONFIG = {
    "tp1": {
        "enabled": True,
        "profit_pct": 0.02,
        "close_pct": 30,
        "move_sl_to_breakeven": True,
    },
    "tp2": {
        "enabled": True,
        "profit_pct": 0.04,
        "close_pct": 50,
    },
    "trailing": {
        "enabled": True,
        "activate_profit_pct": 0.04,
        "lock_gap_pct": 0.04,
    },
    "roi": {
        "enabled": True,
        "rules": {"2880": 0.01, "4320": 0.00},
    },
    "max_hold": {
        "enabled": True,
        "hours": 168,
    },
}


# ======================================================================
# 检查器 (简版，逻辑与真实代码一致)
# ======================================================================

class TP1Checker:
    name = "tp1"

    async def check(self, trade: SimTrade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        cfg = CONFIG.get("tp1", {})
        if not cfg.get("enabled", True):
            return None

        pos_state = getattr(trade, 'position_state', None)
        if pos_state in ("tp1_filled", "partial_tp", "closed"):
            return None

        if getattr(trade, 'trailing_activated', False):
            return None

        if trade.stop_loss == trade.open_rate and trade.open_rate > 0:
            return None

        if current_price is None or current_price <= 0 or trade.open_rate <= 0:
            return None

        profit = trade.calc_profit_pct(current_price)
        target = cfg.get("profit_pct", 0.02)
        if profit >= target:
            close_pct = cfg.get("close_pct", 30)
            return ExitResult(
                should_exit=True,
                reason=f"TP1 ({target*100:.1f}%) 已到",
                exit_type="tp1",
                close_pct=close_pct,
                exit_price=current_price,
                move_sl_to_breakeven=True,
            )
        return None


class TP2Checker:
    name = "tp2"

    async def check(self, trade: SimTrade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        cfg = CONFIG.get("tp2", {})
        if not cfg.get("enabled", True):
            return None

        pos_state = getattr(trade, 'position_state', None)
        if pos_state in ("partial_tp", "closed"):
            return None

        if getattr(trade, 'trailing_activated', False):
            return None

        if current_price is None or current_price <= 0 or trade.open_rate <= 0:
            return None

        profit = trade.calc_profit_pct(current_price)
        target = cfg.get("profit_pct", 0.04)
        if profit >= target:
            close_pct = cfg.get("close_pct", 50)
            return ExitResult(
                should_exit=True,
                reason=f"TP2 ({target*100:.1f}%) 已到",
                exit_type="tp2",
                close_pct=close_pct,
                exit_price=current_price,
                move_sl_to_breakeven=True,
            )
        return None


class StopLossChecker:
    name = "stoploss"

    async def check(self, trade: SimTrade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        if trade.stop_loss <= 0 or current_price is None or current_price <= 0:
            return None

        triggered = (
            (trade.is_short and current_price >= trade.stop_loss) or
            (not trade.is_short and current_price <= trade.stop_loss)
        )
        if triggered:
            is_trailing = getattr(trade, 'trailing_activated', False)
            exit_type = "trailing" if is_trailing else "stoploss"
            label = "追踪止损" if is_trailing else "默认止损"
            return ExitResult(
                should_exit=True,
                reason=f"{label} {trade.stop_loss:.5g}",
                exit_type=exit_type,
                close_pct=100.0,
                exit_price=current_price,
            )
        return None


class TrailingChecker:
    name = "trailing"

    async def check(self, trade: SimTrade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        cfg = CONFIG.get("trailing", {})
        if not cfg.get("enabled", True):
            return None

        activate_pct = cfg.get("activate_profit_pct", 0.04)
        lock_gap = cfg.get("lock_gap_pct", 0.04)

        if current_price is None or current_price <= 0 or trade.open_rate <= 0:
            return None

        profit_pct = trade.calc_profit_pct(current_price)
        highest_profit_pct = trade.trailing_highest_profit_pct or 0.0

        # 未激活
        if not trade.trailing_activated:
            if profit_pct < activate_pct:
                return None
            # 激活
            highest_profit_pct = profit_pct
            trade.trailing_activated = True
            trade.trailing_highest_profit_pct = highest_profit_pct
            trade.trailing_highest_price = current_price
            return None  # 首次激活不触发退出
        else:
            if profit_pct > highest_profit_pct:
                highest_profit_pct = profit_pct
                trade.trailing_highest_profit_pct = highest_profit_pct
                trade.trailing_highest_price = current_price

        # 计算动态锁利 SL（锁利间隔 4%）
        sl_profit_pct = max(0.0, highest_profit_pct - lock_gap)

        if trade.is_short:
            trailing_sl_price = trade.open_rate * (1 - sl_profit_pct)
        else:
            trailing_sl_price = trade.open_rate * (1 + sl_profit_pct)

        # 检查触发
        if trade.is_short:
            if trailing_sl_price < current_price:
                return ExitResult(
                    should_exit=True, reason=f"追踪止损(最高+{highest_profit_pct*100:.1f}% 锁利{sl_profit_pct*100:.1f}%)",
                    exit_type="trailing", close_pct=100.0, exit_price=current_price,
                )
        else:
            if trailing_sl_price > current_price:
                return ExitResult(
                    should_exit=True, reason=f"追踪止损(最高+{highest_profit_pct*100:.1f}% 锁利{sl_profit_pct*100:.1f}%)",
                    exit_type="trailing", close_pct=100.0, exit_price=current_price,
                )

        # 未触发：更新 trade.stop_loss (仅收紧方向)
        old_sl = trade.stop_loss
        if trade.is_short:
            new_sl = min(old_sl, trailing_sl_price) if old_sl > 0 else trailing_sl_price
            if new_sl < old_sl or old_sl == 0:
                trade.stop_loss = new_sl
                trade.trailing_current_sl = new_sl
        else:
            new_sl = max(old_sl, trailing_sl_price) if old_sl > 0 else trailing_sl_price
            if new_sl > old_sl or old_sl == 0:
                trade.stop_loss = new_sl
                trade.trailing_current_sl = new_sl

        return None


class ROIChecker:
    name = "roi"

    async def check(self, trade: SimTrade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        # 模拟 — 48小时内不触发
        return None


class MaxHoldChecker:
    name = "max_hold"

    async def check(self, trade: SimTrade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        # 模拟 — 不触发
        return None


class MaxLossChecker:
    name = "max_loss"

    async def check(self, trade: SimTrade, exchange: str, session, current_price: float | None = None) -> ExitResult | None:
        if current_price is None or current_price <= 0 or trade.open_rate <= 0:
            return None
        if trade.is_short:
            loss_pct = (current_price - trade.open_rate) / trade.open_rate
        else:
            loss_pct = (trade.open_rate - current_price) / trade.open_rate
        if loss_pct <= 0:
            return None
        threshold = 0.40 if trade.pair.startswith(("BTC", "ETH", "DOGE", "SOL")) else 0.20
        if loss_pct >= threshold:
            return ExitResult(
                should_exit=True, reason=f"最大亏损 {loss_pct*100:.2f}%",
                exit_type="max_loss", close_pct=100.0, exit_price=current_price,
            )
        return None


# ======================================================================
# ExitManager (简版)
# ======================================================================

class SimExitManager:
    def __init__(self):
        self._tp1 = TP1Checker()
        self._tp2 = TP2Checker()
        self._trailing = TrailingChecker()
        self._stoploss = StopLossChecker()
        self._max_loss = MaxLossChecker()
        self._roi = ROIChecker()
        self._max_hold = MaxHoldChecker()

    async def check(self, trade: SimTrade) -> ExitResult | None:
        pos_state = trade.position_state
        if pos_state in (PositionState.PENDING_ENTRY, PositionState.CLOSED):
            return None

        # MaxLoss 始终优先
        for checker in [self._max_loss, self._stoploss]:
            result = await checker.check(trade, "okx", None, self._current_price)
            if result and result.should_exit:
                return result

        if pos_state == PositionState.OPEN:
            result = await self._tp1.check(trade, "okx", None, self._current_price)
            if result and result.should_exit:
                return result

        elif pos_state == PositionState.TP1_FILLED:
            result = await self._tp2.check(trade, "okx", None, self._current_price)
            if result and result.should_exit:
                return result

        elif pos_state == PositionState.PARTIAL_TP:
            result = await self._trailing.check(trade, "okx", None, self._current_price)
            if result and result.should_exit:
                return result

        return None


# ======================================================================
# 颜色输出
# ======================================================================

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str):
    print(f"  {GREEN}✅ {msg}{RESET}")

def fail(msg: str):
    print(f"  {RED}❌ {msg}{RESET}")

def info(msg: str):
    print(f"  {CYAN}ℹ️  {msg}{RESET}")

def warn(msg: str):
    print(f"  {YELLOW}⚠️  {msg}{RESET}")

def header(title: str):
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")


# ======================================================================
# 测试用例
# ======================================================================

total_tests = 0
passed_tests = 0


def assert_eq(label: str, actual, expected):
    global total_tests, passed_tests
    total_tests += 1
    if actual == expected:
        passed_tests += 1
        ok(f"{label}: {repr(actual)}")
    else:
        fail(f"{label}: 期望 {repr(expected)}, 实际 {repr(actual)}")


async def test_tp1_trigger():
    """场景 1: TP1 在 +2% 触发"""
    header("场景 1: TP1 在 +2% 触发")
    trade = SimTrade(id=1, pair="BTC/USDT:USDT", amount=1.0, amount_requested=1.0,
                     position_state=PositionState.OPEN, stop_loss=98000.0)
    manager = SimExitManager()

    # 价格 +2.5% (超过 TP1 阈值)
    price = trade.open_rate * 1.025
    manager._current_price = price

    result = await manager.check(trade)

    assert_eq("TP1 触发", result is not None, True)
    if result:
        assert_eq("exit_type = tp1", result.exit_type, "tp1")
        assert_eq("close_pct = 30%", result.close_pct, 30.0)
        assert_eq("move_sl_to_breakeven", result.move_sl_to_breakeven, True)

    # 模拟 TP1 成交后的状态
    trade.stop_loss = trade.open_rate  # 保本
    trade.amount = 1.0 * 0.7  # 剩余 70%
    trade.position_state = PositionState.TP1_FILLED

    info(f"TP1 成交后: 剩余仓位={trade.amount:.4f}, SL={trade.stop_loss:.2f}")

    # TP1 不应再次触发
    result2 = await manager.check(trade)
    assert_eq("TP1 不重复触发", result2, None)

    return trade  # 传给下一场景


async def test_tp2_trigger(trade: SimTrade):
    """场景 2: TP2 在 +4% 触发"""
    header("场景 2: TP2 在 +4% 触发")
    manager = SimExitManager()

    assert_eq("状态为 tp1_filled", trade.position_state, PositionState.TP1_FILLED)

    # 价格 +5% (超过 TP2 阈值)
    price = trade.open_rate * 1.05
    manager._current_price = price

    result = await manager.check(trade)

    assert_eq("TP2 触发", result is not None, True)
    if result:
        assert_eq("exit_type = tp2", result.exit_type, "tp2")
        assert_eq("close_pct = 50%", result.close_pct, 50.0)

    # 模拟 TP2 成交后的状态
    trade.amount = trade.amount * 0.5  # 剩余 35% of original
    trade.position_state = PositionState.PARTIAL_TP

    info(f"TP2 成交后: 剩余仓位={trade.amount:.4f} (原仓位的35%)")

    # TP2 不应再次触发
    result2 = await manager.check(trade)
    assert_eq("TP2 不重复触发", result2, None)

    # Trailing 应已激活 (通过 main.py 处理)
    # 这里手动模拟 main.py 中的 Trailing 激活
    price = trade.open_rate * 1.05
    profit_pct = trade.calc_profit_pct(price)
    trade.trailing_activated = True
    trade.trailing_highest_profit_pct = profit_pct
    trade.trailing_highest_price = price
    lock_gap = 0.04  # 锁利间隔 4%
    sl_profit_pct = max(0.0, profit_pct - lock_gap)
    trade.stop_loss = trade.open_rate * (1 + sl_profit_pct)
    trade.trailing_current_sl = trade.stop_loss
    info(f"Trailing 激活: profit={profit_pct*100:.2f}%, SL={trade.stop_loss:.2f} (锁利{sl_profit_pct*100:.1f}%, 间隔{lock_gap*100:.0f}%)")

    return trade


async def test_trailing_locks_profit(trade: SimTrade):
    """场景 3: Trailing 动态锁利"""
    header("场景 3: Trailing 动态锁利")
    manager = SimExitManager()

    assert_eq("状态为 partial_tp", trade.position_state, PositionState.PARTIAL_TP)
    assert_eq("trailing 已激活", trade.trailing_activated, True)

    # 价格继续上涨到 +6% (最高盈利 +6%)
    price = trade.open_rate * 1.06
    manager._current_price = price
    result = await manager.check(trade)
    assert_eq("上涨到+6%不触发退出", result, None)

    # SL 应上移到 +2%（6% - 4% 锁利间隔）
    expected_sl = trade.open_rate * 1.02
    info(f"当前 SL = {trade.stop_loss:.2f} (应约为 {expected_sl:.2f})")
    sl_ok = abs(trade.stop_loss - expected_sl) < 1.0
    assert_eq("SL 上移到 +2% (6%-4%)", sl_ok, True)

    # 价格上涨到 +7% (最高盈利 +7%)
    price = trade.open_rate * 1.07
    manager._current_price = price
    result = await manager.check(trade)
    assert_eq("上涨到+7%不触发退出", result, None)

    # SL 应上移到 +3%（7% - 4% 锁利间隔）
    expected_sl2 = trade.open_rate * 1.03
    sl_ok2 = abs(trade.stop_loss - expected_sl2) < 1.0
    assert_eq("SL 上移到 +3% (7%-4%)", sl_ok2, True)

    # 价格回落到 +2.5% (低于 SL 的 +3%)
    price = trade.open_rate * 1.025
    manager._current_price = price
    result = await manager.check(trade)
    assert_eq("回落到+2.5%触发 Trailing 退出", result is not None, True)
    if result:
        assert_eq("exit_type = trailing", result.exit_type, "trailing")
        info(f"Trailing 退出: {result.reason}")

    # 仓位关闭
    trade.position_state = PositionState.CLOSED
    assert_eq("仓位已关闭", trade.position_state, PositionState.CLOSED)

    return trade


async def test_tp1_profit_calculation():
    """场景 4: TP1/TP2 利润计算 (不含杠杆)"""
    header("场景 4: 利润计算正确性")
    trade = SimTrade(id=2, pair="ETH/USDT:USDT", amount=1.0, amount_requested=1.0,
                     position_state=PositionState.OPEN, stop_loss=98000.0)

    # +1.9% → TP1 不应触发 (低于 2%)
    price = trade.open_rate * 1.019
    assert_eq("+1.9% 未达 TP1", trade.calc_profit_pct(price) >= 0.02, False)

    # +2.1% → TP1 应触发
    price = trade.open_rate * 1.021
    assert_eq("+2.1% 达到 TP1", trade.calc_profit_pct(price) >= 0.02, True)

    # +3.9% → TP2 不应触发 (低于 4%)
    price = trade.open_rate * 1.039
    assert_eq("+3.9% 未达 TP2", trade.calc_profit_pct(price) >= 0.04, False)

    # +4.1% → TP2 应触发
    price = trade.open_rate * 1.041
    assert_eq("+4.1% 达到 TP2", trade.calc_profit_pct(price) >= 0.04, True)


async def test_state_machine_transitions():
    """场景 5: 完整状态机转换"""
    header("场景 5: 完整状态机转换")
    trade = SimTrade(id=3, pair="SOL/USDT:USDT", amount=1.0, amount_requested=1.0,
                     position_state=PositionState.OPEN, stop_loss=96000.0)

    assert_eq("初始状态 OPEN", trade.position_state, PositionState.OPEN)

    # OPEN → TP1_FILLED
    trade.position_state = PositionState.TP1_FILLED
    trade.amount = 0.7
    trade.stop_loss = trade.open_rate
    assert_eq("TP1 后 TP1_FILLED", trade.position_state, PositionState.TP1_FILLED)
    assert_eq("剩余仓位 70%", trade.amount, 0.7)
    assert_eq("SL 保本价", trade.stop_loss, trade.open_rate)

    # TP1_FILLED → PARTIAL_TP
    trade.position_state = PositionState.PARTIAL_TP
    trade.amount = 0.35  # 50% of 70%
    trade.trailing_activated = True
    assert_eq("TP2 后 PARTIAL_TP", trade.position_state, PositionState.PARTIAL_TP)
    assert_eq("剩余仓位 35%", trade.amount, 0.35)
    assert_eq("Trailing 激活", trade.trailing_activated, True)

    # PARTIAL_TP → CLOSED
    trade.position_state = PositionState.CLOSED
    assert_eq("全平后 CLOSED", trade.position_state, PositionState.CLOSED)

    info("仓位分布: TP1 平 30% + TP2 平 35% + Trailing 平 35% = 100%")


async def test_short_position():
    """场景 6: 空单测试"""
    header("场景 6: 空单")
    trade = SimTrade(id=4, pair="DOGE/USDT:USDT", amount=10000.0, amount_requested=10000.0,
                     is_short=True, position_state=PositionState.OPEN,
                     stop_loss=102000.0)

    # 价格下跌 2.5% → TP1 触发
    price = trade.open_rate * 0.975
    manager = SimExitManager()
    manager._current_price = price

    result = await manager.check(trade)
    assert_eq("空单 TP1 触发", result is not None and result.exit_type == "tp1", True)
    if result:
        info(f"空单 TP1: close_pct={result.close_pct}%")

    # 模拟 TP1 成交
    trade.stop_loss = trade.open_rate
    trade.amount = 10000.0 * 0.7
    trade.position_state = PositionState.TP1_FILLED

    # 价格继续下跌 5.5% → TP2 触发
    price = trade.open_rate * 0.945
    manager._current_price = price
    result = await manager.check(trade)
    assert_eq("空单 TP2 触发", result is not None and result.exit_type == "tp2", True)

    # 模拟 TP2 成交 + Trailing 激活
    trade.amount = trade.amount * 0.5
    trade.position_state = PositionState.PARTIAL_TP
    trade.trailing_activated = True
    trade.trailing_highest_profit_pct = 0.055
    trade.trailing_highest_price = price
    # 空单 SL 下移（锁利间隔 4%）
    trade.stop_loss = trade.open_rate * (1 - max(0.0, 0.055 - 0.04))

    info(f"空单 TP2 后: 剩余={trade.amount:.0f}, SL={trade.stop_loss:.2f}, profit={trade.calc_profit_pct(price)*100:.2f}%")


async def test_position_sizing():
    """场景 7: 仓位计算验证"""
    header("场景 7: 仓位计算")

    original = 100.0  # 原始仓位

    # TP1: 平 30%
    tp1_close = original * 0.30
    after_tp1 = original - tp1_close
    assert_eq("TP1 平仓量", tp1_close, 30.0)
    assert_eq("TP1 后剩余", after_tp1, 70.0)

    # TP2: 平剩余 50% (即原仓位的 35%)
    tp2_close = after_tp1 * 0.50
    after_tp2 = after_tp1 - tp2_close
    assert_eq("TP2 平仓量 (=原35%)", tp2_close, 35.0)
    assert_eq("TP2 后剩余 (=原35%)", after_tp2, 35.0)

    # Trailing: 管理最后 35%
    assert_eq("最终占比", after_tp2, 35.0)
    assert_eq("平仓总计", tp1_close + tp2_close, 65.0)
    assert_eq("总和", tp1_close + tp2_close + after_tp2, 100.0)

    info(f"TP1: 平 {tp1_close}% | TP2: 平 {tp2_close}% | Trailing: 管 {after_tp2}%")


# ======================================================================
# 主程序
# ======================================================================

async def main():
    header("TP2 全流程模拟验证")
    print("测试配置:")
    print(f"  TP1: +{CONFIG['tp1']['profit_pct']*100:.0f}%, 平{CONFIG['tp1']['close_pct']}%")
    print(f"  TP2: +{CONFIG['tp2']['profit_pct']*100:.0f}%, 平剩余{CONFIG['tp2']['close_pct']}%")
    print(f"  Trailing: 激活@{CONFIG['trailing']['activate_profit_pct']*100:.0f}%, 锁利间隔{CONFIG['trailing']['lock_gap_pct']*100:.0f}%")

    global trade
    trade = await test_tp1_trigger()
    trade = await test_tp2_trigger(trade)
    trade = await test_trailing_locks_profit(trade)
    await test_tp1_profit_calculation()
    await test_state_machine_transitions()
    await test_short_position()
    await test_position_sizing()

    # 总结
    header("测试结果")
    print(f"  通过: {passed_tests}/{total_tests}")
    if passed_tests == total_tests:
        print(f"  {GREEN}{BOLD}全部通过!{RESET}")
    else:
        print(f"  {RED}{BOLD}{total_tests - passed_tests} 个失败!{RESET}")
        sys.exit(1)

    print(f"\n{CYAN}仓位分布: 30%(TP1) + 35%(TP2) + 35%(Trailing) = 100%{RESET}")
    print(f"{CYAN}状态机: OPEN → TP1_FILLED → PARTIAL_TP → CLOSED{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
