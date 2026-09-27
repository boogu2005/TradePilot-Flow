"""
Reconciler v5 — Low-Frequency Health Check (every 10 minutes).

======================= CORE PRINCIPLE =======================
OKX REST API is the SINGLE SOURCE OF TRUTH.

Database stores only DESIRED state (what SHOULD be).
Database NEVER stores ACTUAL state (what IS).
Snapshot/WS are for performance display ONLY.
They NEVER participate in repair decisions.

All "does SL exist? does TP exist?" decisions MUST come from:
  → fetch_pending_algo_orders() (REST)
  → fetch_algo_order_by_id() (REST)
  → fetch_positions() (REST)

API failure = skip entire cycle. NEVER assume missing = not exists.
============================================================

Architecture:
  fetch_positions() + fetch_pending_algo_orders()  ← REST, Source of Truth
       ↓
  Build ActualState map (per symbol+side)
       ↓
  Read DesiredState from DB (per trade)
       ↓
  Delta = Desired - Actual
       ↓
  Delta == 0 → ZERO API calls → skip
  Delta != 0 → repair (cancel old + create new, atomically)
       ↓
  Done. Single pass. No loops. No retries. No repair queue.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from utils.time import ensure_utc

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade, Order, NON_OPEN_EXCHANGE_STATES
from core.exchange_runtime import runtime
from core.protection_targets import (
    initial_sl_price, teacher_sl_price, teacher_tp_prices,
    tp_price_for, single_teacher_tp,
)

L = logger.bind(module="reconciler")

# ———— Configuration ————
ENTRY_ORDER_TTL_HOURS = 72

# dedup: same log key max once per hour
_recent_logged: dict[str, float] = {}
_LOG_DEDUP_SECONDS = 3600
_MAX_LOG_KEYS = 1000


# ============================================================================
# Helpers
# ============================================================================

def _is_entry_order(order: Order) -> bool:
    role = (order.ft_order_role or "").lower()
    return role == "entry"


def _is_entry_order_stale(order: Order) -> bool:
    now = datetime.now(timezone.utc)
    expire_at = ensure_utc(order.expire_at)
    if expire_at:
        return now > expire_at
    order_date = ensure_utc(order.order_date)
    if order_date:
        expire_time = order_date + timedelta(hours=ENTRY_ORDER_TTL_HOURS)
        return now > expire_time
    return False


def _log_once(key: str, msg: str, level: str = "info"):
    now = datetime.now(timezone.utc).timestamp()
    last = _recent_logged.get(key, 0)
    if now - last < _LOG_DEDUP_SECONDS:
        return
    if len(_recent_logged) >= _MAX_LOG_KEYS:
        cutoff = now - _LOG_DEDUP_SECONDS * 2
        stale = [k for k, v in _recent_logged.items() if v < cutoff]
        for k in stale:
            del _recent_logged[k]
    _recent_logged[key] = now
    if level == "warning":
        L.warning(msg)
    elif level == "error":
        L.error(msg)
    else:
        L.info(msg)


def _norm_symbol(s: str) -> str:
    """Normalize symbol to OKX instId format: BTC-USDT-SWAP"""
    s = (s or "").upper().replace("/", "").replace(":USDT", "")
    if not s.endswith("USDT"):
        return s
    base = s[:-4]
    return f"{base}-USDT-SWAP"


def _safe_float(v: any, default: float = 0.0) -> float:
    """Convert value to float, handling empty strings and None."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


# ============================================================================
# Actual State — from OKX REST (Source of Truth)
# ============================================================================

@dataclass
class AlgoInfo:
    """A single algo order as reported by OKX."""
    algo_id: str = ""
    inst_id: str = ""
    algo_type: str = ""      # "stoploss" | "takeprofit" | "trailing_stop"
    side: str = ""           # buy / sell
    trigger_price: float = 0.0
    amount: float = 0.0      # sz
    state: str = ""          # live / filled / cancelled
    reduce_only: bool = False


@dataclass
class ActualState:
    """What OKX actually has for a single symbol+side."""
    symbol: str = ""
    side: str = ""           # long / short
    contracts: float = 0.0
    entry_price: float = 0.0
    sl_orders: list[AlgoInfo] = field(default_factory=list)
    tp_orders: list[AlgoInfo] = field(default_factory=list)
    trailing_orders: list[AlgoInfo] = field(default_factory=list)


@dataclass
class DesiredState:
    """What the database says we WANT."""
    trade_id: int = 0
    pair: str = ""
    is_short: bool = False
    target_sl: float = 0.0          # desired stop loss price
    target_tp1_price: float = 0.0   # desired TP1 trigger price
    target_tp1_pct: float = 30.0    # desired TP1 close percent (30%)
    target_tp2_price: float = 0.0   # desired TP2 trigger price
    target_tp2_pct: float = 35.0    # desired TP2 close percent (35% of original)
    trailing_enabled: bool = False  # should trailing be active
    breakeven_enabled: bool = False # should SL be at breakeven


@dataclass
class RepairAction:
    """A single repair action to execute."""
    action_type: str  # "create_sl" | "cancel_sl" | "create_tp" | "cancel_tp" | "cancel_trailing"
    symbol: str = ""
    algo_id: str = ""
    amount: float = 0.0
    trigger_price: float = 0.0
    side: str = ""
    reason: str = ""
    tp_index: int = 1  # which TP (1 or 2) for create_tp/cancel_tp


# ============================================================================
# OKX REST Data Fetching
# ============================================================================

async def _fetch_okx_positions() -> list[dict]:
    """Fetch ALL positions from OKX REST. Source of Truth."""
    from exchange_engine.exchange import fetch_positions
    try:
        return await fetch_positions(exchange="okx")
    except Exception as e:
        L.error(f"[Reconciler] REST fetch_positions FAILED: {e}")
        raise


async def _fetch_okx_algo_orders(symbol: str) -> list[dict]:
    """
    Fetch pending algo orders from OKX REST. RAISES on API failure.
    Uses fetch_pending_algo_orders (v5: fixed to raise instead of return []).
    """
    from exchange_engine.exchange import fetch_pending_algo_orders
    short = symbol.upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")
    result = await fetch_pending_algo_orders(short, exchange="okx")
    if result:
        live_count = sum(1 for o in result if o.get("state") == "live")
        L.debug(f"[Reconciler] {short}: {len(result)} algo orders ({live_count} live)")
    else:
        L.debug(f"[Reconciler] {short}: 0 algo orders (empty)")
    return result


async def _fetch_okx_algo_by_id(algo_id: str, symbol: str) -> dict | None:
    """Fetch a single algo order by algoId from OKX REST."""
    from exchange_engine.exchange import fetch_algo_order_by_id
    try:
        return await fetch_algo_order_by_id(algo_id, symbol, exchange="okx")
    except Exception as e:
        L.warning(f"[Reconciler] REST fetch_algo_by_id({algo_id}) failed: {e}")
        return None


# ============================================================================
# [ALTERNATIVE PATH] Build Actual State Map — not called in reconcile()
# ============================================================================
# build_actual_state_map + read_desired_state + compute_delta form the
# "delta engine" architecture described in the module docstring. They are
# NOT called in the current reconcile() flow (which uses inline Phase D logic).
# Kept as a reference implementation and for manual/scripted use.
# ============================================================================

def _classify_algo_order(raw: dict) -> AlgoInfo | None:
    """
    Classify a raw OKX algo order into AlgoInfo.
    Classification based on: ordType + triggerPx type + trailing flag + clOrdId.

    v7: Use clOrdId as fallback when slTriggerPx/tpTriggerPx are both absent.
    OKX batch API may omit these fields; clOrdId format is bot{tp|sl}{id}v{ver}.

    [修改] 移除 reduceOnly 过滤 — OKX pending-algo 响应中 reduceOnly 字段
    可能缺失，导致所有 conditional 类型单被误判为入场单而丢弃。
    改用 slTriggerPx / tpTriggerPx / callbackSpread 区分 SL/TP/Trailing。
    """
    ord_type = (raw.get("ordType") or "").lower()
    side = (raw.get("side") or "").lower()
    state = raw.get("state", "unknown")

    # 只处理 live 状态的单
    if state != "live":
        return None

    # 区分 SL / TP / Trailing
    sl_trigger = raw.get("slTriggerPx") or raw.get("slTriggerPx", "")
    tp_trigger = raw.get("tpTriggerPx") or raw.get("tpTriggerPx", "")
    callback_spread = raw.get("callbackSpread") or raw.get("callbackSpread", "")
    trigger_px = _safe_float(raw.get("triggerPx"))

    info = AlgoInfo(
        algo_id=raw.get("algoId", ""),
        inst_id=raw.get("instId", ""),
        side=side,
        trigger_price=trigger_px,
        amount=_safe_float(raw.get("sz")),
        state=state,
        reduce_only=True,  # 所有需处理的单都假设为减仓
    )

    # [修改] 按实际触发条件分类，不依赖 reduceOnly
    if ord_type in ("move_order_stop", "trailing_stop") or callback_spread:
        info.algo_type = "trailing_stop"
    elif sl_trigger or (ord_type == "conditional" and trigger_px > 0 and not tp_trigger):
        # v7: check clOrdId before falling back to SL
        cl_ord_id = raw.get("clOrdId", "") or raw.get("clientOrderId", "")
        parsed = _parse_clordid_type(cl_ord_id)
        if parsed == "takeprofit":
            info.algo_type = "takeprofit"
        else:
            info.algo_type = "stoploss"
    elif tp_trigger or ord_type == "takeprofit":
        # 有 tpTriggerPx → TP
        info.algo_type = "takeprofit"
    elif ord_type in ("stoploss", "stop"):
        info.algo_type = "stoploss"
    else:
        # v7: check clOrdId before conservative fallback
        cl_ord_id = raw.get("clOrdId", "") or raw.get("clientOrderId", "")
        parsed = _parse_clordid_type(cl_ord_id)
        if parsed == "takeprofit":
            info.algo_type = "takeprofit"
        elif parsed == "stoploss":
            info.algo_type = "stoploss"
        else:
            info.algo_type = "stoploss"

    return info


def build_actual_state_map(
    positions: list[dict],
    algo_orders: list[dict],
) -> dict[tuple[str, str], ActualState]:
    """
    Build a map of (symbol_norm, side) → ActualState from OKX REST data.

    symbol_norm: e.g., "BTC-USDT-SWAP"
    side: "long" or "short"
    """
    result: dict[tuple[str, str], ActualState] = {}

    # Step 1: Build from positions
    for p in positions:
        contracts = _safe_float(p.get("contracts"))
        if contracts <= 0:
            continue
        sym = _norm_symbol(p.get("symbol", ""))
        side = (p.get("side") or "long").lower()
        key = (sym, side)
        result[key] = ActualState(
            symbol=sym,
            side=side,
            contracts=contracts,
            entry_price=_safe_float(p.get("entry_price")),
        )

    # Step 2: Attach algo orders to their positions
    for raw in algo_orders:
        info = _classify_algo_order(raw)
        if info is None or not info.algo_id:
            continue

        inst_id = info.inst_id
        pos_side = (raw.get("posSide") or "").lower()

        # Find matching position (normalize both sides for comparison)
        matched_key = None
        for (sym, side) in result:
            if _norm_pair(sym) == _norm_pair(inst_id) and side == pos_side:
                matched_key = (sym, side)
                break

        if matched_key is None:
            matched_key = (inst_id, pos_side)
            if matched_key not in result:
                result[matched_key] = ActualState(
                    symbol=inst_id, side=pos_side, contracts=0
                )

        state = result[matched_key]
        if info.algo_type == "stoploss":
            state.sl_orders.append(info)
        elif info.algo_type == "takeprofit":
            state.tp_orders.append(info)
        elif info.algo_type == "trailing_stop":
            state.trailing_orders.append(info)

    return result


# ============================================================================
# Read Desired State from Database
# ============================================================================

def read_desired_state(trade: Trade) -> DesiredState:
    """
    Read what the database says we WANT.
    The DB stores DESIRED (target) state, not ACTUAL state.
    """
    target_tp2 = tp_price_for(trade, 2) or 0.0
    return DesiredState(
        trade_id=trade.id,
        pair=trade.pair,
        is_short=trade.is_short,
        target_sl=teacher_sl_price(trade) or trade.stop_loss,
        target_tp1_price=tp_price_for(trade, 1) or 0.0,
        target_tp1_pct=100.0 if single_teacher_tp(trade) else 30.0,
        target_tp2_price=target_tp2,
        target_tp2_pct=100.0 if single_teacher_tp(trade) else 35.0,
        trailing_enabled=trade.trailing_activated,
        breakeven_enabled=(trade.stop_loss == trade.open_rate and trade.open_rate > 0),
    )


# ============================================================================
# [ALTERNATIVE PATH] Delta Engine — not called in reconcile()
# ============================================================================
# compute_delta + _execute_repair_actions form a standalone repair pipeline.
# They are NOT called in the current reconcile() (Phase D does inline delta).
# Use these for manual repair runs: compute_delta(desired, actual) → RepairAction[]
# ============================================================================

def _price_close(a: float, b: float, tick_size: float = 0.1) -> bool:
    """Check if two prices are equal within tick size tolerance."""
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= tick_size


def compute_delta(desired: DesiredState, actual: ActualState) -> list[RepairAction]:
    """
    Compare Desired vs Actual and produce a list of RepairActions.
    Returns empty list if everything is correct (ZERO API calls).

    OKX is the source of truth for what EXISTS.
    DB is the source of truth for what we WANT.
    """
    actions: list[RepairAction] = []

    side = "sell" if desired.is_short else "buy"
    norm_sym = _norm_symbol(desired.pair)

    # ———— Stop Loss ————
    if desired.target_sl > 0:
        existing_sl = None
        for s in actual.sl_orders:
            if s.algo_type == "stoploss" and s.state == "live":
                existing_sl = s
                break

        if existing_sl is None:
            # SL missing → create
            actions.append(RepairAction(
                action_type="create_sl",
                symbol=norm_sym,
                amount=actual.contracts,
                trigger_price=desired.target_sl,
                side=side,
                reason="SL not found on OKX",
            ))
        elif not _price_close(existing_sl.trigger_price, desired.target_sl):
            # SL exists but wrong price → cancel old + create new
            actions.append(RepairAction(
                action_type="cancel_sl",
                symbol=norm_sym,
                algo_id=existing_sl.algo_id,
                reason=f"SL price mismatch: OKX={existing_sl.trigger_price} desired={desired.target_sl}",
            ))
            actions.append(RepairAction(
                action_type="create_sl",
                symbol=norm_sym,
                amount=actual.contracts,
                trigger_price=desired.target_sl,
                side=side,
                reason="SL price correction",
            ))

    # ———— Take Profit 1 ————
    if desired.target_tp1_price > 0:
        existing_tp1 = None
        for t in actual.tp_orders:
            if t.algo_type == "takeprofit" and t.state == "live":
                if desired.target_tp2_price > 0:
                    # 按价格区分 TP1 和 TP2
                    dist_tp1 = abs(t.trigger_price - desired.target_tp1_price)
                    dist_tp2 = abs(t.trigger_price - desired.target_tp2_price)
                    if dist_tp1 <= dist_tp2 or dist_tp2 / max(desired.target_tp2_price, 1) > 0.01:
                        existing_tp1 = t
                        break
                else:
                    existing_tp1 = t
                    break

        tp1_amount = actual.contracts * desired.target_tp1_pct / 100.0 if actual.contracts > 0 else 0

        if existing_tp1 is None:
            if tp1_amount > 0:
                actions.append(RepairAction(
                    action_type="create_tp",
                    symbol=norm_sym,
                    amount=tp1_amount,
                    trigger_price=desired.target_tp1_price,
                    side=side,
                    reason="TP1 not found on OKX",
                    tp_index=1,
                ))
        elif not _price_close(existing_tp1.trigger_price, desired.target_tp1_price):
            actions.append(RepairAction(
                action_type="cancel_tp",
                symbol=norm_sym,
                algo_id=existing_tp1.algo_id,
                reason=f"TP1 price mismatch: OKX={existing_tp1.trigger_price} desired={desired.target_tp1_price}",
                tp_index=1,
            ))
            if tp1_amount > 0:
                actions.append(RepairAction(
                    action_type="create_tp",
                    symbol=norm_sym,
                    amount=tp1_amount,
                    trigger_price=desired.target_tp1_price,
                    side=side,
                    reason="TP1 price correction",
                    tp_index=1,
                ))

    # ———— Take Profit 2 ————
    if desired.target_tp2_price > 0:
        existing_tp2 = None
        for t in actual.tp_orders:
            if t.algo_type == "takeprofit" and t.state == "live":
                dist_tp1 = abs(t.trigger_price - desired.target_tp1_price) if desired.target_tp1_price > 0 else float('inf')
                dist_tp2 = abs(t.trigger_price - desired.target_tp2_price)
                if dist_tp2 < dist_tp1 and dist_tp2 / max(desired.target_tp2_price, 1) <= 0.01:
                    existing_tp2 = t
                    break
                elif dist_tp2 < dist_tp1 and existing_tp1 is None:
                    existing_tp2 = t
                    break

        tp2_amount = actual.contracts * desired.target_tp2_pct / 100.0 if actual.contracts > 0 else 0

        if existing_tp2 is None:
            if tp2_amount > 0:
                actions.append(RepairAction(
                    action_type="create_tp",
                    symbol=norm_sym,
                    amount=tp2_amount,
                    trigger_price=desired.target_tp2_price,
                    side=side,
                    reason="TP2 not found on OKX",
                    tp_index=2,
                ))
        elif not _price_close(existing_tp2.trigger_price, desired.target_tp2_price):
            actions.append(RepairAction(
                action_type="cancel_tp",
                symbol=norm_sym,
                algo_id=existing_tp2.algo_id,
                reason=f"TP2 price mismatch: OKX={existing_tp2.trigger_price} desired={desired.target_tp2_price}",
                tp_index=2,
            ))
            if tp2_amount > 0:
                actions.append(RepairAction(
                    action_type="create_tp",
                    symbol=norm_sym,
                    amount=tp2_amount,
                    trigger_price=desired.target_tp2_price,
                    side=side,
                    reason="TP2 price correction",
                    tp_index=2,
                ))

    # ———— Trailing Stop ————
    if not desired.trailing_enabled and actual.trailing_orders:
        # Trailing should NOT be active but exists → cancel
        for t in actual.trailing_orders:
            if t.state == "live":
                actions.append(RepairAction(
                    action_type="cancel_trailing",
                    symbol=norm_sym,
                    algo_id=t.algo_id,
                    reason="Trailing should not be active",
                ))

    # ———— Duplicate detection ————
    # If more than 1 live SL → cancel extras
    live_sls = [s for s in actual.sl_orders if s.state == "live"]
    if len(live_sls) > 1:
        # Keep the one closest to desired price, cancel rest
        live_sls.sort(key=lambda s: abs(s.trigger_price - desired.target_sl))
        for dup in live_sls[1:]:
            actions.append(RepairAction(
                action_type="cancel_sl",
                symbol=norm_sym,
                algo_id=dup.algo_id,
                reason=f"Duplicate SL (algoId={dup.algo_id})",
            ))

    # If more than 1 live TP per category → cancel extras
    live_tps = [t for t in actual.tp_orders if t.state == "live"]
    if len(live_tps) > 1:
        # Categorize by target price
        tp1_target = desired.target_tp1_price
        tp2_target = desired.target_tp2_price
        tp1_candidates = []
        tp2_candidates = []
        for t in live_tps:
            px = t.trigger_price
            if px <= 0:
                continue
            if tp1_target > 0 and tp2_target > 0:
                dist_tp1 = abs(px - tp1_target)
                dist_tp2 = abs(px - tp2_target)
                if dist_tp1 <= dist_tp2 and dist_tp1 / max(tp1_target, 1) < 0.01:
                    tp1_candidates.append(t)
                elif dist_tp2 < dist_tp1 and dist_tp2 / max(tp2_target, 1) < 0.01:
                    tp2_candidates.append(t)
            elif tp1_target > 0 and abs(px - tp1_target) / max(tp1_target, 1) < 0.01:
                tp1_candidates.append(t)
            elif tp2_target > 0 and abs(px - tp2_target) / max(tp2_target, 1) < 0.01:
                tp2_candidates.append(t)
        # Dedup each category
        for cat_tps, cat_name in [(tp1_candidates, "TP1"), (tp2_candidates, "TP2")]:
            if len(cat_tps) > 1:
                cat_tps.sort(key=lambda t: abs(t.trigger_price - (
                    desired.target_tp1_price if cat_name == "TP1" else desired.target_tp2_price)))
                for dup in cat_tps[1:]:
                    actions.append(RepairAction(
                        action_type="cancel_tp",
                        symbol=norm_sym,
                        algo_id=dup.algo_id,
                        reason=f"Duplicate {cat_name} (algoId={dup.algo_id})",
                        tp_index=1 if cat_name == "TP1" else 2,
                    ))

    return actions


# ============================================================================
# Repair Executor
# ============================================================================

async def _execute_repair_actions(
    trade: Trade,
    actions: list[RepairAction],
    session: Session,
) -> int:
    """
    Execute repair actions. Returns count of actions executed.

    RULES:
    - Cancels come BEFORE creates (to avoid duplicate rejection by OKX).
    - Each create is verified by REST after execution.
    - Uses deterministic clientOrderId for idempotency.
    """
    if not actions:
        return 0

    trade_ex = trade.exchange or "okx"
    executed = 0

    # Sort: cancels first, then creates
    cancels = [a for a in actions if a.action_type.startswith("cancel_")]
    creates = [a for a in actions if a.action_type.startswith("create_")]
    ordered = cancels + creates

    for action in ordered:
        try:
            if action.action_type == "cancel_sl":
                await runtime.cancel_algo_order(action.algo_id, action.symbol)
                trade.sl_algo_id = None
                L.info(f"[Repair] {trade.pair} cancelled SL algoId={action.algo_id}: {action.reason}")
                executed += 1

            elif action.action_type == "cancel_tp":
                await runtime.cancel_algo_order(action.algo_id, action.symbol)
                tp_idx = getattr(action, 'tp_index', 1)
                if tp_idx == 1:
                    trade.tp1_algo_id = None
                elif tp_idx == 2:
                    trade.tp2_algo_id = None
                L.info(f"[Repair] {trade.pair} cancelled TP{tp_idx} algoId={action.algo_id}: {action.reason}")
                executed += 1

            elif action.action_type == "cancel_trailing":
                await runtime.cancel_algo_order(action.algo_id, action.symbol)
                L.info(f"[Repair] {trade.pair} cancelled Trailing algoId={action.algo_id}: {action.reason}")
                executed += 1

            elif action.action_type == "create_sl":
                # PRE-FLIGHT: REST verify no live SL exists for this symbol+side
                existing = await _fetch_okx_algo_orders(action.symbol)
                already_has_sl = any(
                    _classify_algo_order(o) and _classify_algo_order(o).algo_type == "stoploss"
                    and _classify_algo_order(o).state == "live"
                    for o in existing
                )
                if already_has_sl:
                    L.info(f"[Repair] {trade.pair} SL already exists on OKX (REST confirmed), skipping create")
                    continue

                from core.protection_creator import protection_creator
                # We need to handle the case where stop_loss is set on the trade
                trade.stop_loss = action.trigger_price
                result = await protection_creator.create_sl(trade, session)
                if result.success:
                    L.success(f"[Repair] {trade.pair} SL created algoId={result.algo_id} @ {action.trigger_price}")
                    executed += 1
                else:
                    L.error(f"[Repair] {trade.pair} SL create failed: {result.error}")

            elif action.action_type == "create_tp":
                tp_idx = getattr(action, 'tp_index', 1)
                tp_label = f"TP{tp_idx}" if tp_idx > 1 else "TP1"
                # PRE-FLIGHT: REST verify no live TP exists at this price
                existing = await _fetch_okx_algo_orders(action.symbol)
                already_has_tp = any(
                    _classify_algo_order(o) and _classify_algo_order(o).algo_type == "takeprofit"
                    and _classify_algo_order(o).state == "live"
                    for o in existing
                )
                if already_has_tp:
                    L.info(f"[Repair] {trade.pair} {tp_label} already exists on OKX (REST confirmed), skipping create")
                    continue

                from core.protection_creator import protection_creator
                if tp_idx == 1:
                    trade.tp1_price = action.trigger_price
                elif tp_idx == 2:
                    trade.tp2_price = action.trigger_price
                result = await protection_creator.create_tp(trade, session, tp_index=tp_idx)
                if result.success:
                    L.success(f"[Repair] {trade.pair} {tp_label} created algoId={result.algo_id} @ {action.trigger_price}")
                    executed += 1
                else:
                    L.error(f"[Repair] {trade.pair} {tp_label} create failed: {result.error}")

        except Exception as e:
            L.error(f"[Repair] {trade.pair} action {action.action_type} failed: {e}")

    return executed


# ============================================================================
# Legacy Cleanup Operations (still useful)
# ============================================================================

async def cleanup_closed_position(session: Session) -> int:
    """Clean up residual orders for closed trades."""
    closed = session.query(Trade).filter(Trade.is_open.is_(False)).all()
    cleaned = 0
    for trade in closed:
        for o in (trade.orders or []):
            if not o.ft_is_open:
                continue
            if _is_entry_order(o) and not _is_entry_order_stale(o):
                continue
            if o.status in NON_OPEN_EXCHANGE_STATES:
                o.ft_is_open = False
                cleaned += 1
                continue
            try:
                await runtime.cancel_order(o.order_id, o.ft_pair)
                o.ft_is_open = False
                cleaned += 1
            except Exception as e:
                if "51400" in str(e):
                    o.ft_is_open = False
                    o.status = o.status or "canceled"
                    cleaned += 1
    if cleaned:
        session.commit()
    return cleaned


async def repair_orders(session: Session) -> int:
    """
    Sync DB order status with exchange (zombie repair).
    Uses OKX REST (source of truth), NOT WS cache.
    """
    from exchange_engine.exchange import fetch_open_orders, fetch_order

    open_orders = session.query(Order).filter(Order.ft_is_open.is_(True)).all()
    if not open_orders:
        return 0

    # Batch fetch ALL open orders from OKX REST (source of truth)
    try:
        all_rest_orders = await fetch_open_orders(symbol=None, exchange="okx")
        rest_order_map: dict[str, dict] = {}
        for o in all_rest_orders:
            oid = o.get("id") or o.get("orderId", "")
            if oid:
                rest_order_map[oid] = o
    except Exception as e:
        L.warning(f"[Reconciler] Phase A fetch_open_orders REST failed: {e}")
        return 0  # Cannot verify without REST

    repaired = 0
    for o in open_orders:
        trade = o.trade
        if not trade:
            if _is_entry_order(o) and not _is_entry_order_stale(o):
                continue
            o.ft_is_open = False
            repaired += 1
            continue

        # Check REST batch results first
        ex_order = rest_order_map.get(o.order_id)

        if ex_order is None:
            # Not in batch — might be an algo order. Individual REST check.
            try:
                ex_order = await fetch_order(o.order_id, o.ft_pair, include_algo=True)
            except Exception as e:
                _log_once(f"repair_fetch_fail:{o.order_id}",
                          f"[Reconciler] Phase A fetch_order({o.order_id}) REST failed: {e}", "warning")
                continue

        if ex_order is None:
            if _is_entry_order(o) and not _is_entry_order_stale(o):
                continue
            o.ft_is_open = False
            o.status = "not_found"
            repaired += 1
        elif ex_order.get("status") in NON_OPEN_EXCHANGE_STATES:
            o.ft_is_open = False
            o.status = ex_order["status"]
            repaired += 1

    if repaired:
        session.commit()
    return repaired


def _norm_pair(pair: str) -> str:
    """Normalize any pair format to short form: BTCUSDT"""
    return (pair or "").upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT").replace("-USDC-SWAP", "USDC")


def _to_okx_inst_id(norm_pair: str) -> str:
    """Convert normalized pair back to OKX instId: BTCUSDT → BTC-USDT-SWAP"""
    p = (norm_pair or "").upper()
    if p.endswith("USDT"):
        return f"{p[:-4]}-USDT-SWAP"
    return p


def _is_algo_sl(o: dict) -> bool:
    """
    Check if an OKX algo order is a Stop Loss.

    v6: Add fallback for ordType="conditional" (same as _classify_algo_type).
    OKX returns ordType="conditional" for both SL and TP; the actual type
    is determined by slTriggerPx / tpTriggerPx. When slTriggerPx is absent
    but the order is conditional, treat as SL (conservative default).

    v7: Use clOrdId as fallback when slTriggerPx/tpTriggerPx are both absent.
    OKX batch API may omit these fields; clOrdId format is bot{tp|sl}{id}v{ver}.
    """
    if o.get("state") != "live":
        return False
    ord_type = (o.get("ordType") or "").lower()
    sl_trigger = o.get("slTriggerPx") or ""
    tp_trigger = o.get("tpTriggerPx") or ""
    # Explicit SL markers
    if bool(sl_trigger) or ord_type in ("stoploss", "stop"):
        return True
    # v6: conditional without trigger fields → treat as SL (conservative)
    # Must NOT have tpTriggerPx (which would indicate it's actually a TP)
    if ord_type == "conditional" and not tp_trigger:
        # v7: check clOrdId before falling back to SL
        cl_ord_id = o.get("clOrdId", "") or o.get("clientOrderId", "")
        parsed = _parse_clordid_type(cl_ord_id)
        if parsed == "takeprofit":
            return False  # It's a TP, not SL
        if parsed == "stoploss":
            return True
        return True  # Fallback: treat as SL
    return False


def _is_algo_tp(o: dict) -> bool:
    """
    Check if an OKX algo order is a Take Profit.

    v7: Use clOrdId as fallback when tpTriggerPx is absent.
    OKX batch API may omit tpTriggerPx for conditional TP orders.
    """
    if o.get("state") != "live":
        return False
    ord_type = (o.get("ordType") or "").lower()
    tp_trigger = o.get("tpTriggerPx") or ""
    if bool(tp_trigger) or ord_type == "takeprofit":
        return True
    # v7: check clOrdId for conditional orders without tpTriggerPx
    sl_trigger = o.get("slTriggerPx") or ""
    if ord_type == "conditional" and not sl_trigger:
        cl_ord_id = o.get("clOrdId", "") or o.get("clientOrderId", "")
        parsed = _parse_clordid_type(cl_ord_id)
        if parsed == "takeprofit":
            return True
    return False


def _parse_clordid_type(cl_ord_id: str) -> str | None:
    """
    Parse clientOrderId to determine order type.
    Format: bot{tp|sl|tpN}{trade_id}v{version}
    Examples: bottp1173v0 → takeprofit, botsl1173v1 → stoploss
    Returns "stoploss", "takeprofit", or None.
    """
    if not cl_ord_id or not cl_ord_id.startswith("bot"):
        return None
    import re
    # Remove "bot" prefix, then extract type before digits+version
    rest = cl_ord_id[3:]
    m = re.match(r'^(sl|tp\d*)\d+v\d+', rest)
    if m:
        t = m.group(1)
        if t == "sl":
            return "stoploss"
        if t.startswith("tp"):
            return "takeprofit"
    return None


async def _fetch_all_algo_orders() -> list[dict]:
    """
    Fetch ALL live algo orders across all symbols from OKX REST.
    Used by Phase 4 dedup to detect duplicate TP/SL orders.
    """
    from exchange_engine.exchange import get_exchange, _exponential_backoff_wrapper
    ex = get_exchange("okx")
    all_data = []
    # v6: Cover ALL valid OKX algo ordTypes (conditional, oco, trigger, move_order_stop)
    for ord_type in ("conditional", "move_order_stop", "trigger", "oco"):
        try:
            resp = await _exponential_backoff_wrapper(
                lambda ot=ord_type: ex.privateGetTradeOrdersAlgoPending({
                    "instType": "SWAP",
                    "ordType": ot,
                    "state": "live",
                }),
                exchange="okx", label=f"fetch_all_algo({ord_type})",
            )
            if resp is None:
                L.warning(f"[Reconciler] fetch_all_algo ordType={ord_type}: resp is None!")
                continue
            if isinstance(resp, dict):
                data = resp.get("data", [])
                # 无挂单是常态，不刷屏；仅在发现挂单时记录（诊断价值在非零时）
                if data:
                    L.info(f"[Reconciler] fetch_all_algo ordType={ord_type}: {len(data)} orders")
                all_data.extend(data)
            elif isinstance(resp, list):
                if resp:
                    L.info(f"[Reconciler] fetch_all_algo ordType={ord_type}: {len(resp)} orders (list response)")
                all_data.extend(resp)
            else:
                L.warning(f"[Reconciler] fetch_all_algo ordType={ord_type}: unexpected type {type(resp)}")
        except Exception as e:
            L.warning(f"[Reconciler] fetch_all_algo ordType={ord_type} failed: {e}")
    return all_data


async def reconcile_trade_existence(session: Session) -> int:
    """
    [LEGACY] Check for missing/recovered trades by comparing OKX positions with DB.
    NOT called in reconcile() — superseded by _recover_missing_trades() in Phase B.

    Difference from _recover_missing_trades: this version pushes to RepairQueue;
    _recover_missing_trades pre-syncs SL/TP directly via REST.

    Uses normalized pair comparison to handle all three formats:
      BTC/USDT:USDT / BTC-USDT-SWAP / BTCUSDT → BTCUSDT
    """
    try:
        positions = await _fetch_okx_positions()
    except Exception:
        return 0

    fixed = 0
    active_trades = Trade.get_active_trades(session)

    for pos in positions:
        pair_raw = pos.get("symbol", "")
        side = (pos.get("side") or "").lower()
        contracts = pos.get("contracts", 0)
        if contracts <= 0:
            continue

        pos_norm = _norm_pair(pair_raw)
        is_short = (side == "short")

        # Match by normalized pair — handles all three formats
        existing = None
        for t in active_trades:
            if not t.is_open or (t.amount or 0) <= 0:
                continue
            if _norm_pair(t.pair) == pos_norm and t.is_short == is_short:
                existing = t
                break

        if existing is None:
            L.info(f"[Reconciler] OKX has position {pos_norm} {'SHORT' if is_short else 'LONG'} but DB missing → recovering")
            try:
                # exchange.py fetch_positions() 将 OKX 返回值规范化为 snake_case 键名
                # （见 exchange.py:518-528: entry_price, unrealized_pnl, contracts, leverage）
                entry_price = _safe_float(pos.get("entry_price"))
                leverage = _safe_float(pos.get("leverage"), 10)
                contracts = _safe_float(pos.get("contracts"))
                unrealized_pnl = _safe_float(pos.get("unrealized_pnl"))

                if entry_price <= 0 or contracts <= 0:
                    L.warning(f"[Reconciler] Skip recovery {pos_norm}: entry_price={entry_price} contracts={contracts}")
                    continue

                # Normalize to CCXT pair format: BTCUSDT → BTC/USDT:USDT
                base = pos_norm.replace("USDT", "")
                norm_pair_formatted = f"{base}/USDT:USDT"

                from datetime import datetime as dt, timezone as tz
                from core.config_loader import load_config

                trade = Trade(
                    pair=norm_pair_formatted,
                    base_currency=pos_norm.replace("USDT", ""),
                    stake_currency="USDT",
                    exchange="okx",
                    is_open=True,
                    is_short=is_short,
                    open_rate=entry_price,
                    amount=contracts,
                    amount_requested=contracts,
                    open_date=dt.now(tz.utc),
                    opened_at=dt.now(tz.utc),
                    strategy="recovery",
                    leverage=leverage if leverage > 0 else 10,
                    trading_mode="futures",
                    signal_id="recovery",
                    exit_mode="auto",
                    position_state="open",
                    fee_open=0.0004,
                    fee_close=0.0004,
                    stake_amount=abs(contracts * entry_price / (leverage if leverage > 0 else 10)),
                )

                config = load_config()
                sl_pct = abs(config.get("risk", {}).get("default_stoploss_pct", 0.02))
                trade.adjust_stop_loss(entry_price, sl_pct, initial=True)

                session.add(trade)
                session.flush()

                # Push to Repair Queue
                try:
                    from core.repair_queue import repair_queue, RepairTask
                    repair_queue.push(RepairTask(
                        priority=1,
                        created_at=dt.now(tz.utc).timestamp(),
                        trade_id=trade.id,
                        task_type="create_sl",
                    ))
                except Exception:
                    pass

                L.success(f"[Reconciler] Created trade #{trade.id} for {pos_norm} "
                          f"{'SHORT' if is_short else 'LONG'} @ {entry_price} x{leverage}")
                fixed += 1
            except Exception as e:
                L.error(f"[Reconciler] Failed to recover {pos_norm}: {e}")

    return fixed


async def cleanup_orphans(session: Session) -> int:
    """Clean up orphan orders and residual algo orders for closed trades."""
    cleaned = 0

    # Orphan orders (no parent trade)
    orphan_orders = session.query(Order).filter(
        ~Order.ft_trade_id.in_(session.query(Trade.id))
    ).all()
    for o in orphan_orders:
        if _is_entry_order(o) and not _is_entry_order_stale(o):
            continue
        if o.status not in NON_OPEN_EXCHANGE_STATES:
            try:
                await runtime.cancel_order(o.order_id, o.ft_pair)
            except Exception as e:
                if "51400" not in str(e):
                    continue
        o.ft_is_open = False
        o.status = o.status or "canceled"
        cleaned += 1

    # Residual orders for closed trades
    closed_trades = session.query(Trade).filter(Trade.is_open == False).all()
    for trade in closed_trades:
        for o in (trade.orders or []):
            if not o.ft_is_open:
                continue
            if _is_entry_order(o) and not _is_entry_order_stale(o):
                continue
            if o.status not in NON_OPEN_EXCHANGE_STATES:
                try:
                    await runtime.cancel_order(o.order_id, o.ft_pair)
                except Exception as e:
                    if "51400" not in str(e):
                        continue
            o.ft_is_open = False
            o.status = o.status or "canceled"
            cleaned += 1

    if cleaned:
        session.commit()
    return cleaned


# ============================================================================
# MAIN ENTRY POINT — Five-Phase Health Check (every 10 minutes)
# ============================================================================
#
# ⚠️ CORE PRINCIPLE: OKX REST API is the ONLY source of truth.
#    Database is a CACHE, never trusted for reconciliation decisions.
#    When DB disagrees with OKX → DB is repaired to match OKX.
#
# Phase A: DB Cleanup     — zombie/phantom/orphan orders (cancel via REST, status verify via REST)
# Phase B: Position Match — OKX↔DB bidirectional sync, recover missing trades
# Phase C: Residual Algos — cancel SL/TP/trailing for positions that no longer exist
# Phase D: Delta Sync     — fetch ALL OKX algo orders; compare vs DB; fix DB + dedup + fill
# Phase E: Stale Entries  — cancel limit entry orders pending >72h
#
# Each phase is independent — one failure does not block others.
# ============================================================================


async def _tp_rehang_blocked(trade, tp_index: int, okx_contracts: float,
                             current_price: float | None) -> str | None:
    """
    Phase D 补挂 TP 前的守卫 — 判断该 TP 是否实际「已触发」而非「缺失」。

    事故背景 (TRIA #586): TP1/TP2 都已在交易所触发成交，但条件单触发后
    algoId 从 OKX 消失，Phase D 误判「TP2 缺失」→ 补挂 → 价格已在触发价上方
    → 刚挂上立即成交 → 一个仓位出现第 4 次平仓。

    两条防线（满足任一条即拦截）：
    1. 价格测试: 当前价已过该 TP 触发价 → 补挂必然立即成交（重复平仓）
    2. 仓位测试: 当前仓位 ≤ 该 TP 阶段完成后的理论剩余 ×1.03 容差
       → 该 TP 的份额已被平掉（覆盖「触发成交后价格回落」的窗口）

    返回 None = 允许补挂；否则返回跳过原因。
    """
    trigger = tp_price_for(trade, tp_index) or 0.0
    if trigger <= 0:
        return "无法确定TP触发价，保守跳过"

    # ———— 防线 1: 价格已过触发价 ————
    if current_price is None or current_price <= 0:
        # ticker 不可用 → 保守跳过本轮补挂（宁可缺失下轮再挂，不可重复挂）
        return "无法获取当前价格，保守跳过"
    if trade.is_short:
        if current_price <= trigger:
            return f"当前价 {current_price:.6g} 已 ≤ TP触发价 {trigger:.6g}，补挂会立即成交"
    else:
        if current_price >= trigger:
            return f"当前价 {current_price:.6g} 已 ≥ TP触发价 {trigger:.6g}，补挂会立即成交"

    # ———— 防线 2: 仓位已到该 TP 阶段完成后的理论剩余 ————
    # TP1 平 30% → 理论剩余 70%；TP2 平 35% → 理论剩余 35%（均基于开仓全仓量 amount_requested）
    if not single_teacher_tp(trade) and (trade.amount_requested or 0) > 0:
        remain_ratio = 0.70 if tp_index == 1 else 0.35
        threshold = trade.amount_requested * remain_ratio * 1.03
        if okx_contracts <= threshold:
            return (f"仓位 {okx_contracts}张 ≤ 理论剩余 {threshold:.1f}张"
                    f"(TP{tp_index}份额已被平掉)")

    return None


async def bookkeep_external_close(session, trade, reason: str | None = None) -> bool:
    """
    交易所侧平仓的完整记账 — 本地没有成交确认(挂单 SL/TP 在 OKX 触发成交)时,
    对 DB trade 补全: close_rate / 盈亏 / CloseHistory / 状态关闭 / 清残留保护单。

    背景: 此前 reconcile 只标记 is_open=0 不算账, close_rate 空缺到午夜
    修复脚本回填 → dashboard 历史交易延迟一整天。

    记账价格为 ticker 现价(估算,无手续费), signal_meta 打 _pnl_estimated 标记,
    午夜修复脚本会用它拉真实 fills 精化。

    返回 True=已记账/已关闭; False=行情不可用等(留给下轮或兜底路径)。
    """
    trade_ex = trade.exchange or "okx"

    # 防重复: 已被其他路径关闭则视为成功
    if not trade.is_open:
        return True

    # ———— 取价(唯一真相源 REST ticker, 与 position_sync 同款) ————
    try:
        from exchange_engine import exchange as ex
        ticker = await ex.fetch_ticker(trade.pair, exchange=trade_ex)
        rate = ticker.get("last", 0) if ticker else 0
    except Exception as e:
        L.warning(f"[BookkeepClose] {trade.pair} fetch_ticker 失败: {e}, 跳过本轮记账")
        return False
    if not rate or rate <= 0:
        L.warning(f"[BookkeepClose] {trade.pair} ticker 无效 rate={rate}, 跳过本轮记账")
        return False

    # ———— 完整记账 ————
    # 1. 先定 exit_reason(close() 内部写 CloseHistory 会读取它)
    trade.exit_reason = trade.exit_reason or (reason or "position_closed_on_exchange")
    # 2. 标记估算盈亏(午夜修复脚本据此用真实 fills 精化)
    meta = dict(trade.signal_meta or {})
    meta["_pnl_estimated"] = True
    trade.signal_meta = meta
    # 3. trade.close(): close_rate/公式盈亏/CloseHistory/amount=0/state=closed
    trade.close(rate)
    L.info(f"[BookkeepClose] {trade.pair} 完整记账 @ {rate:.6g}"
           f" reason={trade.exit_reason} (估算盈亏, _pnl_estimated)")
    # 4. 清理残留保护单(幂等; 幂等锁在 cleanup_trade_orders 内部)
    try:
        from exit.protection import cleanup_trade_orders
        await cleanup_trade_orders(trade, session=session, exchange=trade_ex)
    except Exception as e:
        L.warning(f"[BookkeepClose] {trade.pair} 清理残留保护单失败: {e}")
    # 5. flush 使 close_history/字段变更立即可见
    try:
        session.flush()
    except Exception:
        pass
    return True


async def reconcile(session: Session) -> dict:
    """
    Five-phase health check. Called every 10 minutes.

    Phases are independent — one failure does not block others.
    """
    stats: dict = {}
    total_cancelled = 0
    total_created = 0

    # =====================================================================
    # Phase A: DB cleanup — cancels via REST, verifies status via REST
    # =====================================================================
    try:
        stats["cleanup_closed"] = await cleanup_closed_position(session)
    except Exception as e:
        L.warning(f"[Reconciler] Phase A cleanup_closed failed: {e}")

    try:
        stats["repair_zombie_orders"] = await repair_orders(session)
    except Exception as e:
        L.warning(f"[Reconciler] Phase A repair_orders failed: {e}")

    try:
        stats["cleanup_orphans"] = await cleanup_orphans(session)
    except Exception as e:
        L.warning(f"[Reconciler] Phase A cleanup_orphans failed: {e}")

    # =====================================================================
    # Phase B: Position Match — OKX↔DB bidirectional sync + Trade recovery
    # =====================================================================
    try:
        positions = await _fetch_okx_positions()
    except Exception as e:
        L.warning(f"[Reconciler] Phase B OKX REST failed: {e}. Skipping remaining phases.")
        stats["okx_fetch"] = "failed"
        stats["status"] = "skipped_api_failure"
        return stats

    stats["okx_positions"] = len(positions)

    # Build OKX position index: (norm_pair, side) → contracts
    okx_pos_map: dict[tuple[str, str], dict] = {}
    for p in positions:
        contracts = _safe_float(p.get("contracts"))
        if contracts <= 0:
            continue
        sym = _norm_symbol(p.get("symbol", ""))
        side = (p.get("side") or "long").lower()
        okx_pos_map[(_norm_pair(sym), side)] = p

    # ———— Reverse Match: DB trades not on OKX → mark closed ————
    active_trades = Trade.get_active_trades(session)
    trades_closed_in_sync = 0
    for trade in active_trades:
        if not trade.is_open or (trade.amount or 0) <= 0:
            continue
        side = "short" if trade.is_short else "long"
        norm = _norm_pair(trade.pair)
        if (norm, side) not in okx_pos_map:
            # Position gone from OKX — 完整记账(close_rate/盈亏/CloseHistory),
            # 行情不可用时回退为旧版字段标记
            closed_ok = await bookkeep_external_close(session, trade)
            if not closed_ok:
                trade.is_open = False
                trade.exit_reason = trade.exit_reason or "position_closed_on_exchange"
                trade.close_date = trade.close_date or datetime.now(timezone.utc)
                trade.position_state = "closed"
            trades_closed_in_sync += 1
            L.info(f"[Reconciler] Phase B: {trade.pair} {'SHORT' if trade.is_short else 'LONG'} "
                   f"OKX无仓位 → {'完整记账(估算)' if closed_ok else '标记closed(无行情,兜底)'}")

    if trades_closed_in_sync > 0:
        stats["trades_closed_by_sync"] = trades_closed_in_sync
        session.flush()

    # ———— Forward Match: OKX positions not in DB → recover ————
    try:
        stats["existence_fixes"] = await _recover_missing_trades(
            session, positions, active_trades, okx_pos_map
        )
    except Exception as e:
        import traceback
        L.warning(f"[Reconciler] Phase B trade recovery failed: {e}")
        L.warning(f"[Reconciler] Phase B traceback:\n{traceback.format_exc()}")

    # =====================================================================
    # Phase C: Residual Algos — cancel SL/TP for positions that don't exist
    # =====================================================================
    try:
        residual_cancelled = await _cleanup_residual_algos(session, okx_pos_map)
        stats["residual_algos_cancelled"] = residual_cancelled
        total_cancelled += residual_cancelled
    except Exception as e:
        L.warning(f"[Reconciler] Phase C residual cleanup failed: {e}")

    # =====================================================================
    # Phase D: Delta Reconciliation — OKX REST is the ONLY source of truth
    # =====================================================================
    # 1. Fetch ALL live algo orders from OKX REST (single API call)
    # 2. Build okx_algo_map: {(norm_pair, side): {algos...}}
    # 3. For each active trade: compare OKX reality vs DB cache
    #    - OKX has SL, DB wrong/missing → fix DB
    #    - OKX missing SL, trade needs it → create on OKX + set DB
    #    - DB has stale algo_id (not on OKX) → clear DB
    # 4. Dedup naturally: if ≥2 SL/TP for same position, keep best, cancel rest
    # =====================================================================
    trades_processed = 0
    active_trades = Trade.get_active_trades(session)

    # —— Fetch OKX algo orders once (single source of truth) ——
    all_algos_raw = await _fetch_all_algo_orders()
    live_algos = [a for a in all_algos_raw if a.get("state") == "live"]
    L.info(f"[Reconciler] Phase D: OKX返回 {len(live_algos)} 个活跃算法单")

    # Build lookup: (norm_pair, posSide) → {sl_algos: [...], tp_algos: [...]}
    okx_algo_map: dict[tuple[str, str], dict] = {}
    for a in live_algos:
        inst = _norm_pair(a.get("instId", ""))
        pos_side = (a.get("posSide") or "").lower()
        if not inst or not pos_side:
            continue
        key = (inst, pos_side)
        if key not in okx_algo_map:
            okx_algo_map[key] = {"sl_algos": [], "tp_algos": [], "trailing_algos": []}
        if _is_algo_sl(a):
            okx_algo_map[key]["sl_algos"].append(a)
        elif _is_algo_tp(a):
            okx_algo_map[key]["tp_algos"].append(a)
        else:
            okx_algo_map[key]["trailing_algos"].append(a)

    for trade in active_trades:
        if not trade.is_open or (trade.amount or 0) <= 0:
            continue
        trades_processed += 1

        side = "short" if trade.is_short else "long"
        norm = _norm_pair(trade.pair)
        pos_data = okx_pos_map.get((norm, side))
        okx_contracts = _safe_float(pos_data.get("contracts")) if pos_data else 0

        if okx_contracts <= 0:
            # ———— OKX 无仓位 → DB 应同步为 closed (完整记账) ————
            if trade.position_state != "closed":
                closed_ok = await bookkeep_external_close(session, trade)
                if not closed_ok:
                    L.warning(f"[Reconciler] Phase D: {trade.pair} OKX无仓位, DB状态={trade.position_state} → 标记closed(无行情,兜底)")
                    trade.is_open = False
                    trade.exit_reason = trade.exit_reason or "position_closed_on_exchange"
                    trade.close_date = trade.close_date or datetime.now(timezone.utc)
                    trade.position_state = "closed"
                else:
                    L.warning(f"[Reconciler] Phase D: {trade.pair} OKX无仓位, DB状态={trade.position_state} → 完整记账(估算)")
            continue

        # Sync position size: OKX → DB
        if abs(trade.amount - okx_contracts) > 1e-8:
            L.info(f"[Reconciler] Phase D pos sync: {trade.pair} DB={trade.amount} → OKX={okx_contracts}")
            trade.amount = okx_contracts

        # ———— Auto-heal missing open_rate from OKX ————
        if trade.open_rate <= 0 and pos_data:
            okx_entry = _safe_float(pos_data.get("entry_price"))
            if okx_entry > 0:
                trade.open_rate = okx_entry
                L.info(f"[Reconciler] Phase D 从OKX同步open_rate: {trade.pair} → {okx_entry}")

        # ———— Auto-heal missing stop_loss ————
        if (trade.stop_loss or 0) <= 0 and trade.open_rate > 0:
            from core.config_loader import load_config as _load_cfg
            cfg = _load_cfg()
            sl_pct = abs(cfg.get("risk", {}).get("default_stoploss_pct", 0.02))
            teacher_sl = teacher_sl_price(trade)
            if teacher_sl:
                trade.stop_loss = teacher_sl
                sl_pct = abs(teacher_sl - trade.open_rate) / trade.open_rate
            elif trade.is_short:
                trade.stop_loss = float(trade.open_rate * (1 + sl_pct))
            else:
                trade.stop_loss = float(trade.open_rate * (1 - sl_pct))
            trade.stop_loss_pct = -sl_pct
            trade.initial_stop_loss = trade.stop_loss
            trade.initial_stop_loss_pct = -sl_pct
            L.info(f"[Reconciler] Phase D 自动修复stop_loss: {trade.pair} "
                   f"open_rate={trade.open_rate} → stop_loss={trade.stop_loss} "
                   f"({sl_pct*100:.0f}%)")

        norm_sym = _norm_symbol(trade.pair)

        # Get OKX algo state for this position
        okx_algos = okx_algo_map.get((norm, side), {"sl_algos": [], "tp_algos": [], "trailing_algos": []})
        okx_sl_list = okx_algos["sl_algos"]
        okx_tp_list = okx_algos["tp_algos"]
        okx_trailing_list = okx_algos["trailing_algos"]

        # ———— v7: Price-based reclassification ————
        # OKX batch API may omit slTriggerPx/tpTriggerPx/clOrdId, causing
        # _is_algo_sl/_is_algo_tp to misclassify TP orders as SL/trailing.
        # Use triggerPx matching against trade's desired prices to correct.
        target_sl = (teacher_sl_price(trade) if trade.position_state == "open"
                     else trade.stop_loss) or initial_sl_price(trade) or 0.0
        target_tp = tp_price_for(trade, 1) or 0.0
        L.debug(f"[Reconciler] Phase D v7 reclass: {trade.pair} target_sl={target_sl} target_tp={target_tp} "
                f"sl_list={len(okx_sl_list)} tp_list={len(okx_tp_list)} trailing={len(okx_trailing_list)}")
        if target_sl > 0 and target_tp > 0:
            # v7: Also reclassify from trailing_algos (which may contain misclassified TP/SL)
            all_unknown = okx_sl_list + okx_trailing_list
            still_sl = []
            still_trailing = []
            for a in all_unknown:
                # 角色互斥: DB 已登记的 SL 单不允许被重分类为 TP
                # (事故: 今日 10:37 曾把真实 SL 单按价格误写进 tp1_algo_id)
                if trade.sl_algo_id and a.get("algoId") == trade.sl_algo_id:
                    still_sl.append(a)
                    continue
                px = _safe_float(a.get("triggerPx"))
                if px > 0:
                    sl_dist = abs(px - target_sl) / max(target_sl, 1e-8)
                    tp_dist = abs(px - target_tp) / max(target_tp, 1e-8)
                    if tp_dist < sl_dist and tp_dist < 0.05:  # closer to TP + within 5%
                        okx_tp_list.append(a)
                        L.info(f"[Reconciler] Phase D 价格重分类: {trade.pair} algoId={a.get('algoId','')[:16]} "
                               f"triggerPx={px} →TP (sl_dist={sl_dist:.4f} tp_dist={tp_dist:.4f})")
                        continue
                    if sl_dist < tp_dist and sl_dist < 0.05:  # closer to SL + within 5%
                        still_sl.append(a)
                        continue
                still_trailing.append(a)
            okx_sl_list = still_sl
            okx_trailing_list = still_trailing

        # ———— SL: OKX reality vs DB cache ————
        # v6: 合并 sl_algos + trailing_algos 一起处理。OKX 全仓止损单在减仓后
        # 依然有效，同一仓位只应有 1 个保护单（SL 或 Trailing Stop）。
        all_sl_algos = okx_sl_list + okx_trailing_list

        if (trade.stop_loss or 0) > 0:
            if all_sl_algos:
                # OKX has SL/Trailing → sync to DB + dedup
                if len(all_sl_algos) > 1:
                    # Dedup: keep the one closest to target price, cancel ALL others
                    all_sl_algos.sort(key=lambda o: abs(_safe_float(o.get("triggerPx")) - target_sl))
                    for dup in all_sl_algos[1:]:
                        aid = dup.get("algoId", "")
                        if aid:
                            try:
                                await runtime.cancel_algo_order(aid, _to_okx_inst_id(norm))
                                L.warning(f"[Reconciler] Phase D 去重SL: 取消 algoId={aid} (保留 {all_sl_algos[0].get('algoId','?')})")
                                total_cancelled += 1
                            except Exception as e:
                                L.warning(f"[Reconciler] Phase D 去重SL失败 algoId={aid}: {e}")

                best_sl = all_sl_algos[0]
                okx_sl_id = best_sl.get("algoId", "")
                okx_sl_price = _safe_float(best_sl.get("triggerPx"))

                if trade.sl_algo_id != okx_sl_id:
                    # 角色互斥: OKX 候选不得与已登记的 TP 单冲突
                    if okx_sl_id and (okx_sl_id == trade.tp1_algo_id or okx_sl_id == trade.tp2_algo_id):
                        L.warning(f"[Reconciler] Phase D 角色互斥: {trade.pair} OKX候选 {okx_sl_id} 已是TP单 → 不写入sl_algo_id")
                    else:
                        # DB stale or missing → fix from OKX
                        L.info(f"[Reconciler] Phase D SL修复: {trade.pair} DB={trade.sl_algo_id or 'None'} → OKX={okx_sl_id} @{okx_sl_price}")
                        trade.sl_algo_id = okx_sl_id
                        # 清理历史误标: 若 SL id 曾被写进 tp 字段，清除（配合补挂守卫，缺失会安全重挂）
                        for _tp_f in ("tp1_algo_id", "tp2_algo_id"):
                            if getattr(trade, _tp_f, None) == okx_sl_id:
                                setattr(trade, _tp_f, None)
                                L.warning(f"[Reconciler] Phase D 角色互斥: {trade.pair} 清除 {_tp_f} 中误标的SL id {okx_sl_id}")

                # 老师价位优先：交易所仍是默认止损时，先建立老师单，再撤旧单。
                teacher_sl = teacher_sl_price(trade)
                if teacher_sl and trade.position_state == "open" and okx_sl_price > 0 \
                        and abs(okx_sl_price - teacher_sl) > max(teacher_sl * 0.001, 0.01):
                    old_algo_id = okx_sl_id
                    trade.stop_loss = teacher_sl
                    trade.initial_stop_loss = teacher_sl
                    from core.protection_creator import protection_creator
                    protection_creator.bump_clordid_version(trade.id, "sl")
                    if await _fill_single_sl(trade, session, okx_contracts):
                        total_created += 1
                        if old_algo_id and old_algo_id != trade.sl_algo_id:
                            try:
                                await runtime.cancel_algo_order(old_algo_id, _to_okx_inst_id(norm))
                                total_cancelled += 1
                            except Exception as e:
                                L.warning(f"[Reconciler] 老师SL已补挂，旧默认SL撤单失败: {e}")

                # Check if SL price on OKX matches desired price
                # 老师指定的 SL 已在上方纠正。其余差异可能是 trailing 产生，
                # 协调器不按 DB 的移动价频繁撤改单。
                if trade.position_state not in ("partial_tp", "tp1_filled"):
                    if okx_sl_price > 0 and abs(okx_sl_price - trade.stop_loss) > max(okx_sl_price * 0.005, 1.0):
                        L.debug(
                            f"[Reconciler] Phase D: {trade.pair} SL价格差异 "
                            f"OKX={okx_sl_price} DB={trade.stop_loss} → 保留原始SL，不纠正"
                        )
                else:
                    # partial_tp / tp1_filled: SL 价格差异由 TrailingChecker / 保本逻辑处理（仅DB），协调器不干预
                    if okx_sl_price > 0 and abs(okx_sl_price - trade.stop_loss) > max(okx_sl_price * 0.005, 1.0):
                        L.debug(
                            f"[Reconciler] Phase D: {trade.pair} {trade.position_state}状态，SL价格差异"
                            f" OKX={okx_sl_price} DB={trade.stop_loss} → 保留原始SL作为最终保障"
                        )
            else:
                # OKX has no SL in batch fetch → per-symbol REST verify before clearing
                if trade.sl_algo_id:
                    # Double-check: REST verify the specific algo still exists
                    verified = await _fetch_okx_algo_by_id(trade.sl_algo_id, norm_sym)
                    if verified and verified.get("state") == "live":
                        L.info(f"[Reconciler] Phase D: {trade.pair} SL {trade.sl_algo_id} REST-verified alive (batch fetch missed it)")
                        all_sl_algos.append(verified)  # Add to list so sync logic runs
                        # Re-run dedup after adding verified order
                        if len(all_sl_algos) > 1:
                            all_sl_algos.sort(key=lambda o: abs(_safe_float(o.get("triggerPx")) - target_sl))
                            for dup in all_sl_algos[1:]:
                                aid = dup.get("algoId", "")
                                if aid:
                                    try:
                                        await runtime.cancel_algo_order(aid, _to_okx_inst_id(norm))
                                        L.warning(f"[Reconciler] Phase D 去重SL(REST验证后): 取消 algoId={aid}")
                                        total_cancelled += 1
                                    except Exception as e:
                                        L.warning(f"[Reconciler] Phase D 去重SL失败 algoId={aid}: {e}")
                        # Update reference from verified order
                        best = all_sl_algos[0]
                        if trade.sl_algo_id != best.get("algoId", ""):
                            trade.sl_algo_id = best.get("algoId", "")
                    else:
                        L.warning(f"[Reconciler] Phase D: {trade.pair} DB有sl_algo_id={trade.sl_algo_id} 但REST确认已不存在 → 清除DB引用")
                        trade.sl_algo_id = None

            # ———— Create SL if missing on OKX ————
            # 优先补老师最新 SL；没有老师价位时使用固定初始 SL。
            # stop_loss 可能被 trailing 修改，不直接用它当补挂目标。
            if not trade.sl_algo_id:
                _saved_sl = trade.stop_loss
                # 读取持仓记录的保护目标
                desired_sl = initial_sl_price(trade)
                if desired_sl:
                    trade.stop_loss = desired_sl
                elif trade.open_rate > 0:
                    from core.config_loader import load_config as _ld
                    _sl_pct = abs(_ld().get("risk", {}).get("default_stoploss_pct", 0.02))
                    trade.stop_loss = float(
                        trade.open_rate * (1 + _sl_pct) if trade.is_short
                        else trade.open_rate * (1 - _sl_pct)
                    )
                created = await _fill_single_sl(trade, session, okx_contracts)
                if created:
                    total_created += 1
                    L.success(
                        f"[Reconciler] Phase D: {trade.pair} SL补挂 @ {trade.stop_loss} "
                        f"(老师优先, initial_stop_loss={trade.initial_stop_loss})"
                    )
                # 恢复 trailing stop_loss（如果是 partial_tp 状态且之前有值）
                if trade.position_state == "partial_tp" and _saved_sl > 0:
                    trade.stop_loss = _saved_sl

        else:
            # Trade has no stop_loss target → but OKX might have leftover SL → cancel ALL
            if all_sl_algos:
                for a in all_sl_algos:
                    aid = a.get("algoId", "")
                    if aid:
                        try:
                            await runtime.cancel_algo_order(aid, _to_okx_inst_id(norm))
                            L.warning(f"[Reconciler] Phase D: {trade.pair} stop_loss=0但OKX有SL/Trailing {aid} → 取消")
                            total_cancelled += 1
                        except Exception:
                            pass
                if trade.sl_algo_id:
                    trade.sl_algo_id = None

        # ———— TP: OKX reality vs DB cache (v2: TP1 + TP2 separate) ————
        # 计算目标 TP1 和 TP2 价格
        target_tp1 = tp_price_for(trade, 1) or 0.0
        target_tp2 = tp_price_for(trade, 2) or 0.0

        if teacher_tp_prices(trade):
            bot_tp_ids = {o.order_id for o in (trade.orders or [])
                          if (o.ft_order_tag or "").startswith("tp_")}
            kept_tp_orders = []
            for order in okx_tp_list:
                aid = order.get("algoId", "")
                px = _safe_float(order.get("triggerPx"))
                matches_teacher = any(target > 0 and px > 0
                                      and abs(px - target) / target < 0.001
                                      for target in (target_tp1, target_tp2))
                is_bot_order = aid in bot_tp_ids or str(order.get("clOrdId", "")).startswith("bottp")
                if not matches_teacher and is_bot_order and aid:
                    try:
                        await runtime.cancel_algo_order(aid, _to_okx_inst_id(norm))
                        total_cancelled += 1
                        if trade.tp1_algo_id == aid:
                            trade.tp1_algo_id = None
                        if trade.tp2_algo_id == aid:
                            trade.tp2_algo_id = None
                        L.info(f"[Reconciler] 老师TP已生效，清理旧系统TP algoId={aid}")
                        continue
                    except Exception as e:
                        L.warning(f"[Reconciler] 清理旧系统TP失败 algoId={aid}: {e}")
                kept_tp_orders.append(order)
            okx_tp_list = kept_tp_orders

        # 只有一级老师止盈时，旧的系统 TP2 不再属于目标保护单。
        if teacher_tp_prices(trade) and target_tp2 <= 0 and trade.tp2_algo_id \
                and trade.tp2_algo_id != trade.tp1_algo_id:
            old_tp2_id = trade.tp2_algo_id
            try:
                await runtime.cancel_algo_order(old_tp2_id, _to_okx_inst_id(norm))
                trade.tp2_algo_id = None
                okx_tp_list = [o for o in okx_tp_list if o.get("algoId") != old_tp2_id]
                total_cancelled += 1
            except Exception as e:
                L.warning(f"[Reconciler] {trade.pair} 取消旧默认TP2失败: {e}")

        if okx_tp_list:
            # ———— Categorize TPs by target price ————
            # TP1 类别: 价格接近 target_tp1（2%）的
            # TP2 类别: 价格接近 target_tp2（4%）的
            # 其余: 无法分类（可能被取消）
            tp1_candidates = []
            tp2_candidates = []
            for t in okx_tp_list:
                px = _safe_float(t.get("triggerPx"))
                if px <= 0:
                    continue
                if target_tp1 > 0 and target_tp2 > 0:
                    dist_tp1 = abs(px - target_tp1)
                    dist_tp2 = abs(px - target_tp2)
                    # 哪个目标更近就归哪类，阈值 1% 以内
                    if dist_tp1 < dist_tp2 and dist_tp1 / max(target_tp1, 1) < 0.01:
                        tp1_candidates.append(t)
                    elif dist_tp2 < dist_tp1 and dist_tp2 / max(target_tp2, 1) < 0.01:
                        tp2_candidates.append(t)
                    else:
                        # 距离两者都远 → 无法分类
                        pass
                elif target_tp1 > 0 and abs(px - target_tp1) / max(target_tp1, 1) < 0.01:
                    tp1_candidates.append(t)
                elif target_tp2 > 0 and abs(px - target_tp2) / max(target_tp2, 1) < 0.01:
                    tp2_candidates.append(t)

            # ———— Dedup: 每类最多保留 1 个，取消多余的 ————
            for cat_name, candidates in [("TP1", tp1_candidates), ("TP2", tp2_candidates)]:
                if len(candidates) > 1:
                    candidates.sort(key=lambda o: abs(
                        _safe_float(o.get("triggerPx")) - (target_tp1 if cat_name == "TP1" else target_tp2)))
                    for dup in candidates[1:]:
                        aid = dup.get("algoId", "")
                        if aid:
                            try:
                                await runtime.cancel_algo_order(aid, _to_okx_inst_id(norm))
                                L.warning(f"[Reconciler] Phase D 去重{cat_name}: 取消 algoId={aid}")
                                total_cancelled += 1
                            except Exception as e:
                                L.warning(f"[Reconciler] Phase D 去重{cat_name}失败 algoId={aid}: {e}")

            # ———— Sync TP1 algo_id ————
            if tp1_candidates:
                best_tp1 = tp1_candidates[0]
                okx_tp1_id = best_tp1.get("algoId", "")
                okx_tp1_price = _safe_float(best_tp1.get("triggerPx"))
                if trade.tp1_algo_id != okx_tp1_id:
                    if okx_tp1_id and okx_tp1_id == trade.sl_algo_id:
                        # 角色互斥: OKX TP 候选实际是已登记的 SL 单
                        L.warning(f"[Reconciler] Phase D 角色互斥: {trade.pair} OKX候选 {okx_tp1_id} 实为SL单 → 不写入tp1_algo_id")
                    else:
                        L.info(f"[Reconciler] Phase D TP1修复: {trade.pair} DB={trade.tp1_algo_id or 'None'} → OKX={okx_tp1_id} @{okx_tp1_price}")
                        trade.tp1_algo_id = okx_tp1_id

            # ———— Sync TP2 algo_id ————
            if tp2_candidates:
                best_tp2 = tp2_candidates[0]
                okx_tp2_id = best_tp2.get("algoId", "")
                okx_tp2_price = _safe_float(best_tp2.get("triggerPx"))
                if trade.tp2_algo_id != okx_tp2_id:
                    if okx_tp2_id and okx_tp2_id == trade.sl_algo_id:
                        # 角色互斥: OKX TP 候选实际是已登记的 SL 单
                        L.warning(f"[Reconciler] Phase D 角色互斥: {trade.pair} OKX候选 {okx_tp2_id} 实为SL单 → 不写入tp2_algo_id")
                    else:
                        L.info(f"[Reconciler] Phase D TP2修复: {trade.pair} DB={trade.tp2_algo_id or 'None'} → OKX={okx_tp2_id} @{okx_tp2_price}")
                        trade.tp2_algo_id = okx_tp2_id
        else:
            # OKX has no TP in batch fetch → per-symbol REST verify before clearing
            if trade.tp1_algo_id:
                if trade.sl_algo_id and trade.tp1_algo_id == trade.sl_algo_id:
                    # 角色互斥: tp1_algo_id 误标了 SL 单 id → 清除（今日 10:37 事故类型）
                    L.warning(f"[Reconciler] Phase D 角色互斥: {trade.pair} tp1_algo_id {trade.tp1_algo_id} 实为SL单 → 清除误标")
                    trade.tp1_algo_id = None
                else:
                    verified = await _fetch_okx_algo_by_id(trade.tp1_algo_id, norm_sym)
                    if verified and verified.get("state") == "live":
                        L.info(f"[Reconciler] Phase D: {trade.pair} TP1 {trade.tp1_algo_id} REST-verified alive (batch fetch missed it)")
                        okx_tp_list.append(verified)
                    else:
                        L.warning(f"[Reconciler] Phase D: {trade.pair} DB有tp1_algo_id={trade.tp1_algo_id} 但REST确认已不存在 → 清除DB引用")
                        trade.tp1_algo_id = None
            if trade.tp2_algo_id:
                if trade.sl_algo_id and trade.tp2_algo_id == trade.sl_algo_id:
                    # 角色互斥: tp2_algo_id 误标了 SL 单 id → 清除
                    L.warning(f"[Reconciler] Phase D 角色互斥: {trade.pair} tp2_algo_id {trade.tp2_algo_id} 实为SL单 → 清除误标")
                    trade.tp2_algo_id = None
                else:
                    verified = await _fetch_okx_algo_by_id(trade.tp2_algo_id, norm_sym)
                    if verified and verified.get("state") == "live":
                        L.info(f"[Reconciler] Phase D: {trade.pair} TP2 {trade.tp2_algo_id} REST-verified alive (batch fetch missed it)")
                        if verified not in okx_tp_list:
                            okx_tp_list.append(verified)
                    else:
                        L.warning(f"[Reconciler] Phase D: {trade.pair} DB有tp2_algo_id={trade.tp2_algo_id} 但REST确认已不存在 → 清除DB引用")
                        trade.tp2_algo_id = None

        # 老师目标与已登记的旧默认 TP 不同：先补老师单，再撤旧单。
        if teacher_tp_prices(trade):
            for index, target in ((1, target_tp1), (2, target_tp2)):
                if target <= 0:
                    continue
                old_id = getattr(trade, f"tp{index}_algo_id", None)
                old_order = next((o for o in okx_tp_list if o.get("algoId") == old_id), None)
                old_price = _safe_float((old_order or {}).get("triggerPx"))
                if old_id and old_price > 0 and abs(old_price - target) > max(target * 0.001, 0.01):
                    if await _fill_single_tp(trade, session, okx_contracts, tp_index=index):
                        total_created += 1
                        if getattr(trade, f"tp{index}_algo_id", None) != old_id:
                            try:
                                await runtime.cancel_algo_order(old_id, _to_okx_inst_id(norm))
                                total_cancelled += 1
                            except Exception as e:
                                L.warning(f"[Reconciler] 老师TP{index}已补挂，旧默认TP撤单失败: {e}")

        # ———— Create missing TP(s) on OKX ————
        # GUARD: partial_tp 状态下不创建 TP（仓位已进入移动止盈止损，由 TrailingChecker 管理）
        if trade.position_state != "partial_tp":
            # TP 补挂守卫: 防止把「已触发成交」(algoId 从 OKX 消失)误判为「缺失」而重复挂单
            # (TRIA #586 事故: 重复补挂的 TP2 刚挂上立即成交 → 第 4 次平仓)
            _need_tp_px = (
                (target_tp1 > 0 and not trade.tp1_algo_id and trade.position_state == "open")
                or (target_tp2 > 0 and not trade.tp2_algo_id and trade.position_state == "tp1_filled")
            )
            _ticker_px = None
            if _need_tp_px:
                try:
                    _ticker_px = (await runtime.fetch_ticker(trade.pair)).get("last")
                except Exception:
                    _ticker_px = None

            # TP1: OPEN 状态下缺失则创建
            if target_tp1 > 0 and not trade.tp1_algo_id and trade.position_state == "open":
                reason = await _tp_rehang_blocked(trade, 1, okx_contracts, _ticker_px)
                if reason:
                    L.warning(f"[Reconciler] Phase D: {trade.pair} 跳过补挂TP1 — {reason}（状态机由监控循环推进）")
                else:
                    created = await _fill_single_tp(trade, session, okx_contracts, tp_index=1)
                    if created:
                        total_created += 1
            # TP2: TP1_FILLED 状态下缺失则创建
            if target_tp2 > 0 and not trade.tp2_algo_id and trade.position_state == "tp1_filled":
                reason = await _tp_rehang_blocked(trade, 2, okx_contracts, _ticker_px)
                if reason:
                    L.warning(f"[Reconciler] Phase D: {trade.pair} 跳过补挂TP2 — {reason}（状态机由监控循环推进）")
                else:
                    created = await _fill_single_tp(trade, session, okx_contracts, tp_index=2)
                    if created:
                        total_created += 1

    session.commit()

    # =====================================================================
    # Phase E: Stale Entry Orders — cancel limit orders pending >72h
    # =====================================================================
    try:
        stale_cancelled = await _cancel_stale_entry_orders(session)
        stats["stale_entries_cancelled"] = stale_cancelled
        total_cancelled += stale_cancelled
    except Exception as e:
        L.warning(f"[Reconciler] Phase E stale entry cancel failed: {e}")

    # P1: 归档 pending_entry + amount=0 + 无未成交入场单的僵尸 trade
    try:
        zombies = await _close_zombie_pending_trades(session)
        if zombies:
            stats["zombies_archived"] = zombies
            total_cancelled += zombies
    except Exception as e:
        L.warning(f"[Reconciler] zombie cleanup failed: {e}")

    stats["positions_checked"] = trades_processed
    stats["cancelled"] = total_cancelled
    stats["created"] = total_created
    stats["status"] = "ok"

    _cycle_summary = (
        f"[Reconciler] Cycle complete: {trades_processed} trades, "
        f"{total_cancelled} cancelled, {total_created} created, "
        f"residual={stats.get('residual_algos_cancelled',0)}, "
        f"stale={stats.get('stale_entries_cancelled',0)}, "
        f"recovered={stats.get('existence_fixes',0)}"
    )
    if (
        trades_processed or total_cancelled or total_created
        or stats.get("residual_algos_cancelled") or stats.get("stale_entries_cancelled")
        or stats.get("existence_fixes")
    ):
        L.info(_cycle_summary)  # 有实际动作时保留 INFO
    else:
        L.debug(_cycle_summary)  # 空周期不刷屏，仅 debug 可见

    return stats


# ============================================================================
# Phase B Helper: Recover missing trades with SL/TP pre-sync
# ============================================================================

async def _recover_missing_trades(
    session: Session,
    positions: list[dict],
    active_trades: list[Trade],
    okx_pos_map: dict,
) -> int:
    """
    Forward match: OKX positions not in DB → recover Trade record.

    CRITICAL: Before creating a new Trade, check OKX for existing SL/TP algo orders.
    If found, sync algo_ids to the recovered Trade and mark _setup_done=True.
    This prevents ensure_protection() from creating duplicate TP/SL on the next cycle.
    """
    fixed = 0

    for pos in positions:
        pair_raw = pos.get("symbol", "")
        side = (pos.get("side") or "").lower()
        contracts = _safe_float(pos.get("contracts"))
        if contracts <= 0:
            continue

        pos_norm = _norm_pair(pair_raw)
        is_short = (side == "short")

        # Check if DB already has a matching active trade
        existing = None
        for t in active_trades:
            if not t.is_open or (t.amount or 0) <= 0:
                continue
            if _norm_pair(t.pair) == pos_norm and t.is_short == is_short:
                existing = t
                break

        if existing is not None:
            continue  # Already matched — skip

        # ———— Recover missing Trade ————
        entry_price = _safe_float(pos.get("entry_price"))
        leverage = _safe_float(pos.get("leverage"), 10)

        if entry_price <= 0 or contracts <= 0:
            L.warning(f"[Reconciler] Phase B skip recovery {pos_norm}: entry_price={entry_price} contracts={contracts}")
            continue

        base = pos_norm.replace("USDT", "")
        norm_pair_formatted = f"{base}/USDT:USDT"

        from datetime import datetime as dt
        from core.config_loader import load_config

        trade = Trade(
            pair=norm_pair_formatted,
            base_currency=base,
            stake_currency="USDT",
            exchange="okx",
            is_open=True,
            is_short=is_short,
            open_rate=entry_price,
            amount=contracts,
            amount_requested=contracts,
            open_date=dt.now(timezone.utc),
            opened_at=dt.now(timezone.utc),
            strategy="recovery",
            leverage=leverage if leverage > 0 else 10,
            trading_mode="futures",
            signal_id="recovery",
            exit_mode="auto",
            position_state="open",
            fee_open=0.0004,
            fee_close=0.0004,
            stake_amount=abs(contracts * entry_price / (leverage if leverage > 0 else 10)),
        )

        config = load_config()
        sl_pct = abs(config.get("risk", {}).get("default_stoploss_pct", 0.02))
        trade.adjust_stop_loss(entry_price, sl_pct, initial=True)

        # Verify: ensure stop_loss was actually set (defense against edge cases)
        if (trade.stop_loss or 0) <= 0:
            L.warning(f"[Recover] adjust_stop_loss returned stop_loss={trade.stop_loss}, forcing calculation")
            if trade.is_short:
                trade.stop_loss = float(entry_price * (1 + abs(sl_pct)))
            else:
                trade.stop_loss = float(entry_price * (1 - abs(sl_pct)))
            trade.stop_loss_pct = -abs(sl_pct)
            trade.initial_stop_loss = trade.stop_loss
            trade.initial_stop_loss_pct = -abs(sl_pct)
            L.info(f"[Recover] {pos_norm} stop_loss forced to {trade.stop_loss}")

        session.add(trade)
        session.flush()

        # ———— Pre-sync existing SL/TP from OKX ————
        norm_sym = _norm_symbol(trade.pair)
        try:
            algo_orders = await _fetch_okx_algo_orders(norm_sym)
            for o in algo_orders:
                o_inst = _norm_pair(o.get("instId", ""))
                if o_inst != pos_norm:
                    continue
                if o.get("state") != "live":
                    continue
                if _is_algo_sl(o):
                    trade.sl_algo_id = o.get("algoId", "")
                    L.info(f"[Recover] {trade.pair} 发现已有SL algoId={trade.sl_algo_id}")
                elif _is_algo_tp(o):
                    trade.tp1_algo_id = o.get("algoId", "")
                    L.info(f"[Recover] {trade.pair} 发现已有TP algoId={trade.tp1_algo_id}")

            # Mark _setup_done to prevent ensure_protection from re-creating
            from sqlalchemy.orm.attributes import flag_modified
            meta = dict(trade.signal_meta or {})
            meta["_setup_done"] = True
            trade.signal_meta = meta
            flag_modified(trade, "signal_meta")
            session.flush()
        except Exception as e:
            L.warning(f"[Recover] {trade.pair} pre-sync SL/TP failed: {e}")

        L.success(f"[Reconciler] Phase B recovered trade #{trade.id} for {pos_norm} "
                  f"{'SHORT' if is_short else 'LONG'} @ {entry_price} x{leverage} "
                  f"SL={trade.sl_algo_id or 'pending'} TP={trade.tp1_algo_id or 'pending'}")
        fixed += 1

    return fixed


# ============================================================================
# Phase C: Cleanup residual algo orders for positions that no longer exist
# ============================================================================

async def _cleanup_residual_algos(
    session: Session,
    okx_pos_map: dict,
) -> int:
    """
    Cancel SL/TP/trailing algo orders on OKX that don't correspond to any
    live position. This catches orders that were left behind when a position
    was manually closed on OKX or closed by a TP/SL fill.

    Safety rule: ONLY trusts OKX REST position data (source of truth).
    If OKX says no position → algo is orphaned → cancel it.
    DB references are cleaned up but NEVER trusted to block cancellation.
    """
    cancelled = 0
    all_algos = await _fetch_all_algo_orders()
    if not all_algos:
        return 0

    live_algos = [a for a in all_algos if a.get("state") == "live"]
    if not live_algos:
        return 0

    # Build set of known algo_ids from DB open trades
    active_trades = Trade.get_active_trades(session)
    known_algo_ids: set[str] = set()
    for t in active_trades:
        if t.sl_algo_id:
            known_algo_ids.add(t.sl_algo_id)
        if t.tp1_algo_id:
            known_algo_ids.add(t.tp1_algo_id)
        if getattr(t, 'tp2_algo_id', None):
            known_algo_ids.add(t.tp2_algo_id)
        if getattr(t, 'tp3_algo_id', None):
            known_algo_ids.add(t.tp3_algo_id)

    # Also query ALL trades (including closed) for stale reference cleanup
    all_trades_for_cleanup = session.query(Trade).all()

    for a in live_algos:
        aid = a.get("algoId", "")
        inst = _norm_pair(a.get("instId", ""))
        pos_side = (a.get("posSide") or "").lower()

        # Check 1: Does this (symbol, side) have a live position on OKX?
        has_position = (inst, pos_side) in okx_pos_map

        # Check 2: Is this algo_id referenced by any DB trade?
        is_known = aid in known_algo_ids

        if not has_position:
            # OKX REST says no position → algo is orphaned regardless of DB
            # DB is a cache, never trusted for reconciliation decisions
            try:
                await runtime.cancel_algo_order(aid, _to_okx_inst_id(inst))
                reason = "孤立(无仓位)" if not is_known else "DB有引用但OKX无仓位(DB过期)"
                L.warning(f"[Reconciler] Phase C 残留清理: 取消{inst} {pos_side} algoId={aid} ({reason})")
                cancelled += 1
                # Clean up stale DB references (ALL trades, regardless of is_known)
                # Closed trades may reference stale algo_ids not in active_trades
                for t in all_trades_for_cleanup:
                    if t.sl_algo_id == aid:
                        t.sl_algo_id = None
                        L.info(f"[Reconciler] Phase C 清除DB引用: Trade#{t.id} sl_algo_id={aid}")
                    if t.tp1_algo_id == aid:
                        t.tp1_algo_id = None
                        L.info(f"[Reconciler] Phase C 清除DB引用: Trade#{t.id} tp1_algo_id={aid}")
                    if getattr(t, 'tp2_algo_id', None) == aid:
                        t.tp2_algo_id = None
                    if getattr(t, 'tp3_algo_id', None) == aid:
                        t.tp3_algo_id = None
            except Exception as e:
                err = str(e)
                if "51400" in err or "51001" in err:
                    pass  # Already gone — OK
                else:
                    L.warning(f"[Reconciler] Phase C 取消失败 algoId={aid}: {err[:80]}")
        elif not is_known:
            # Position exists but algo_id not in DB → don't touch (might be manual order)
            pass

    if cancelled > 0:
        L.warning(f"[Reconciler] Phase C 残留清理完成: 取消 {cancelled} 个孤儿算法单")
        session.flush()  # persist DB reference cleanup

    return cancelled


# ============================================================================
# Phase D Helpers: Fill individual SL/TP
# ============================================================================

def _can_split_position(trade: Trade, contracts: float, split_ratio: float = 0.5) -> bool:
    """Check if splitting the position would produce a valid order size."""
    from exchange_engine.exchange import normalize_order_amount
    half = normalize_order_amount(trade.pair, contracts * split_ratio, trade.exchange or "okx")
    return half is not None and half > 0


async def _fill_single_sl(trade: Trade, session: Session, okx_contracts: float) -> bool:
    """Create SL for a single trade if missing. Returns True if created."""
    _meta = dict(trade.signal_meta or {})
    was_skipped = _meta.get("_sl_skipped", False)
    skipped_at = _meta.get("_sl_skipped_contracts", 0)

    if was_skipped and skipped_at == okx_contracts:
        return False  # Marked skip, position unchanged

    if not _can_split_position(trade, okx_contracts, 1.0):
        L.info(f"[Reconciler] Phase D {trade.pair} 仓位太小无法挂SL ({okx_contracts}张)，标记跳过")
        _meta["_sl_skipped"] = True
        _meta["_sl_skipped_contracts"] = okx_contracts
        trade.signal_meta = _meta
        return False

    _meta.pop("_sl_skipped", None)
    _meta.pop("_sl_skipped_contracts", None)
    trade.signal_meta = _meta

    try:
        from core.protection_creator import protection_creator
        result = await protection_creator.create_sl(trade, session)
        if result.success:
            L.success(f"[Reconciler] Phase D 补挂SL | {trade.pair} algoId={result.algo_id} 价格={trade.stop_loss}")
            return True
        elif result.algo_id == "skipped_too_small":
            L.info(f"[Reconciler] Phase D {trade.pair} SL跳过(仓位太小)，不再重试")
            _meta["_sl_skipped"] = True
            _meta["_sl_skipped_contracts"] = okx_contracts
            trade.signal_meta = _meta
    except Exception as e:
        L.error(f"[Reconciler] Phase D 补挂SL异常 {trade.pair}: {e}")
    return False


async def _fill_single_tp(trade: Trade, session: Session, okx_contracts: float,
                          tp_index: int = 1) -> bool:
    """Create TP{tp_index} for a single trade if missing. Returns True if created."""
    if tp_price_for(trade, tp_index) is None:
        return False
    _meta = dict(trade.signal_meta or {})
    skip_key = f"_tp{tp_index}_skipped"
    skip_contracts_key = f"_tp{tp_index}_skipped_contracts"
    was_skipped = _meta.get(skip_key, False)
    skipped_at = _meta.get(skip_contracts_key, 0)

    if was_skipped and skipped_at == okx_contracts:
        return False

    # split 比例与 protection_creator.create_tp 的数量计算保持一致:
    # TP1=30%; TP2 在 tp1_filled 状态(补挂,仓位≈剩余70%)为 50%, open 状态(初始全仓)为 35%
    if single_teacher_tp(trade):
        split_ratio = 1.0
    elif tp_index == 1:
        split_ratio = 0.30
    elif getattr(trade, 'position_state', None) == "tp1_filled":
        split_ratio = 0.50
    else:
        split_ratio = 0.35
    if not _can_split_position(trade, okx_contracts, split_ratio):
        L.info(f"[Reconciler] Phase D {trade.pair} 仓位太小无法挂TP{tp_index} ({okx_contracts}张, ratio={split_ratio})，标记跳过")
        _meta[skip_key] = True
        _meta[skip_contracts_key] = okx_contracts
        trade.signal_meta = _meta
        return False

    _meta.pop(skip_key, None)
    _meta.pop(skip_contracts_key, None)
    trade.signal_meta = _meta

    try:
        from core.protection_creator import protection_creator
        result = await protection_creator.create_tp(trade, session, tp_index=tp_index)
        if result.success:
            tp_label = f"TP{tp_index}" if tp_index > 1 else "TP1"
            L.success(f"[Reconciler] Phase D 补挂{tp_label} | {trade.pair} algoId={result.algo_id}")
            return True
        elif result.algo_id == "skipped_too_small":
            L.info(f"[Reconciler] Phase D {trade.pair} TP{tp_index}跳过(仓位太小)，不再重试")
            _meta[skip_key] = True
            _meta[skip_contracts_key] = okx_contracts
            trade.signal_meta = _meta
    except Exception as e:
        L.error(f"[Reconciler] Phase D 补挂TP{tp_index}异常 {trade.pair}: {e}")
    return False


# ============================================================================
# [DEPRECATED] Phase E: Dedup — superseded by inline dedup in Phase D
# ============================================================================
# Dedup of duplicate SL/TP orders is now handled inline during Phase D
# (see lines ~1019-1093 in reconcile()). This standalone function is kept
# for reference but is NOT called in the normal reconcile() cycle.
# ============================================================================

async def _dedup_algo_orders(session: Session, active_trades: list[Trade]) -> int:
    """
    [DEPRECATED] Fetch all live algo orders from OKX, group by (symbol, side),
    detect duplicate SL/TP and cancel extras (keep the one closest to target price).

    Superseded by inline dedup in Phase D of reconcile().
    Kept for reference and manual use only.
    """
    all_algos = await _fetch_all_algo_orders()
    live_algos = [a for a in all_algos if a.get("state") == "live"]
    L.info(f"[Reconciler] Phase E 去重: {len(live_algos)} 个活跃算法单")

    if len(live_algos) <= 1:
        return 0

    # Group by (norm_pair, posSide)
    by_key: dict[tuple[str, str], list[dict]] = {}
    for a in live_algos:
        inst = _norm_pair(a.get("instId", ""))
        pos_side = (a.get("posSide") or "").lower()
        if not inst or not pos_side:
            continue
        by_key.setdefault((inst, pos_side), []).append(a)

    cancelled = 0
    for (norm, pos_side), orders in by_key.items():
        if len(orders) <= 1:
            continue

        sl_orders = [o for o in orders if _is_algo_sl(o)]
        tp_orders = [o for o in orders if _is_algo_tp(o)]

        # Find target prices from matching DB trade
        target_sl = 0.0
        target_tp = 0.0
        for t in active_trades:
            if t.is_open and _norm_pair(t.pair) == norm:
                t_side = "short" if t.is_short else "long"
                if t_side == pos_side:
                    target_sl = t.stop_loss if t.stop_loss > 0 else target_sl
                    target_tp = t.tp1_price if (t.tp1_price or 0) > 0 else target_tp
                    break

        # Dedup SL
        if len(sl_orders) > 1:
            sl_orders.sort(key=lambda o: abs(_safe_float(o.get("triggerPx")) - target_sl))
            for dup in sl_orders[1:]:
                aid = dup.get("algoId", "")
                if aid:
                    try:
                        await runtime.cancel_algo_order(aid, _to_okx_inst_id(norm))
                        L.warning(f"[Reconciler] Phase E 去重SL: 取消 algoId={aid} (保留 {sl_orders[0].get('algoId','?')})")
                        cancelled += 1
                    except Exception as e:
                        L.warning(f"[Reconciler] Phase E 去重SL失败 algoId={aid}: {e}")

        # Dedup TP
        if len(tp_orders) > 1:
            tp_orders.sort(key=lambda o: abs(_safe_float(o.get("triggerPx")) - target_tp))
            for dup in tp_orders[1:]:
                aid = dup.get("algoId", "")
                if aid:
                    try:
                        await runtime.cancel_algo_order(aid, _to_okx_inst_id(norm))
                        L.warning(f"[Reconciler] Phase E 去重TP: 取消 algoId={aid} (保留 {tp_orders[0].get('algoId','?')})")
                        cancelled += 1
                    except Exception as e:
                        L.warning(f"[Reconciler] Phase E 去重TP失败 algoId={aid}: {e}")

    if cancelled > 0:
        L.warning(f"[Reconciler] Phase E 去重完成: 取消 {cancelled} 个重复单")

    return cancelled


# ============================================================================
# Phase E: Cancel stale entry orders (limit orders pending >72h)
# ============================================================================

async def _cancel_stale_entry_orders(session: Session) -> int:
    """
    Cancel limit entry orders that have been pending longer than ENTRY_ORDER_TTL_HOURS.
    Freqtrade-equivalent: unfilledtimeout.entry with custom TTL.

    For orders where the trade has never filled (amount=0, position_state=pending_entry):
      - Cancel order + close trade entirely
    For orders where the trade partially filled:
      - Cancel only the unfilled order, keep the position
    """
    cancelled = 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ENTRY_ORDER_TTL_HOURS)

    # Find stale entry orders
    stale_orders = session.query(Order).filter(
        Order.ft_order_role == "entry",
        Order.ft_is_open == True,
        Order.order_date < cutoff,  # older than 72h
    ).all()

    if not stale_orders:
        return 0

    L.info(f"[Reconciler] Phase E: 发现 {len(stale_orders)} 个超时入场单 (> {ENTRY_ORDER_TTL_HOURS}h)")

    for o in stale_orders:
        trade = o.trade
        try:
            await runtime.cancel_order(o.order_id, o.ft_pair)
            o.ft_is_open = False
            o.status = "expired"
            cancelled += 1

            if trade and trade.is_open and (trade.amount or 0) <= 0:
                # Trade never filled → close it
                trade.is_open = False
                trade.exit_reason = f"entry_expired_{ENTRY_ORDER_TTL_HOURS}h"
                trade.close_date = now
                trade.position_state = "closed"
                L.info(f"[Reconciler] Phase E: {trade.pair} 入场超时 → 关闭Trade #{trade.id}")

        except Exception as e:
            err = str(e)
            if "51400" in err:
                # Already gone from exchange
                o.ft_is_open = False
                o.status = "expired"
                cancelled += 1
            else:
                L.warning(f"[Reconciler] Phase E 取消超时单失败 {o.ft_pair} {o.order_id}: {err[:80]}")

    if cancelled > 0:
        session.flush()
        L.warning(f"[Reconciler] Phase E 超时取消完成: {cancelled} 个入场单")

    return cancelled


async def _close_zombie_pending_trades(session: Session) -> int:
    """
    P1: 归档 pending_entry 且 amount=0 且无未成交入场单的僵尸 trade。

    成因：入场单已被取消/过期但 trade 未被关闭（历史 bug 残留）。这类 trade
    既无持仓也无待成交单，却停留在 pending_entry。check_entry_order 因
    has_open_orders=False 提前返回不会清理；Phase E 只扫"仍 open 的入场单"
    也触及不到。在此统一归档。

    安全性：pending_entry + amount=0 + 无 open 入场单 + OKX 确认无仓位 = 僵尸。
    OKX 仓位快照不可用（None）时跳过，遵循"API 不可用不假设"原则。
    """
    closed = 0
    now = datetime.now(timezone.utc)
    for trade in Trade.get_active_trades(session):
        if trade.position_state != "pending_entry":
            continue
        if (trade.amount or 0) > 0:
            continue  # 有成交量，不是僵尸
        has_open_entry = any(
            (o.ft_is_open and (o.ft_order_role or "") == "entry")
            for o in (trade.orders or [])
        )
        if has_open_entry:
            continue  # 仍在等待成交，不是僵尸
        # OKX 真相源：确认无仓位才关闭
        try:
            pos = runtime.get_position(trade.pair)
        except Exception:
            pos = None
        if pos is None:
            continue  # 快照不可用，跳过（不假设无仓位）
        if float(pos.get("contracts", 0) or 0) > 0:
            continue  # OKX 有仓位，交由 Phase D 同步，不在此关闭
        trade.is_open = False
        trade.exit_reason = trade.exit_reason or "zombie_pending_entry"
        trade.close_date = trade.close_date or now
        trade.position_state = "closed"
        closed += 1
        L.warning(f"[Reconciler] Phase E: 僵尸归档 #{trade.id} {trade.pair} "
                  f"pending_entry+amount=0+无挂单 -> closed")
    if closed > 0:
        session.flush()
    return closed


# ============================================================================
# Event-Driven: ensure_protection (called ONCE after entry fill)
# ============================================================================

def _heal_open_rate_from_okx(trade: Trade) -> bool:
    """
    P0: 当 trade.open_rate 缺失时，从 OKX 仓位快照回填真实入场价。

    限价单挂单时 open_rate=0；成交后由 check_entry_order -> update_trade 同步。
    但 EntryFillHandler（WS 事件驱动）可能在 check_entry_order 之前触发
    ensure_protection，此时 open_rate 仍为 0。OKX = 唯一真相源，从仓位快照
    的 entry_price 回填。仅在 open_rate<=0 时回填，绝不覆盖已有值。
    """
    if (trade.open_rate or 0) > 0:
        return False
    try:
        pos = runtime.get_position(trade.pair)
    except Exception:
        return False
    if not pos:
        return False
    okx_entry = _safe_float(pos.get("entry_price"))
    if okx_entry > 0:
        trade.open_rate = okx_entry
        L.info(f"[EnsureProtection] {trade.pair} 从OKX回填open_rate: {okx_entry}")
        return True
    return False


def _recompute_initial_stop_loss(trade: Trade) -> None:
    """
    P0: 限价单成交后用真实 open_rate 重算初始 stop_loss。

    限价单挂单时 open_rate=0，signal_consumer 阶段 adjust_stop_loss(0, ...)
    把 stop_loss 设成 0（老师绝对止损也因 open_rate=0 的百分比比较失效而丢失）。
    成交后用真实 open_rate 计算保护价：老师绝对价优先，缺失时用固定比例。
    只在 stop_loss<=0 时执行，绝不覆盖已正确设置或被 trailing 修改的值。
    直接写字段，不调用 adjust_stop_loss（后者对 stop_loss=0 的 short 仓位有缺陷）。
    """
    if (trade.stop_loss or 0) > 0:
        return
    if (trade.open_rate or 0) <= 0:
        return

    from core.protection_targets import teacher_sl_price
    meta = trade.signal_meta or {}
    sig_sl = teacher_sl_price(trade)
    # 与 signal_consumer 保持同一来源，确保限价单与市价单得到一致的 SL
    # 使用 config_loader 默认值 2%，不再硬编码 4%
    from core.config_loader import load_config as _load_cfg
    _cfg = _load_cfg()
    default_sl_pct = abs(_cfg.get("risk", {}).get("default_stoploss_pct", 0.02))

    if sig_sl is not None:
        teacher_sl_pct = abs(sig_sl - trade.open_rate) / max(trade.open_rate, 1e-8)
        sl_price = sig_sl
        sl_pct = -teacher_sl_pct
    else:
        sl_price = (float(trade.open_rate * (1 + default_sl_pct)) if trade.is_short
                    else float(trade.open_rate * (1 - default_sl_pct)))
        sl_pct = -default_sl_pct

    trade.stop_loss = sl_price
    trade.initial_stop_loss = sl_price
    trade.stop_loss_pct = sl_pct
    trade.initial_stop_loss_pct = sl_pct
    if meta.get("sl_type") == "trailing":
        trade.is_stop_loss_trailing = True

    L.info(
        f"[EnsureProtection] {trade.pair} 限价成交后重算SL: "
        f"open_rate={trade.open_rate} SL={sl_price} ({abs(sl_pct)*100:.2f}%)"
    )


async def ensure_protection(trade: Trade, session: Session,
                          recalculate: bool = False) -> dict:
    """
    Event-driven protection setup. Called ONCE after entry is confirmed filled.
    Creates SL and TP1 if they don't exist on OKX.

    v6: recalculate=True mode for batch fill coordination.
    When recalculate=True, skips _setup_done check and recalculates
    TP1/SL based on OKX real position data.

    IDEMPOTENT: checks _setup_done flag first (unless recalculate=True).
    REST-FIRST: verifies OKX before creating.
    API FAILURE = ABORT (never assume "missing").
    """
    from core.protection_creator import protection_creator
    from core.protection_targets import teacher_sl_price, tp_price_for

    result = {"sl": "skipped", "tp": "skipped", "tp2": "skipped"}

    if not trade.is_open or (trade.amount or 0) <= 0:
        return result

    # ———— GUARD: Already done? (skip in recalculate mode) ————
    meta = trade.signal_meta or {}
    if not recalculate and meta.get("_setup_done"):
        return result

    trade_ex = trade.exchange or "okx"
    norm_sym = _norm_symbol(trade.pair)
    setup_ok = True  # Track whether setup completed successfully

    # ---- P0: 限价单成交后用真实 open_rate 重算 stop_loss ----
    # 限价单挂单时 open_rate=0，signal_consumer 阶段把 stop_loss 设成 0。
    # 成交后 update_trade 才更新 open_rate；若 EntryFillHandler 先于
    # check_entry_order 触发，open_rate 可能仍为 0 -> 从 OKX 仓位快照回填。
    # 然后 ensure_protection 才能创建 SL，避免裸仓（仅有 TP 无 SL）。
    if (trade.stop_loss or 0) <= 0:
        if (trade.open_rate or 0) <= 0:
            _heal_open_rate_from_okx(trade)
        _recompute_initial_stop_loss(trade)

    # ———— SL ————
    if (trade.stop_loss or 0) > 0:
        # PRE-FLIGHT: REST check if SL already exists on OKX
        try:
            algo_orders = await _fetch_okx_algo_orders(norm_sym)
            sl_orders = [o for o in algo_orders if
                _norm_pair(o.get("instId", "")) == _norm_pair(norm_sym)
                and _is_algo_sl(o)
            ]
            teacher_sl = teacher_sl_price(trade)
            has_sl = any(
                not teacher_sl or abs(
                    _safe_float(o.get("slTriggerPx") or o.get("triggerPx")) - teacher_sl
                ) <= max(teacher_sl * 0.001, 0.01)
                for o in sl_orders
            )
            old_sl_id = str(trade.sl_algo_id or "")
            stale_sl_ids = [str(o.get("algoId")) for o in sl_orders
                            if teacher_sl
                            and abs(_safe_float(o.get("slTriggerPx") or o.get("triggerPx"))
                                    - teacher_sl) > max(teacher_sl * 0.001, 0.01)
                            and (str(o.get("algoId") or "") == old_sl_id
                                 or str(o.get("clOrdId") or "").lower().startswith("botsl"))]
        except Exception as e:
            # API failure → ABORT, mark done so we don't loop forever
            L.warning(f"[EnsureProtection] {trade.pair} REST pre-flight FAILED, aborting: {e}")
            result["sl"] = "api_failed"
            setup_ok = False

        if setup_ok:
            if has_sl:
                if teacher_sl:
                    for sl_order in sl_orders:
                        if abs(_safe_float(sl_order.get("slTriggerPx") or sl_order.get("triggerPx"))
                               - teacher_sl) <= max(teacher_sl * 0.001, 0.01):
                            trade.sl_algo_id = sl_order.get("algoId") or trade.sl_algo_id
                            break
                L.info(f"[EnsureProtection] {trade.pair} SL already exists on OKX, skipping")
                result["sl"] = "already_exists"
            else:
                if teacher_sl and stale_sl_ids:
                    protection_creator.bump_clordid_version(trade.id, "sl")
                sl_result = await protection_creator.create_sl(trade, session)
                if sl_result.success:
                    L.success(f"[EnsureProtection] {trade.pair} SL created algoId={sl_result.algo_id}")
                    result["sl"] = "created"
                else:
                    L.error(f"[EnsureProtection] {trade.pair} SL create failed: {sl_result.error}")
                    result["sl"] = "failed"

            # A default bot SL does not satisfy a teacher SL. Only remove old
            # bot orders after the teacher order is confirmed or created.
            if teacher_sl and result["sl"] in ("created", "already_exists"):
                for old_id in stale_sl_ids:
                    if old_id:
                        try:
                            await runtime.cancel_algo_order(old_id, _to_okx_inst_id(norm_sym))
                        except Exception as e:
                            L.warning(f"[EnsureProtection] {trade.pair} old SL cancel failed: {e}")

    # ———— TP1 ————
    if setup_ok:
        tp1_price = tp_price_for(trade, 1) or 0.0
        if tp1_price > 0:
            trade.tp1_price = tp1_price

        # SOL fix: 如果 open_rate 仍为 0（限价单未成交），从 OKX 回填后计算 TP1
        if tp1_price <= 0 and (trade.open_rate or 0) <= 0:
            _heal_open_rate_from_okx(trade)
            if trade.open_rate > 0:
                tp1_price = tp_price_for(trade, 1) or 0.0
                trade.tp1_price = tp1_price
                L.info(f"[EnsureProtection] {trade.pair} 回填open_rate后计算TP1: {tp1_price}")

        if tp1_price > 0:
            try:
                algo_orders = await _fetch_okx_algo_orders(norm_sym)
                has_tp = any(
                    _norm_pair(o.get("instId", "")) == _norm_pair(norm_sym)
                    and _is_algo_tp(o)
                    and abs(_safe_float(o.get("tpTriggerPx") or o.get("triggerPx")) - tp1_price) / tp1_price < 0.001
                    for o in algo_orders
                )
            except Exception as e:
                L.warning(f"[EnsureProtection] {trade.pair} REST TP pre-flight FAILED: {e}")
                result["tp"] = "api_failed"
            else:
                if has_tp:
                    L.info(f"[EnsureProtection] {trade.pair} TP already exists on OKX, skipping")
                    result["tp"] = "already_exists"
                else:
                    tp_result = await protection_creator.create_tp(trade, session)
                    if tp_result.success:
                        L.success(f"[EnsureProtection] {trade.pair} TP1 created algoId={tp_result.algo_id}")
                        result["tp"] = "created"
                    elif tp_result.algo_id == "skipped_too_small":
                        # Position too small to split for TP — permanent condition
                        L.info(f"[EnsureProtection] {trade.pair} TP1 仓位太小无法拆分: {tp_result.error}")
                        result["tp"] = "skipped_too_small"
                    else:
                        L.error(f"[EnsureProtection] {trade.pair} TP1 create failed: {tp_result.error}")
                        result["tp"] = "failed"

    # ———— TP2 (35% at +4%) ————
    if setup_ok and result.get("tp") not in ("failed", "api_failed"):
        tp2_price = tp_price_for(trade, 2) or 0.0
        if tp2_price > 0:
            trade.tp2_price = tp2_price

        if tp2_price > 0:
            try:
                algo_orders = await _fetch_okx_algo_orders(norm_sym)
                has_tp2 = any(
                    _norm_pair(o.get("instId", "")) == _norm_pair(norm_sym)
                    and _is_algo_tp(o)
                    and abs(_safe_float(o.get("tpTriggerPx") or o.get("triggerPx")) - tp2_price) / tp2_price < 0.001
                    for o in algo_orders
                )
            except Exception as e:
                L.warning(f"[EnsureProtection] {trade.pair} REST TP2 pre-flight FAILED: {e}")
                result["tp2"] = "api_failed"
            else:
                if has_tp2:
                    L.info(f"[EnsureProtection] {trade.pair} TP2 already exists on OKX, skipping")
                    result["tp2"] = "already_exists"
                else:
                    tp2_result = await protection_creator.create_tp(trade, session, tp_index=2)
                    if tp2_result.success:
                        L.success(f"[EnsureProtection] {trade.pair} TP2 created algoId={tp2_result.algo_id}")
                        result["tp2"] = "created"
                    elif tp2_result.algo_id == "skipped_too_small":
                        L.info(f"[EnsureProtection] {trade.pair} TP2 仓位太小无法拆分: {tp2_result.error}")
                        result["tp2"] = "skipped_too_small"
                    else:
                        L.error(f"[EnsureProtection] {trade.pair} TP2 create failed: {tp2_result.error}")
                        result["tp2"] = "failed"

    # ———— Mark done ONLY if setup actually succeeded ————
    # Previously _setup_done was set unconditionally, which prevented
    # the 5s order_monitor from retrying when SL/TP creation failed
    # (e.g. OKX REST pre-flight error, position not yet visible, etc.).
    #
    # v6: "skipped_too_small" is a PERMANENT condition for TP (position
    # can't be split below minimum order size). Treat it as "ok" for
    # _setup_done purposes — otherwise EntryFillHandler retries every 5s
    # and creates duplicate SLs after cooldown expires.
    sl_ok = result.get("sl") in ("created", "already_exists")
    tp_ok = result.get("tp") in ("created", "already_exists", "skipped", "skipped_too_small")
    # TP2 is secondary — don't block _setup_done if it fails (can be retried later)
    tp2_ok = result.get("tp2") in ("created", "already_exists", "skipped", "skipped_too_small", "api_failed")

    from sqlalchemy.orm.attributes import flag_modified
    if sl_ok and tp_ok:
        meta = dict(trade.signal_meta or {})
        meta["_setup_done"] = True
        trade.signal_meta = meta
        flag_modified(trade, "signal_meta")  # CRITICAL: force SQLAlchemy JSON detection
        session.flush()
        L.info(f"[EnsureProtection] {trade.pair} 保护设置完成 SL={result.get('sl')} TP1={result.get('tp')} TP2={result.get('tp2')}")
    elif recalculate:
        # v6: In recalculate mode, don't mark _setup_done — allow future batch fills
        # to trigger again. The _setup_done flag is set by the initial ensure_protection
        # call (non-recalculate mode) only.
        session.flush()
        L.info(f"[EnsureProtection] {trade.pair} 分批重算完成 SL={result.get('sl')} TP1={result.get('tp')} TP2={result.get('tp2')}")
    else:
        L.warning(f"[EnsureProtection] {trade.pair} 未完全成功 SL={result.get('sl')} TP1={result.get('tp')} TP2={result.get('tp2')}，不标记_done，允许重试")

    session.commit()
    return result


# ============================================================================
# Event-Driven: reconcile_on_batch_fill (called on EACH batch limit fill)
# ============================================================================

async def reconcile_on_batch_fill(trade: Trade, session: Session, okx_contracts: float) -> dict:
    """
    v6: Event-driven batch fill coordination. Called when a limit_range trade's
    position size INCREASES (new batch filled). Recalculates TP1 and SL using
    OKX real position data as the single source of truth.

    CORE PRINCIPLE: OKX real position is the ONLY source of truth.
    DB fields (trade.amount, trade.open_rate) are updated from OKX, not trusted.

    Flow:
      1. REST fetch_positions() → OKX real contracts, entry_price
      2. Update trade.open_rate and trade.amount from OKX (if changed)
      3. Recalculate SL price based on latest entry_price
      4. Recalculate TP1 price based on latest entry_price
      5. REST check existing protection orders on OKX
      6. Compare desired vs actual → cancel+create only if changed
      7. Prevent duplicate orders (PRE-FLIGHT REST check)

    IDEMPOTENT: uses PRE-FLIGHT REST checks before every create.
    API FAILURE = ABORT (never assume "missing" or "exists").
    """
    from core.protection_creator import protection_creator
    from exchange_engine.exchange import normalize_order_amount

    result = {"sl": "skipped", "tp": "skipped", "contracts": okx_contracts}

    if not trade.is_open or okx_contracts <= 0:
        return result

    trade_ex = trade.exchange or "okx"
    norm_sym = _norm_symbol(trade.pair)

    # ———— Step 1: Get OKX real position data (REST, source of truth) ————
    from exchange_engine.exchange import fetch_positions
    try:
        positions = await fetch_positions(exchange=trade_ex)
    except Exception as e:
        L.error(f"[BatchFill] {trade.pair} REST fetch_positions FAILED: {e}")
        result["error"] = "rest_fetch_failed"
        return result

    real_contracts = 0.0
    real_entry_price = 0.0
    for p in positions:
        if _norm_symbol(p.get("symbol", "")) == norm_sym:
            real_contracts = _safe_float(p.get("contracts"))
            real_entry_price = _safe_float(p.get("entry_price"))
            break

    if real_contracts <= 0:
        L.warning(f"[BatchFill] {trade.pair} OKX无仓位，跳过")
        return result

    # ———— Step 2: Sync trade from OKX real data ————
    position_changed = False
    if abs(trade.amount - real_contracts) > 1e-8:
        L.info(f"[BatchFill] {trade.pair} 仓位同步: DB={trade.amount} → OKX={real_contracts}")
        trade.amount = real_contracts
        position_changed = True

    if real_entry_price > 0 and abs(trade.open_rate - real_entry_price) > 1e-8:
        old_rate = trade.open_rate
        L.info(f"[BatchFill] {trade.pair} 入场价同步: DB={old_rate} → OKX={real_entry_price}")
        trade.open_rate = real_entry_price
        position_changed = True

    # ———— Step 3: Recalculate SL based on latest entry_price ————
    if trade.open_rate > 0:
        from core.config_loader import load_config as _load_cfg
        cfg = _load_cfg()
        sl_pct = abs(cfg.get("risk", {}).get("default_stoploss_pct", 0.02))

        # Check if teacher set a specific SL
        meta = trade.signal_meta or {}
        sig_sl = teacher_sl_price(trade)

        if sig_sl is not None and trade.open_rate > 0:
            teacher_sl_pct = abs(sig_sl - trade.open_rate) / max(trade.open_rate, 1e-8)
            new_sl_price = float(sig_sl)
            new_sl_pct = -teacher_sl_pct
        else:
            new_sl_price = (float(trade.open_rate * (1 + sl_pct)) if trade.is_short
                            else float(trade.open_rate * (1 - sl_pct)))
            new_sl_pct = -sl_pct

        sl_price_changed = abs(trade.stop_loss - new_sl_price) > max(abs(new_sl_price) * 0.001, 0.1)
        if sl_price_changed or position_changed:
            old_sl = trade.stop_loss
            trade.stop_loss = new_sl_price
            trade.initial_stop_loss = new_sl_price
            trade.stop_loss_pct = new_sl_pct
            trade.initial_stop_loss_pct = new_sl_pct
            L.info(f"[BatchFill] {trade.pair} SL重算: {old_sl:.4f} → {new_sl_price:.4f} "
                   f"({abs(new_sl_pct)*100:.2f}%)")

    # ———— Step 4: Recalculate TP1 based on latest entry_price ————
    tp1_price = tp_price_for(trade, 1) or 0.0
    if tp1_price > 0:
        trade.tp1_price = tp1_price

    # ———— Step 5: REST check existing protection orders ————
    try:
        algo_orders = await _fetch_okx_algo_orders(norm_sym)
    except Exception as e:
        L.error(f"[BatchFill] {trade.pair} REST fetch_algo FAILED: {e}")
        result["error"] = "rest_algo_failed"
        return result

    has_sl = any(
        _norm_pair(o.get("instId", "")) == _norm_pair(norm_sym)
        and _is_algo_sl(o)
        for o in algo_orders
    )
    has_tp = any(
        _norm_pair(o.get("instId", "")) == _norm_pair(norm_sym)
        and _is_algo_tp(o)
        for o in algo_orders
    )

    # ———— Step 6: SL — check if update needed ————
    if (trade.stop_loss or 0) > 0 and real_contracts > 0:
        sl_amt = normalize_order_amount(trade.pair, real_contracts, trade_ex)
        if sl_amt is not None and sl_amt > 0:
            if has_sl:
                # SL exists — check if price/amount needs update
                # Find existing SL details
                existing_sl_price = 0.0
                existing_sl_amount = 0.0
                existing_sl_id = ""
                for o in algo_orders:
                    if (_norm_pair(o.get("instId", "")) == _norm_pair(norm_sym)
                            and _is_algo_sl(o)):
                        existing_sl_price = _safe_float(o.get("triggerPx"))
                        existing_sl_amount = _safe_float(o.get("sz"))
                        existing_sl_id = o.get("algoId", "")
                        break

                price_mismatch = (existing_sl_price > 0 and
                                  abs(existing_sl_price - trade.stop_loss) > max(abs(trade.stop_loss) * 0.005, 0.5))
                amount_mismatch = (existing_sl_amount > 0 and
                                   abs(existing_sl_amount - sl_amt) > max(sl_amt * 0.01, 1.0))

                if price_mismatch or amount_mismatch:
                    L.info(f"[BatchFill] {trade.pair} SL需要更新: "
                           f"价格 OKX={existing_sl_price}→目标={trade.stop_loss} "
                           f"数量 OKX={existing_sl_amount}→目标={sl_amt}")

                    # Cancel old SL FIRST to avoid OKX duplicate rejection
                    if existing_sl_id:
                        try:
                            await runtime.cancel_algo_order(existing_sl_id,
                                                            _to_okx_inst_id(_norm_pair(trade.pair)))
                            trade.sl_algo_id = None
                            L.info(f"[BatchFill] {trade.pair} 旧SL已取消 algoId={existing_sl_id}")
                        except Exception as e:
                            L.warning(f"[BatchFill] {trade.pair} 取消旧SL失败: {e}")

                    # Create new SL
                    protection_creator.bump_clordid_version(trade.id, "sl")
                    sl_result = await protection_creator.create_sl(trade, session)
                    if sl_result.success:
                        L.success(f"[BatchFill] {trade.pair} SL已更新 algoId={sl_result.algo_id} "
                                  f"价格={trade.stop_loss} 数量={sl_amt}")
                        result["sl"] = "updated"
                    else:
                        L.error(f"[BatchFill] {trade.pair} SL更新失败: {sl_result.error}")
                        result["sl"] = "failed"
                else:
                    L.debug(f"[BatchFill] {trade.pair} SL无需更新 (价格/数量匹配)")
                    result["sl"] = "unchanged"
            else:
                # SL missing → create
                if not trade.sl_algo_id:
                    # Double-check: re-fetch to confirm truly missing
                    protection_creator.bump_clordid_version(trade.id, "sl")
                    sl_result = await protection_creator.create_sl(trade, session)
                    if sl_result.success:
                        L.success(f"[BatchFill] {trade.pair} SL已创建 algoId={sl_result.algo_id}")
                        result["sl"] = "created"
                    else:
                        L.error(f"[BatchFill] {trade.pair} SL创建失败: {sl_result.error}")
                        result["sl"] = "failed"
                else:
                    # DB thinks it exists but REST didn't find it → clear stale ref
                    verified = await _fetch_okx_algo_by_id(trade.sl_algo_id, norm_sym)
                    if verified and verified.get("state") == "live":
                        L.info(f"[BatchFill] {trade.pair} SL REST-verified alive (batch miss)")
                        result["sl"] = "already_exists"
                    else:
                        L.warning(f"[BatchFill] {trade.pair} SL stale ref {trade.sl_algo_id} → 清除")
                        trade.sl_algo_id = None
                        # Try creating
                        protection_creator.bump_clordid_version(trade.id, "sl")
                        sl_result = await protection_creator.create_sl(trade, session)
                        if sl_result.success:
                            result["sl"] = "created"

    # ———— Step 7: TP1 — check if update needed ————
    if tp1_price > 0 and real_contracts > 0:
        # TP1 quantity = 50% of real position
        tp_qty = normalize_order_amount(trade.pair, real_contracts * (1.0 if single_teacher_tp(trade) else 0.30), trade_ex)
        if tp_qty is not None and tp_qty > 0:
            if has_tp:
                existing_tp_price = 0.0
                existing_tp_amount = 0.0
                existing_tp_id = ""
                for o in algo_orders:
                    if (_norm_pair(o.get("instId", "")) == _norm_pair(norm_sym)
                            and _is_algo_tp(o)):
                        existing_tp_price = _safe_float(o.get("triggerPx"))
                        existing_tp_amount = _safe_float(o.get("sz"))
                        existing_tp_id = o.get("algoId", "")
                        break

                price_mismatch = (existing_tp_price > 0 and
                                  abs(existing_tp_price - tp1_price) > max(abs(tp1_price) * 0.005, 0.5))
                amount_mismatch = (existing_tp_amount > 0 and
                                   abs(existing_tp_amount - tp_qty) > max(tp_qty * 0.01, 1.0))

                if price_mismatch or amount_mismatch:
                    L.info(f"[BatchFill] {trade.pair} TP1需要更新: "
                           f"价格 OKX={existing_tp_price}→目标={tp1_price} "
                           f"数量 OKX={existing_tp_amount}→目标={tp_qty}")

                    # Cancel old TP1
                    if existing_tp_id:
                        try:
                            await runtime.cancel_algo_order(existing_tp_id,
                                                            _to_okx_inst_id(_norm_pair(trade.pair)))
                            trade.tp1_algo_id = None
                            L.info(f"[BatchFill] {trade.pair} 旧TP1已取消 algoId={existing_tp_id}")
                        except Exception as e:
                            L.warning(f"[BatchFill] {trade.pair} 取消旧TP1失败: {e}")

                    # Create new TP1
                    protection_creator.bump_clordid_version(trade.id, "tp1")
                    tp_result = await protection_creator.create_tp(trade, session)
                    if tp_result.success:
                        L.success(f"[BatchFill] {trade.pair} TP1已更新 algoId={tp_result.algo_id} "
                                  f"价格={tp1_price} 数量={tp_qty}")
                        result["tp"] = "updated"
                    else:
                        L.error(f"[BatchFill] {trade.pair} TP1更新失败: {tp_result.error}")
                        result["tp"] = "failed"
                else:
                    L.debug(f"[BatchFill] {trade.pair} TP1无需更新 (价格/数量匹配)")
                    result["tp"] = "unchanged"
            else:
                # TP1 missing → create
                if trade.position_state != "partial_tp":
                    protection_creator.bump_clordid_version(trade.id, "tp1")
                    tp_result = await protection_creator.create_tp(trade, session)
                    if tp_result.success:
                        L.success(f"[BatchFill] {trade.pair} TP1已创建 algoId={tp_result.algo_id}")
                        result["tp"] = "created"
                    elif tp_result.algo_id == "skipped_too_small":
                        result["tp"] = "skipped_too_small"
                    else:
                        L.error(f"[BatchFill] {trade.pair} TP1创建失败: {tp_result.error}")
                        result["tp"] = "failed"

    # ———— Step 8: Update signal_meta tracking ————
    meta = dict(trade.signal_meta or {})
    meta["_tp1_setup_amount"] = real_contracts
    trade.signal_meta = meta
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(trade, "signal_meta")

    session.flush()
    L.info(f"[BatchFill] {trade.pair} 分批协调完成: "
           f"contracts={real_contracts} entry_price={real_entry_price} "
           f"SL={result.get('sl')} TP={result.get('tp')}")

    return result


# ============================================================================
# Event-Driven: reconcile_on_ws_reconnect
# ============================================================================

async def reconcile_on_ws_reconnect(session: Session) -> dict:
    """
    Called ONCE after WS reconnection.
    Runs a single reconcile pass to sync any state changes that happened
    while WS was disconnected.
    """
    L.info("[Reconciler] WS reconnected — running single reconciliation pass")
    return await reconcile(session)


# ============================================================================
# Daily Maintenance
# ============================================================================

async def daily_database_maintenance(session: Session) -> None:
    """Daily DB maintenance: VACUUM, ANALYZE, cleanup old data."""
    from sqlalchemy import text
    try:
        session.execute(text("VACUUM"))
        session.execute(text("ANALYZE"))
        L.info("[Maintenance] VACUUM + ANALYZE complete")
        # trades/orders 保留 10 年（收益账本与订单审计，≈ 永久保留）
        cutoff_10y = (datetime.now(timezone.utc) - timedelta(days=3650)).replace(tzinfo=None)
        session.query(Trade).filter(
            Trade.is_open == False,
            Trade.close_date < cutoff_10y,
        ).delete()
        session.query(Order).filter(
            Order.ft_is_open == False,
            Order.order_update_date < cutoff_10y,
        ).delete()
        session.commit()
    except Exception as e:
        L.error(f"[Maintenance] failed: {e}")
