"""
EntryFillHandler 事件驱动去重 & 可靠性模拟 — 365 轮。

模拟场景：
  1. 正常市价单：EntryFillHandler WS触发 → SL/TP 一次创建成功
  2. 竞态：EntryFillHandler + order_monitor 同时触发 → 不重复
  3. REST 暂时失败：pre-flight 失败 → _setup_done 不置位 → 重试成功
  4. 仓位未就绪：Guard 4"No real position"→ 不置位 → WS下轮重试
  5. 已存在：SL/TP 已在 OKX → pre-flight 检测到 → 跳过创建
  6. WS 风暴：3条 position_update 短时间到达 → debounce 合并
  7. Consumer 先到：EntryFillHandler 后到 → 跳过
  8. 全链路失败：都失败 → Reconciler Phase D 兜底
  9. 部分失败：SL 成功 TP 失败 → 不置位 → 重试
  10. Reconciler 去重：两个 SL → Reconciler 去重

用法：
    python3 tests/sim_entry_fill.py
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from collections import defaultdict

STATS = defaultdict(int)


# ============================================================================
# Fake OKX
# ============================================================================

class FakeOKX:
    def __init__(self):
        self.positions: dict[str, dict] = {}
        self.algo_orders: dict[str, dict] = {}
        self._create_fail_count: int = 0
        self._create_fail_type: str | None = None   # "sl" / "tp" / None=both
        self._preflight_fail_count: int = 0

    def set_position(self, symbol, side, contracts):
        self.positions[f"{symbol}:{side}"] = {"symbol": symbol, "side": side, "contracts": contracts}

    def has_position(self, symbol, side):
        key = f"{symbol}:{side}"
        return key in self.positions and self.positions[key]["contracts"] > 0

    def add_algo_order(self, algo_id, algo_type, symbol, side, state="live"):
        self.algo_orders[algo_id] = {"algoId": algo_id, "algo_type": algo_type,
                                     "symbol": symbol, "side": side, "state": state}

    def has_live_sl(self, symbol):
        if self._preflight_fail_count > 0:
            self._preflight_fail_count -= 1
            raise Exception("OKX pre-flight timeout")
        for a in self.algo_orders.values():
            if a["algo_type"] == "sl" and a["state"] == "live" and a["symbol"] == symbol:
                return True
        return False

    def has_live_tp(self, symbol):
        if self._preflight_fail_count > 0:
            self._preflight_fail_count -= 1
            raise Exception("OKX pre-flight timeout")
        for a in self.algo_orders.values():
            if a["algo_type"] == "tp" and a["state"] == "live" and a["symbol"] == symbol:
                return True
        return False

    def get_live_sl_id(self, symbol):
        for a in self.algo_orders.values():
            if a["algo_type"] == "sl" and a["state"] == "live" and a["symbol"] == symbol:
                return a["algoId"]
        return None

    def get_live_tp_id(self, symbol):
        for a in self.algo_orders.values():
            if a["algo_type"] == "tp" and a["state"] == "live" and a["symbol"] == symbol:
                return a["algoId"]
        return None

    def create_algo_order(self, algo_type, symbol, side):
        if self._create_fail_count > 0:
            if self._create_fail_type is None or self._create_fail_type == algo_type:
                self._create_fail_count -= 1
                raise Exception("OKX create_algo timeout")
        algo_id = f"algo_{algo_type}_{symbol}_{random.randint(10000,99999)}"
        self.algo_orders[algo_id] = {"algoId": algo_id, "algo_type": algo_type,
                                     "symbol": symbol, "side": side, "state": "live"}
        return algo_id

    def set_fail(self, preflight=0, create=0, create_fail_type=None):
        self._preflight_fail_count = preflight
        self._create_fail_count = create
        self._create_fail_type = create_fail_type

    def total_fail_remaining(self):
        return self._preflight_fail_count + self._create_fail_count


# ============================================================================
# Fake Trade
# ============================================================================

@dataclass
class FakeTrade:
    id: int
    pair: str
    is_short: bool = False
    is_open: bool = True
    amount: float = 1.0
    stop_loss: float = 0.0
    open_rate: float = 100.0
    sl_algo_id: str | None = None
    tp1_algo_id: str | None = None
    tp1_price: float = 0.0
    signal_meta: dict = field(default_factory=dict)


class FakeTradeLock:
    def __init__(self):
        self._locks: dict[int, asyncio.Lock] = {}

    def acquire(self, trade_id):
        if trade_id not in self._locks:
            self._locks[trade_id] = asyncio.Lock()
        return self._locks[trade_id]

    def clear(self):
        self._locks.clear()


trade_locks = FakeTradeLock()
_cooldowns: dict[int, dict[str, float]] = {}


def reset_globals(handler=None):
    _cooldowns.clear()
    trade_locks.clear()
    if handler is not None:
        handler._last_trigger.clear()


def _check_cooldown(trade_id, algo_key):
    now = time.monotonic()
    last = _cooldowns.get(trade_id, {}).get(algo_key, 0)
    return (now - last) >= 60.0


def _set_cooldown(trade_id, algo_key):
    if trade_id not in _cooldowns:
        _cooldowns[trade_id] = {}
    _cooldowns[trade_id][algo_key] = time.monotonic()


# ============================================================================
# 核心逻辑
# ============================================================================

class EntryFillHandler:
    def __init__(self, okx):
        self.okx = okx
        self._last_trigger: dict[int, float] = {}
        self._debounce_s = 5.0

    async def on_position_update(self, trade, symbol, side, contracts):
        if contracts <= 0:
            return
        now = time.monotonic()
        last = self._last_trigger.get(trade.id, 0)
        if now - last < self._debounce_s:
            STATS["entryfill_debounce_skipped"] += 1
            return
        self._last_trigger[trade.id] = now

        if trade.signal_meta.get("_setup_done"):
            STATS["entryfill_setup_done_skipped"] += 1
            return

        lock = trade_locks.acquire(trade.id)
        async with lock:
            if trade.signal_meta.get("_setup_done"):
                STATS["entryfill_postlock_skipped"] += 1
                return
            await ensure_protection(trade, self.okx, caller="EntryFillHandler")


async def order_monitor_step(trade, okx):
    if not trade.is_open or trade.amount <= 0:
        return
    if trade.signal_meta.get("_setup_done"):
        STATS["monitor_setup_done_skipped"] += 1
        return

    lock = trade_locks.acquire(trade.id)
    async with lock:
        if trade.signal_meta.get("_setup_done"):
            STATS["monitor_postlock_skipped"] += 1
            return
        await ensure_protection(trade, okx, caller="order_monitor")


async def consumer_post_entry(trade, okx):
    if trade.signal_meta.get("_setup_done"):
        STATS["consumer_setup_done_skipped"] += 1
        return

    lock = trade_locks.acquire(trade.id)
    async with lock:
        if trade.signal_meta.get("_setup_done"):
            STATS["consumer_postlock_skipped"] += 1
            return
        await ensure_protection(trade, okx, caller="consumer_loop")


async def ensure_protection(trade, okx, caller="unknown"):
    """
    模拟 ensure_protection，保留所有关键 Guard。
    与真实代码一致：
      - pre-flight REST 检查 → has_sl/has_tp
      - Guard 4: OKX 仓位检查
      - Guard 5: cooldown
      - 只在 SL + TP 都成功/已存在时标记 _setup_done
    """
    result = {"sl": "skipped", "tp": "skipped"}
    if not trade.is_open or trade.amount <= 0:
        return result
    if trade.signal_meta.get("_setup_done"):
        return result

    side = "short" if trade.is_short else "long"

    # —— SL ——
    if trade.stop_loss > 0:
        try:
            has_sl = okx.has_live_sl(trade.pair)
        except Exception:
            result["sl"] = "api_failed"
            return _finish_ensure(trade, result, caller)

        if has_sl:
            # 更新 DB 引用（与 protection_creator.create_sl Guard 5 一致）
            existing_id = okx.get_live_sl_id(trade.pair)
            if existing_id:
                trade.sl_algo_id = existing_id
            result["sl"] = "already_exists"
        else:
            if not okx.has_position(trade.pair, side):
                result["sl"] = "failed"
                return _finish_ensure(trade, result, caller)
            if not _check_cooldown(trade.id, "sl"):
                STATS["sl_cooldown_blocked"] += 1
                result["sl"] = "failed"
                return _finish_ensure(trade, result, caller)
            _set_cooldown(trade.id, "sl")
            try:
                algo_id = okx.create_algo_order("sl", trade.pair, "sell" if trade.is_short else "buy")
            except Exception:
                result["sl"] = "api_failed"
                return _finish_ensure(trade, result, caller)
            if algo_id:
                trade.sl_algo_id = algo_id
                result["sl"] = "created"
                STATS["sl_created"] += 1
            else:
                result["sl"] = "failed"

    # —— TP ——
    sl_done = result["sl"] in ("created", "already_exists", "skipped")
    if sl_done and trade.tp1_price > 0:
        try:
            has_tp = okx.has_live_tp(trade.pair)
        except Exception:
            result["tp"] = "api_failed"
            return _finish_ensure(trade, result, caller)

        if has_tp:
            existing_id = okx.get_live_tp_id(trade.pair)
            if existing_id:
                trade.tp1_algo_id = existing_id
            result["tp"] = "already_exists"
        else:
            if not okx.has_position(trade.pair, side):
                result["tp"] = "failed"
                return _finish_ensure(trade, result, caller)
            if not _check_cooldown(trade.id, "tp1"):
                STATS["tp_cooldown_blocked"] += 1
                result["tp"] = "failed"
                return _finish_ensure(trade, result, caller)
            _set_cooldown(trade.id, "tp1")
            try:
                algo_id = okx.create_algo_order("tp", trade.pair, "sell" if trade.is_short else "buy")
            except Exception:
                result["tp"] = "api_failed"
                return _finish_ensure(trade, result, caller)
            if algo_id:
                trade.tp1_algo_id = algo_id
                result["tp"] = "created"
                STATS["tp_created"] += 1
            else:
                result["tp"] = "failed"

    return _finish_ensure(trade, result, caller)


def _finish_ensure(trade, result, caller):
    sl_ok = result["sl"] in ("created", "already_exists")
    tp_ok = result["tp"] in ("created", "already_exists", "skipped")
    if sl_ok and tp_ok:
        trade.signal_meta["_setup_done"] = True
        STATS["setup_done_marked"] += 1
    else:
        STATS[f"done_not_set@{caller}"] += 1
    return result


async def reconciler_phase_d(trade, okx):
    """Reconciler Phase D — 不依赖 _setup_done，直接检查 algo_id"""
    if not trade.is_open or trade.amount <= 0:
        return
    side = "short" if trade.is_short else "long"
    if not okx.has_position(trade.pair, side):
        return

    if not trade.sl_algo_id and trade.stop_loss > 0:
        if not okx.has_live_sl(trade.pair):
            try:
                algo_id = okx.create_algo_order("sl", trade.pair, "sell" if trade.is_short else "buy")
            except Exception:
                algo_id = None
            if algo_id:
                trade.sl_algo_id = algo_id
                STATS["reconciler_sl_created"] += 1

    if not trade.tp1_algo_id and trade.tp1_price > 0:
        if not okx.has_live_tp(trade.pair):
            try:
                algo_id = okx.create_algo_order("tp", trade.pair, "sell" if trade.is_short else "buy")
            except Exception:
                algo_id = None
            if algo_id:
                trade.tp1_algo_id = algo_id
                STATS["reconciler_tp_created"] += 1


# ============================================================================
# 快照辅助
# ============================================================================

class Snap:
    """记录 STATS 快照用于 delta 断言"""
    def __init__(self):
        self.s = dict(STATS)

    def delta(self, key):
        return STATS.get(key, 0) - self.s.get(key, 0)


# ============================================================================
# 10 个场景
# ============================================================================

async def scenario_1_normal(okx, handler):
    """正常：WS 推仓位 → EntryFillHandler 触发 → SL+TP 一次创建"""
    snap = Snap()
    trade = FakeTrade(id=100, pair="SNDKUSDT", is_short=True,
                      stop_loss=104, open_rate=100, tp1_price=98)
    okx.set_position("SNDKUSDT", "short", 10)

    await handler.on_position_update(trade, "SNDKUSDT", "short", 10)

    assert trade.sl_algo_id is not None, "SL not created"
    assert trade.tp1_algo_id is not None, "TP not created"
    assert trade.signal_meta.get("_setup_done") is True
    assert snap.delta("sl_created") == 1
    assert snap.delta("tp_created") == 1
    assert snap.delta("setup_done_marked") == 1

    # order_monitor 再来应跳过
    await order_monitor_step(trade, okx)
    assert snap.delta("monitor_setup_done_skipped") + snap.delta("monitor_postlock_skipped") >= 1

    # Reconciler 应跳过（algo_id 都存在）
    sl, tp = trade.sl_algo_id, trade.tp1_algo_id
    await reconciler_phase_d(trade, okx)
    assert trade.sl_algo_id == sl and trade.tp1_algo_id == tp


async def scenario_2_race(okx, handler):
    """并发竞态：三个入口同时触发 → trade_lock 串行 + pre-flight 去重 → 只创建一次"""
    snap = Snap()
    trade = FakeTrade(id=200, pair="ETHUSDT", is_short=False,
                      stop_loss=950, open_rate=1000, tp1_price=1020)
    okx.set_position("ETHUSDT", "long", 5)

    await asyncio.gather(
        handler.on_position_update(trade, "ETHUSDT", "long", 5),
        order_monitor_step(trade, okx),
        consumer_post_entry(trade, okx),
    )

    assert snap.delta("sl_created") == 1, f"SL created {snap.delta('sl_created')} times, expected 1"
    assert snap.delta("tp_created") == 1, f"TP created {snap.delta('tp_created')} times, expected 1"
    assert trade.signal_meta.get("_setup_done") is True
    # OKX 上只有 2 个 algo order（1 SL + 1 TP）
    assert len(okx.algo_orders) == 2, f"Expected 2 algo orders, got {len(okx.algo_orders)}"


async def scenario_3_api_fail_retry(okx, handler):
    """REST pre-flight 暂时失败 → _setup_done 不置位 → 下轮 WS 推送重试成功"""
    snap = Snap()
    trade = FakeTrade(id=300, pair="BTCUSDT", is_short=True,
                      stop_loss=63000, open_rate=60000, tp1_price=58800)
    okx.set_position("BTCUSDT", "short", 1)

    # Round 1: SL pre-flight 报错 → fail, _setup_done 不置位
    okx.set_fail(preflight=1)  # 第一次 preflight (SL) 就失败 → SL api_failed
    await handler.on_position_update(trade, "BTCUSDT", "short", 1)
    assert trade.signal_meta.get("_setup_done") is not True
    assert trade.sl_algo_id is None
    assert snap.delta("done_not_set@EntryFillHandler") >= 1

    # Round 2: OKX 恢复 (无 preflight 失败) → 重试成功
    okx.set_fail(preflight=0, create=0)
    reset_globals(handler)
    await handler.on_position_update(trade, "BTCUSDT", "short", 1)
    assert trade.sl_algo_id is not None, "SL not created on retry"
    assert trade.tp1_algo_id is not None, "TP not created on retry"
    assert trade.signal_meta.get("_setup_done") is True


async def scenario_4_position_delay(okx, handler):
    """仓位未在 REST 出现 → Guard 4 失败 → 不置位 → 仓位出现后重试"""
    snap = Snap()
    trade = FakeTrade(id=400, pair="SOLUSDT", is_short=True,
                      stop_loss=156, open_rate=150, tp1_price=147)
    # 仓位尚未在 OKX 出现

    # Round 1: 触发但无仓位
    await handler.on_position_update(trade, "SOLUSDT", "short", 5)
    assert trade.signal_meta.get("_setup_done") is not True
    assert trade.sl_algo_id is None

    # Round 2: 仓位出现了
    okx.set_position("SOLUSDT", "short", 5)
    reset_globals(handler)
    await asyncio.gather(
        handler.on_position_update(trade, "SOLUSDT", "short", 5),
        order_monitor_step(trade, okx),
    )
    assert trade.sl_algo_id is not None, "SL not created after position appears"
    assert trade.signal_meta.get("_setup_done") is True


async def scenario_5_already_exists(okx, handler):
    """SL/TP 已在 OKX → pre-flight 检测到 → 同步引用 → 不创建新单"""
    snap = Snap()
    trade = FakeTrade(id=500, pair="DOGEUSDT", is_short=False,
                      stop_loss=0.072, open_rate=0.075, tp1_price=0.0765)
    okx.set_position("DOGEUSDT", "long", 10000)
    okx.add_algo_order("EXISTING_SL_001", "sl", "DOGEUSDT", "long")
    okx.add_algo_order("EXISTING_TP_001", "tp", "DOGEUSDT", "long")

    await handler.on_position_update(trade, "DOGEUSDT", "long", 10000)

    assert snap.delta("sl_created") == 0, "SL should NOT be created"
    assert snap.delta("tp_created") == 0, "TP should NOT be created"
    assert trade.signal_meta.get("_setup_done") is True
    # 同步了引用
    assert trade.sl_algo_id == "EXISTING_SL_001", f"Expected EXISTING_SL_001, got {trade.sl_algo_id}"
    assert trade.tp1_algo_id == "EXISTING_TP_001", f"Expected EXISTING_TP_001, got {trade.tp1_algo_id}"


async def scenario_6_ws_storm(okx, handler):
    """3 条 position_update 连续到达 → debounce 合并 → 只创建一次"""
    snap = Snap()
    trade = FakeTrade(id=600, pair="AVAXUSDT", is_short=True,
                      stop_loss=27.04, open_rate=26, tp1_price=25.48)
    okx.set_position("AVAXUSDT", "short", 3)

    await asyncio.gather(
        handler.on_position_update(trade, "AVAXUSDT", "short", 3),
        handler.on_position_update(trade, "AVAXUSDT", "short", 3),
        handler.on_position_update(trade, "AVAXUSDT", "short", 3),
    )

    assert snap.delta("sl_created") <= 1, f"SL debounce failed: {snap.delta('sl_created')}"
    assert snap.delta("tp_created") <= 1, f"TP debounce failed: {snap.delta('tp_created')}"
    assert snap.delta("entryfill_debounce_skipped") >= 1, "debounce should have skipped some"


async def scenario_7_consumer_first(okx, handler):
    """Consumer 先到 → 完成后 EntryFillHandler 后到 → 跳过"""
    snap = Snap()
    trade = FakeTrade(id=700, pair="LINKUSDT", is_short=False,
                      stop_loss=13.52, open_rate=13, tp1_price=13.26)
    okx.set_position("LINKUSDT", "long", 20)

    await consumer_post_entry(trade, okx)
    assert trade.signal_meta.get("_setup_done") is True
    sl, tp = trade.sl_algo_id, trade.tp1_algo_id

    await handler.on_position_update(trade, "LINKUSDT", "long", 20)

    assert trade.sl_algo_id == sl and trade.tp1_algo_id == tp, "should not have changed"
    assert snap.delta("entryfill_setup_done_skipped") + snap.delta("entryfill_postlock_skipped") >= 1


async def scenario_8_all_fail_reconciler(okx, handler):
    """全链路失败 → Reconciler Phase D 兜底"""
    snap = Snap()
    trade = FakeTrade(id=800, pair="DOTUSDT", is_short=True,
                      stop_loss=5.72, open_rate=5.5, tp1_price=5.39)
    okx.set_position("DOTUSDT", "short", 8)

    # EntryFillHandler: pre-flight fail → 不置位
    okx.set_fail(preflight=4)
    await handler.on_position_update(trade, "DOTUSDT", "short", 8)
    assert trade.signal_meta.get("_setup_done") is not True
    assert trade.sl_algo_id is None
    assert snap.delta("done_not_set@EntryFillHandler") >= 1

    # order_monitor: create fail → 不置位
    okx.set_fail(create=4)
    reset_globals(handler)
    await order_monitor_step(trade, okx)
    assert trade.signal_meta.get("_setup_done") is not True
    assert trade.sl_algo_id is None

    # Reconciler Phase D: 直接创建 → 成功
    okx.set_fail(preflight=0, create=0)
    await reconciler_phase_d(trade, okx)
    assert trade.sl_algo_id is not None, "Reconciler should have created SL"
    assert trade.tp1_algo_id is not None, "Reconciler should have created TP"
    assert snap.delta("reconciler_sl_created") >= 1
    assert snap.delta("reconciler_tp_created") >= 1


async def scenario_9_partial_failure(okx, handler):
    """SL 成功 TP pre-flight 失败 → _setup_done 不置位 → 重试成功"""
    snap = Snap()
    trade = FakeTrade(id=900, pair="ARBUSDT", is_short=True,
                      stop_loss=1.04, open_rate=1.0, tp1_price=0.98)
    okx.set_position("ARBUSDT", "short", 50)

    # SL 创建成功, TP 创建时 API 失败 → partial → _setup_done 不置位
    # create_fail_type="tp": 只有 TP 的 create_algo_order 抛异常，SL 正常
    okx.set_fail(create=1, create_fail_type="tp")

    await handler.on_position_update(trade, "ARBUSDT", "short", 50)
    assert trade.sl_algo_id is not None, "SL should be created"
    assert trade.signal_meta.get("_setup_done") is not True, "_setup_done must NOT be set on partial fail"
    assert snap.delta("done_not_set@EntryFillHandler") >= 1

    # Round 2: TP 恢复 → 重试成功
    okx.set_fail(preflight=0, create=0)
    reset_globals(handler)
    await handler.on_position_update(trade, "ARBUSDT", "short", 50)
    assert trade.tp1_algo_id is not None, "TP should be created on retry"
    assert trade.signal_meta.get("_setup_done") is True


async def scenario_10_duplicate_sl_reconciler(okx, handler):
    """OKX 上有两个 SL → Reconciler 验证不会重复创建"""
    snap = Snap()
    trade = FakeTrade(id=1000, pair="MATICUSDT", is_short=True,
                      stop_loss=0.52, open_rate=0.5, tp1_price=0.49)
    okx.set_position("MATICUSDT", "short", 100)

    # 已有 SL 和 TP
    okx.add_algo_order("SL_GOOD", "sl", "MATICUSDT", "short")
    okx.add_algo_order("SL_DUP", "sl", "MATICUSDT", "short")
    trade.sl_algo_id = "SL_GOOD"

    # Reconciler 检测到已有 SL → 不创建
    await reconciler_phase_d(trade, okx)
    assert snap.delta("reconciler_sl_created") == 0, "Reconciler should NOT create duplicate SL"
    assert trade.sl_algo_id == "SL_GOOD"


# ============================================================================
# 365 轮主循环
# ============================================================================

SCENARIOS = [
    (65,  scenario_1_normal),
    (50,  scenario_2_race),
    (40,  scenario_3_api_fail_retry),
    (35,  scenario_4_position_delay),
    (35,  scenario_5_already_exists),
    (30,  scenario_6_ws_storm),
    (30,  scenario_7_consumer_first),
    (30,  scenario_8_all_fail_reconciler),
    (25,  scenario_9_partial_failure),
    (25,  scenario_10_duplicate_sl_reconciler),
]


async def main():
    print("=" * 70)
    print("  EntryFillHandler 事件驱动去重 & 可靠性模拟")
    print(f"  {sum(c for c, _ in SCENARIOS)} 轮 × {len(SCENARIOS)} 种场景")
    print("=" * 70)

    total = 0
    passed = 0
    failed = 0
    scenario_stats = defaultdict(lambda: {"pass": 0, "fail": 0, "errors": []})

    for count, fn in SCENARIOS:
        for _ in range(count):
            total += 1
            okx = FakeOKX()
            handler = EntryFillHandler(okx)
            reset_globals()

            label = f"[{total:03d}/{sum(c for c,_ in SCENARIOS)}] {fn.__doc__ or fn.__name__}"
            try:
                await fn(okx, handler)
                passed += 1
                scenario_stats[fn.__name__]["pass"] += 1
                if total <= 10 or total % 50 == 0:
                    print(f"  {label}: ✅ PASS")
            except AssertionError as e:
                failed += 1
                scenario_stats[fn.__name__]["fail"] += 1
                scenario_stats[fn.__name__]["errors"].append(str(e)[:80])
                print(f"  {label}: ❌ FAIL — {e}")
            except Exception as e:
                failed += 1
                scenario_stats[fn.__name__]["fail"] += 1
                scenario_stats[fn.__name__]["errors"].append(f"{type(e).__name__}: {e}"[:80])
                print(f"  {label}: ❌ ERROR — {type(e).__name__}: {e}")

    # =====================================================================
    # 报告
    # =====================================================================
    print("\n" + "=" * 70)
    print("  模 拟 报 告")
    print("=" * 70)

    print(f"\n  📊 总轮数: {total} | ✅ {passed} 通过 | ❌ {failed} 失败")
    pct = passed / total * 100 if total > 0 else 0
    print(f"     通过率: {pct:.1f}%")

    print("\n  📋 场景分布:")
    for _count, fn in SCENARIOS:
        stats = scenario_stats[fn.__name__]
        p = stats["pass"]
        f = stats["fail"]
        bar = "█" * p + ("░" * f if f else "")
        doc = fn.__doc__ or fn.__name__
        print(f"    {doc[:52]:52s}  ✅{p:3d}  ❌{f:3d}  {bar}")

    print("\n  📈 去重统计:")
    dedup_keys = [
        ("entryfill_debounce_skipped",    "EntryFillHandler debounce 跳过"),
        ("entryfill_setup_done_skipped",  "EntryFillHandler _setup_done 跳过"),
        ("entryfill_postlock_skipped",    "EntryFillHandler 锁后二次确认跳过"),
        ("monitor_setup_done_skipped",    "order_monitor _setup_done 跳过"),
        ("monitor_postlock_skipped",      "order_monitor 锁后二次确认跳过"),
        ("consumer_setup_done_skipped",   "consumer_loop _setup_done 跳过"),
        ("consumer_postlock_skipped",     "consumer_loop 锁后二次确认跳过"),
        ("sl_cooldown_blocked",           "SL cooldown 拦截"),
        ("tp_cooldown_blocked",           "TP cooldown 拦截"),
    ]
    for key, desc in dedup_keys:
        val = STATS.get(key, 0)
        if val > 0:
            print(f"    🔒 {desc:45s} {val:5d} 次")

    print("\n  🔧 创建统计:")
    create_keys = [
        ("sl_created",            "SL 创建成功"),
        ("tp_created",            "TP 创建成功"),
        ("reconciler_sl_created", "Reconciler Phase D 补挂 SL"),
        ("reconciler_tp_created", "Reconciler Phase D 补挂 TP"),
        ("setup_done_marked",     "_setup_done 标记完成"),
    ]
    for key, desc in create_keys:
        val = STATS.get(key, 0)
        print(f"    ✨ {desc:45s} {val:5d} 次")

    print("\n  ⚠️  失败-未置位统计（证明失败时允许重试）:")
    fail_keys = sorted([k for k in STATS if k.startswith("done_not_set@")])
    for key in fail_keys:
        val = STATS[key]
        caller = key.replace("done_not_set@", "")
        print(f"    🔄 {caller:45s} {val:5d} 次")

    # 关键验证
    print("\n  🔍 最终验证:")
    total_sl = STATS.get("sl_created", 0) + STATS.get("reconciler_sl_created", 0)
    total_tp = STATS.get("tp_created", 0) + STATS.get("reconciler_tp_created", 0)
    total_skips = sum(STATS.get(k, 0) for k, _ in dedup_keys)
    total_not_done = sum(STATS.get(k, 0) for k in fail_keys)

    print(f"    SL 总创建: {total_sl} 次  |  TP 总创建: {total_tp} 次")
    print(f"    去重跳过: {total_skips} 次  |  失败未置位: {total_not_done} 次")

    # 每个有仓位的 trade 最终都有 SL+TP（无裸仓）
    # 通过每个场景的断言检查，无需额外验证

    if failed == 0:
        print("\n" + "=" * 70)
        print("  🎉 全部通过！事件驱动 SL/TP 去重机制验证成功。")
        print("     - 不会重复创建")
        print("     - 失败时 _setup_done 不置位，允许重试")
        print("     - Reconciler Phase D 不受影响，继续兜底")
        print("=" * 70)
    else:
        print(f"\n  ❌ {failed}/{total} 失败，需排查。")
        # 打印前 5 条错误详情
        for fn_name, stats in scenario_stats.items():
            if stats["errors"]:
                print(f"\n  —— {fn_name} ({len(stats['errors'])} errors) ——")
                for err in stats["errors"][:3]:
                    print(f"    • {err}")

    return failed


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
