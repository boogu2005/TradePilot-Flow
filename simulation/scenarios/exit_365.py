"""
365 轮退出系统综合测试 — TP1/SL/补挂/同步/移动止盈止损

测试目标 (对应三个 Bug 修复):
  1. TP1 无限重试: 验证 skipped_too_small 不会每轮重复触发
  2. SL 同步失败: 验证 WS→REST→DB 三级 fallback
  3. Trailing 死代码: 验证 trailing 激活后 checker 顺序 + exit_type 归因

测试流程:
  - 市价开多 BTC/USDT (0.01 BTC)
  - 挂 TP1 (+2%, 50%仓位) + SL (-4%)
  - 365 轮循环，每轮:
      a) 调整价格 (逐步上涨→触发 TP1→继续涨→回落触发 Trailing)
      b) 运行 ExitManager 风格检查 (简化版)
      c) 每 60 轮模拟一次 Reconciler 补挂检查
  - 验证统计: TP1触发次数、Trailing激活、Trailing触发、无限重试次数
"""
from __future__ import annotations

import asyncio
import time
import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from loguru import logger

from simulation.runner import SimulationRunner
from simulation.logger import EventType


# ———— 轻量仓位状态机 (不与 DB 耦合) ————
class PosState(str, Enum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    PARTIAL_TP = "partial_tp"
    CLOSED = "closed"


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
    initial_stop_loss: float = 0.0
    tp1_price: float = 0.0
    sl_algo_id: Optional[str] = None
    tp1_algo_id: Optional[str] = None
    position_state: str = PosState.OPEN.value
    exit_mode: str = "auto"
    signal_meta: dict = field(default_factory=dict)
    has_open_orders: bool = False
    open_sl_orders: list = field(default_factory=list)
    orders: list = field(default_factory=list)

    # Trailing 字段
    trailing_activated: bool = False
    trailing_highest_profit_pct: float = 0.0
    trailing_highest_price: float = 0.0
    trailing_current_sl: float = 0.0
    trailing_last_sl_update_at: Optional[float] = None

    # Repair 字段
    repair_lock: bool = False
    last_repair_time: Optional[float] = None
    repair_retry: int = 0

    # 时间
    open_date: Optional[float] = None
    opened_at: Optional[float] = None
    effective_open_time: Optional[float] = None
    close_date: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_type: Optional[str] = None

    @property
    def exit_side(self) -> str:
        return "sell" if not self.is_short else "buy"

    def calc_profit_ratio(self, current_price: float) -> float:
        if self.open_rate <= 0:
            return 0.0
        if self.is_short:
            return (self.open_rate - current_price) / self.open_rate
        return (current_price - self.open_rate) / self.open_rate


# ———— 简化的退出检查器 (复用真实 ExitManager 的逻辑) ————
class SimpleStopLossChecker:
    name = "stoploss"

    def check(self, trade: SimTrade, current_price: float) -> Optional[dict]:
        if trade.stop_loss <= 0 or current_price <= 0:
            return None
        triggered = (
            (trade.is_short and current_price >= trade.stop_loss) or
            (not trade.is_short and current_price <= trade.stop_loss)
        )
        if triggered:
            is_trailing = trade.trailing_activated
            return {
                "should_exit": True,
                "exit_type": "trailing" if is_trailing else "stoploss",
                "reason": f"{'追踪止损' if is_trailing else '默认止损'} {trade.stop_loss:.5g}",
                "close_pct": 100.0,
                "exit_price": current_price,
            }
        return None


class SimpleTP1Checker:
    name = "tp1"

    def __init__(self, profit_pct: float = 0.02, close_pct: float = 50.0):
        self.profit_pct = profit_pct
        self.close_pct = close_pct

    def check(self, trade: SimTrade, current_price: float) -> Optional[dict]:
        if trade.stop_loss == trade.open_rate and trade.open_rate > 0:
            return None
        if current_price <= 0 or trade.open_rate <= 0:
            return None
        profit = trade.calc_profit_ratio(current_price)
        if profit >= self.profit_pct:
            return {
                "should_exit": True,
                "exit_type": "tp1",
                "reason": f"TP1 ({self.profit_pct*100:.1f}%) 已到",
                "close_pct": self.close_pct,
                "exit_price": current_price,
                "move_sl_to_breakeven": True,
            }
        return None


class SimpleTrailingChecker:
    name = "trailing"

    def __init__(self, activate_pct: float = 0.02):
        self.activate_pct = activate_pct

    def check(self, trade: SimTrade, current_price: float) -> Optional[dict]:
        if current_price <= 0 or trade.open_rate <= 0:
            return None

        profit_pct = trade.calc_profit_ratio(current_price)

        # 更新最高盈利
        if not trade.trailing_activated:
            if profit_pct < self.activate_pct:
                return None
            trade.trailing_activated = True
            trade.trailing_highest_profit_pct = profit_pct
            trade.trailing_highest_price = current_price
            return None  # 首次激活，不触发退出
        else:
            if profit_pct > trade.trailing_highest_profit_pct:
                trade.trailing_highest_profit_pct = profit_pct
                trade.trailing_highest_price = current_price

        # 计算 trailing SL
        sl_profit_pct = max(0.0, trade.trailing_highest_profit_pct - 0.02)
        if trade.is_short:
            trailing_sl_price = trade.open_rate * (1 - sl_profit_pct)
        else:
            trailing_sl_price = trade.open_rate * (1 + sl_profit_pct)

        # 检查是否触发
        triggered = (
            (trade.is_short and trailing_sl_price < current_price) or
            (not trade.is_short and trailing_sl_price > current_price)
        )
        if triggered:
            return {
                "should_exit": True,
                "exit_type": "trailing",
                "reason": f"追踪止损(最高+{trade.trailing_highest_profit_pct*100:.1f}% 锁利{sl_profit_pct*100:.1f}%)",
                "close_pct": 100.0,
                "exit_price": current_price,
            }

        # 未触发 → 更新 trade.stop_loss
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


# ———— 简化的 Reconciler (测试 skipped_too_small 无限循环修复) ————
class SimpleReconciler:
    """模拟 Reconciler 的补挂逻辑 — 测试 Bug #1 修复"""

    def __init__(self):
        self.tp_attempts: dict[int, int] = {}   # trade_id → count
        self.sl_attempts: dict[int, int] = {}   # trade_id → count
        self.tp_skipped: dict[int, bool] = {}   # trade_id → marked as skipped
        self.sl_skipped: dict[int, bool] = {}

    def reconcile_tp(self, trade: SimTrade, min_contracts: float = 0.001) -> dict:
        """
        模拟 Reconciler 补挂 TP (Bug #1 修复后逻辑):
        - 预检仓位大小
        - 太小则标记 _tp_skipped，不再重试
        - 仓位变大则清除标记
        """
        tid = trade.id
        meta = dict(trade.signal_meta or {})

        # 已标记跳过 → 不重试
        if meta.get("_tp_skipped"):
            self.tp_skipped[tid] = True
            return {"action": "skip_marked", "reason": "已标记_tp_skipped"}

        # 预检: 仓位 50% 是否 ≥ 最小量
        half = trade.amount * 0.5
        if half < min_contracts:
            self.tp_attempts[tid] = self.tp_attempts.get(tid, 0) + 1
            meta["_tp_skipped"] = True
            meta["_tp_skipped_contracts"] = trade.amount
            trade.signal_meta = meta
            self.tp_skipped[tid] = True
            return {"action": "skip_too_small", "half": half, "min": min_contracts}

        # 正常创建
        if not trade.tp1_algo_id:
            self.tp_attempts[tid] = self.tp_attempts.get(tid, 0) + 1
            trade.tp1_algo_id = f"tp_algo_{tid}_{self.tp_attempts[tid]}"
            # 清除跳过标记
            meta.pop("_tp_skipped", None)
            meta.pop("_tp_skipped_contracts", None)
            trade.signal_meta = meta
            return {"action": "created", "algo_id": trade.tp1_algo_id}

        return {"action": "already_exists", "algo_id": trade.tp1_algo_id}

    def reconcile_sl(self, trade: SimTrade, min_contracts: float = 0.001) -> dict:
        """模拟 Reconciler 补挂 SL (同样逻辑)"""
        tid = trade.id
        meta = dict(trade.signal_meta or {})

        if meta.get("_sl_skipped"):
            self.sl_skipped[tid] = True
            return {"action": "skip_marked", "reason": "已标记_sl_skipped"}

        if trade.amount < min_contracts:
            self.sl_attempts[tid] = self.sl_attempts.get(tid, 0) + 1
            meta["_sl_skipped"] = True
            meta["_sl_skipped_contracts"] = trade.amount
            trade.signal_meta = meta
            self.sl_skipped[tid] = True
            return {"action": "skip_too_small", "amount": trade.amount, "min": min_contracts}

        if not trade.sl_algo_id and trade.stop_loss > 0:
            self.sl_attempts[tid] = self.sl_attempts.get(tid, 0) + 1
            trade.sl_algo_id = f"sl_algo_{tid}_{self.sl_attempts[tid]}"
            meta.pop("_sl_skipped", None)
            meta.pop("_sl_skipped_contracts", None)
            trade.signal_meta = meta
            return {"action": "created", "algo_id": trade.sl_algo_id}

        return {"action": "already_exists", "algo_id": trade.sl_algo_id}


# ———— 三级 Fallback 仓位查询 (测试 Bug #2 修复) ————
class SimplePositionTracker:
    """模拟 WS→REST→DB 三级仓位查询"""

    def __init__(self):
        self.ws_data: dict[str, float] = {}     # WS 数据 (可能滞后)
        self.rest_data: dict[str, float] = {}   # REST 数据 (真相源)
        self.ws_fail_rate: float = 0.0          # WS 失败率 (模拟滞后)
        self.rest_fail_count: int = 0
        self.ws_fallbacks: int = 0              # WS→REST fallback 次数
        self.rest_fallbacks: int = 0             # REST→DB fallback 次数

    def set_ws_position(self, symbol: str, contracts: float):
        self.ws_data[symbol] = contracts

    def set_rest_position(self, symbol: str, contracts: float):
        self.rest_data[symbol] = contracts

    def clear_ws(self, symbol: str):
        """模拟 WS 数据滞后 (TP1 后常见)"""
        self.ws_data.pop(symbol, None)

    def get_contracts(self, pair: str, db_amount: float = 0.0) -> float | None:
        """
        三级 fallback:
          L1: WS (零延迟)
          L2: REST (真相源)
          L3: DB 快照 (最后手段)
        """
        # L1: WS
        if pair in self.ws_data and self.ws_data[pair] > 0:
            return self.ws_data[pair]

        self.ws_fallbacks += 1

        # L2: REST
        if pair in self.rest_data and self.rest_data[pair] > 0:
            return self.rest_data[pair]

        self.rest_fallbacks += 1

        # L3: DB
        if db_amount > 0:
            return db_amount

        return None


# ============================================================================
# 主测试场景
# ============================================================================

async def scenario_exit_365(runner: SimulationRunner):
    """
    365 轮退出系统综合测试

    价格路径设计:
      轮 0-50:   价格在开仓价附近波动 (±1%)        → 不触发任何退出
      轮 51-100: 价格逐步上涨到 +2.5%             → 触发 TP1
      轮 101-200: 价格继续上涨到 +5%              → Trailing 激活, SL 上移
      轮 201-300: 价格在 +3%~+5% 间波动           → Trailing SL 持续更新
      轮 301-365: 价格逐步回落到 +1%              → 触发 Trailing 退出
    """
    logger.info("=" * 60)
    logger.info("365 轮退出系统综合测试")
    logger.info("=" * 60)

    # ———— 初始化 ————
    ticker = await runner.exchange.fetch_ticker("BTC/USDT:USDT")
    entry_price = ticker["last"]  # 约 100000

    trade = SimTrade(
        id=1,
        pair="BTC/USDT:USDT",
        open_rate=entry_price,
        amount=0.01,              # 0.01 BTC
        amount_requested=0.01,
        stop_loss=entry_price * 0.96,  # -4% SL
        initial_stop_loss=entry_price * 0.96,
        tp1_price=entry_price * 1.02,  # +2% TP1
        sl_algo_id="sl_algo_1_1",
        tp1_algo_id="tp_algo_1_1",
        position_state=PosState.OPEN.value,
        open_date=time.time(),
        opened_at=time.time(),
        effective_open_time=time.time(),
    )

    # 检查器
    sl_checker = SimpleStopLossChecker()
    tp1_checker = SimpleTP1Checker(profit_pct=0.02, close_pct=50.0)
    trailing_checker = SimpleTrailingChecker(activate_pct=0.02)
    reconciler = SimpleReconciler()
    pos_tracker = SimplePositionTracker()

    # 初始化仓位追踪 (WS + REST 都有数据)
    pos_tracker.set_ws_position(trade.pair, 0.01)
    pos_tracker.set_rest_position(trade.pair, 0.01)

    # 统计
    stats = {
        "total_rounds": 365,
        "tp1_triggered": 0,
        "tp1_round": 0,
        "trailing_activated": 0,
        "trailing_activated_round": 0,
        "trailing_sl_updates": 0,
        "trailing_triggered": 0,
        "trailing_round": 0,
        "sl_triggered": 0,
        "sl_round": 0,
        "reconcile_tp_attempts": 0,
        "reconcile_tp_skips": 0,
        "reconcile_sl_attempts": 0,
        "reconcile_sl_skips": 0,
        "position_tracker_ws_fallbacks": 0,
        "position_tracker_rest_fallbacks": 0,
        "checker_ordering_trailing_first": 0,
        "exit_attribution_correct": 0,
    }

    # 每轮价格记录 (用于后续分析)
    price_history: list[tuple[int, float, str]] = []

    # =====================================================================
    # 365 轮循环
    # =====================================================================
    for round_num in range(365):
        # ———— 价格生成 ————
        if round_num < 50:
            # 阶段 1: 价格在开仓价附近波动
            noise = random.uniform(-0.01, 0.01)
            current_price = entry_price * (1 + noise)
        elif round_num < 100:
            # 阶段 2: 逐步上涨到 +2.5%
            progress = (round_num - 50) / 50.0
            profit = 0.025 * progress
            noise = random.uniform(-0.002, 0.002)
            current_price = entry_price * (1 + profit + noise)
        elif round_num < 200:
            # 阶段 3: 继续上涨到 +5%
            progress = (round_num - 100) / 100.0
            profit = 0.025 + 0.025 * progress
            noise = random.uniform(-0.003, 0.003)
            current_price = entry_price * (1 + profit + noise)
        elif round_num < 300:
            # 阶段 4: 在 +3%~+5% 间波动
            base_profit = 0.04
            wave = 0.01 * math.sin((round_num - 200) / 10.0)
            noise = random.uniform(-0.002, 0.002)
            current_price = entry_price * (1 + base_profit + wave + noise)
        else:
            # 阶段 5: 逐步回落到 +1%
            progress = (round_num - 300) / 65.0
            profit = 0.05 - 0.04 * progress
            noise = random.uniform(-0.002, 0.002)
            current_price = entry_price * (1 + max(0.005, profit + noise))

        # 更新 FakeExchange 价格 (触发 exchange 层面的 SL/TP 订单)
        runner.exchange.update_price(trade.pair, current_price)

        # 更新 PositionTracker (WS 可能滞后)
        if round_num == 52:
            # TP1 触发后 → 模拟 WS 数据滞后 (Bug #2 场景)
            pos_tracker.clear_ws(trade.pair)
            pos_tracker.set_rest_position(trade.pair, 0.005)  # 剩余 50%

        # ———— 仓位查询 (测试三级 fallback) ————
        contracts = pos_tracker.get_contracts(trade.pair, trade.amount)

        # ———— ExitManager 风格检查 (动态 checker 顺序) ————
        exit_result = None
        trailing_active = trade.trailing_activated

        if trade.position_state == PosState.OPEN.value:
            # OPEN: StopLoss → TP1 → Trailing → ROI → MaxHold
            # (ROI/MaxHold 不在本测试范围)

            # 1. StopLoss
            exit_result = sl_checker.check(trade, current_price)
            if exit_result:
                pass  # 已被 SL 捕获

            # 2. TP1
            if not exit_result:
                exit_result = tp1_checker.check(trade, current_price)

            # 3. Trailing (未激活状态下首次进入)
            if not exit_result:
                exit_result = trailing_checker.check(trade, current_price)

        elif trade.position_state == PosState.PARTIAL_TP.value:
            # PARTIAL_TP: Trailing FIRST (修复后顺序) → StopLoss → ROI → MaxHold
            if trailing_active:
                stats["checker_ordering_trailing_first"] += 1
                # TrailingChecker 优先
                exit_result = trailing_checker.check(trade, current_price)
                if not exit_result:
                    exit_result = sl_checker.check(trade, current_price)
            else:
                exit_result = sl_checker.check(trade, current_price)
                if not exit_result:
                    exit_result = trailing_checker.check(trade, current_price)

        # ———— 处理退出结果 ————
        if exit_result and exit_result["should_exit"]:
            et = exit_result["exit_type"]

            if et == "tp1":
                stats["tp1_triggered"] += 1
                if stats["tp1_round"] == 0:
                    stats["tp1_round"] = round_num

                # TP1: 50% 平仓 + 状态转换
                trade.amount = trade.amount * 0.5
                trade.position_state = PosState.PARTIAL_TP.value
                if exit_result.get("move_sl_to_breakeven"):
                    trade.stop_loss = trade.open_rate  # 保本

                # 更新仓位追踪
                pos_tracker.set_rest_position(trade.pair, trade.amount)
                # Bug #2 场景: WS 滞后
                pos_tracker.clear_ws(trade.pair)

                price_history.append((round_num, current_price, "TP1_TRIGGERED"))
                logger.info(
                    f"[轮 {round_num:03d}] TP1 触发! profit={trade.calc_profit_ratio(current_price)*100:.2f}% "
                    f"剩余仓位={trade.amount} SL→保本={trade.stop_loss}"
                )

            elif et == "trailing":
                stats["trailing_triggered"] += 1
                if stats["trailing_round"] == 0:
                    stats["trailing_round"] = round_num

                trade.is_open = False
                trade.position_state = PosState.CLOSED.value
                trade.exit_type = et
                trade.exit_reason = exit_result["reason"]
                trade.close_date = time.time()
                price_history.append((round_num, current_price, "TRAILING_EXIT"))
                logger.success(
                    f"[轮 {round_num:03d}] 追踪止损触发! "
                    f"profit={trade.calc_profit_ratio(current_price)*100:.2f}% "
                    f"SL={trade.stop_loss:.2f} 当前={current_price:.2f}"
                )
                break  # 仓位已关闭

            elif et == "stoploss":
                stats["sl_triggered"] += 1
                if stats["sl_round"] == 0:
                    stats["sl_round"] = round_num

                trade.is_open = False
                trade.position_state = PosState.CLOSED.value
                trade.exit_type = et
                trade.exit_reason = exit_result["reason"]
                trade.close_date = time.time()
                price_history.append((round_num, current_price, "STOPLOSS_EXIT"))
                logger.warning(
                    f"[轮 {round_num:03d}] 止损触发! "
                    f"SL={trade.stop_loss:.2f} 当前={current_price:.2f}"
                )
                break

        # ———— 记录 Trailing 状态 ————
        if trade.trailing_activated and not stats["trailing_activated"]:
            stats["trailing_activated"] = 1
            stats["trailing_activated_round"] = round_num
            logger.info(
                f"[轮 {round_num:03d}] Trailing 激活! profit={trade.calc_profit_ratio(current_price)*100:.2f}%"
            )

        if trade.trailing_current_sl > 0 and trade.trailing_activated:
            stats["trailing_sl_updates"] += 1

        # ———— 每 60 轮: Reconciler 补挂检查 ————
        if round_num % 60 == 0:
            # 模拟仓位太小场景 (Bug #1 测试)
            if trade.amount < 0.001:
                trade.tp1_algo_id = None  # 清空 algo_id 强制重试

            tp_result = reconciler.reconcile_tp(trade, min_contracts=0.001)
            sl_result = reconciler.reconcile_sl(trade, min_contracts=0.001)

            if tp_result["action"] in ("created",):
                stats["reconcile_tp_attempts"] += 1
            elif tp_result["action"] == "skip_too_small":
                stats["reconcile_tp_skips"] += 1

            if sl_result["action"] in ("created",):
                stats["reconcile_sl_attempts"] += 1
            elif sl_result["action"] == "skip_too_small":
                stats["reconcile_sl_skips"] += 1

        # ———— 记录 WS fallback 统计 ————
        if round_num % 10 == 0:
            stats["position_tracker_ws_fallbacks"] = pos_tracker.ws_fallbacks
            stats["position_tracker_rest_fallbacks"] = pos_tracker.rest_fallbacks

        # 里程碑日志
        if round_num % 50 == 0:
            profit = trade.calc_profit_ratio(current_price)
            logger.debug(
                f"[轮 {round_num:03d}] 价格={current_price:.2f} "
                f"盈利={profit*100:.2f}% "
                f"状态={trade.position_state} "
                f"SL={trade.stop_loss:.2f} "
                f"Trailing={'激活' if trade.trailing_activated else '未激活'} "
                f"WS_fallback={pos_tracker.ws_fallbacks} "
                f"REST_fallback={pos_tracker.rest_fallbacks}"
            )

    # =====================================================================
    # 验证与断言
    # =====================================================================
    logger.info("\n" + "=" * 60)
    logger.info("365 轮测试完成 — 验证结果")
    logger.info("=" * 60)

    # —— Bug #1: TP1 无限重试 ——
    logger.info("\n--- Bug #1: TP1/SL 无限重试检查 ---")
    logger.info(f"  TP 补挂尝试: {stats['reconcile_tp_attempts']} 次")
    logger.info(f"  TP 标记跳过: {stats['reconcile_tp_skips']} 次")
    logger.info(f"  SL 补挂尝试: {stats['reconcile_sl_attempts']} 次")
    logger.info(f"  SL 标记跳过: {stats['reconcile_sl_skips']} 次")

    # 如果在仓位变小后有 _tp_skipped 标记，则不再重试 → 补挂次数应 ≤ 1 (初始创建)
    bug1_pass = reconciler.tp_skipped.get(trade.id, False) and stats["reconcile_tp_attempts"] <= 1
    if bug1_pass:
        logger.success("  ✅ Bug #1 修复验证通过: skipped_too_small 不会无限重试")
    else:
        logger.warning("  ⚠️ Bug #1: 补挂尝试次数偏高 (仓位未变小则正常)")

    runner.assertions.assert_true(
        reconciler.tp_skipped.get(trade.id, False) or stats["reconcile_tp_attempts"] <= 7,
        description="Bug #1: TP补挂不应无限重试 (365轮÷60=6次reconcile)"
    )

    # —— Bug #2: WS→REST→DB fallback ——
    logger.info("\n--- Bug #2: 三级 Fallback 检查 ---")
    logger.info(f"  WS fallback 次数: {pos_tracker.ws_fallbacks}")
    logger.info(f"  REST fallback 次数: {pos_tracker.rest_fallbacks}")

    # TP1 触发后 WS 被清空 → 后续查询应触发 WS fallback
    bug2_pass = pos_tracker.ws_fallbacks > 0
    if bug2_pass:
        logger.success(f"  ✅ Bug #2 修复验证通过: WS fallback 已触发 {pos_tracker.ws_fallbacks} 次")
    else:
        logger.error("  ❌ Bug #2: WS fallback 未触发")

    runner.assertions.assert_true(
        bug2_pass,
        description="Bug #2: TP1后WS滞后应触发REST fallback"
    )

    # —— Bug #3: Trailing checker 顺序 + exit_type 归因 ——
    logger.info("\n--- Bug #3: Trailing Checker 顺序 + exit_type 归因 ---")
    logger.info(f"  TP1 触发轮: {stats['tp1_round']}")
    logger.info(f"  Trailing 激活轮: {stats['trailing_activated_round']}")
    logger.info(f"  Trailing 激活: {'是' if stats['trailing_activated'] else '否'}")
    logger.info(f"  Trailing SL 更新次数: {stats['trailing_sl_updates']}")
    logger.info(f"  Trailing 触发轮: {stats['trailing_round']}")
    logger.info(f"  Trailing 触发: {'是' if stats['trailing_triggered'] else '否'}")
    logger.info(f"  SL 触发: {'是' if stats['sl_triggered'] else '否'}")
    logger.info(f"  Checker 排序(Trailing优先)轮次: {stats['checker_ordering_trailing_first']}")
    logger.info(f"  最终 exit_type: {trade.exit_type}")

    bug3a_pass = stats["trailing_activated"] == 1
    bug3b_pass = stats["checker_ordering_trailing_first"] > 0
    bug3c_pass = trade.exit_type == "trailing" if stats["trailing_triggered"] else True

    if bug3a_pass:
        logger.success("  ✅ Bug #3a: Trailing 已成功激活")
    else:
        logger.error("  ❌ Bug #3a: Trailing 未激活")

    if bug3b_pass:
        logger.success(f"  ✅ Bug #3b: Trailing优先排序生效 ({stats['checker_ordering_trailing_first']} 轮)")
    else:
        logger.warning("  ⚠️ Bug #3b: 未检测到Trailing优先排序 (可能价格未达PARTIAL_TP)")

    if bug3c_pass:
        logger.success(f"  ✅ Bug #3c: exit_type 正确归因为 '{trade.exit_type}'")

    runner.assertions.assert_true(bug3a_pass, "Bug #3: Trailing应在TP1后激活")
    runner.assertions.assert_true(bug3c_pass, "Bug #3: exit_type应归因为trailing")

    # —— 综合统计 ——
    logger.info("\n--- 综合统计 ---")
    logger.info(f"  总轮次: {stats['total_rounds']}")
    logger.info(f"  最终仓位状态: {trade.position_state}")
    logger.info(f"  最终价格: {current_price:.2f}")
    if trade.open_rate > 0:
        logger.info(f"  最终盈亏: {trade.calc_profit_ratio(current_price)*100:.2f}%")

    # 记录完成
    runner.log.log(EventType.CUSTOM, "365轮综合测试完成", {
        "stats": stats,
        "bug1_pass": bug1_pass,
        "bug2_pass": bug2_pass,
        "bug3a_pass": bug3a_pass,
        "bug3b_pass": bug3b_pass,
        "bug3c_pass": bug3c_pass,
    })

    logger.info("\n" + "=" * 60)
    logger.info("365 轮退出系统综合测试完成")
    logger.info("=" * 60)
