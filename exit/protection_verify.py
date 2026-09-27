"""
Protection Verify v5 — REST-only verification of SL/TP on OKX.

OKX REST API is the ONLY source of truth for "does SL exist? does TP exist?".

NEVER uses:
  ❌ WS AlgoOrderTracker (may not have SWAP algo orders)
  ❌ Snapshot
  ❌ Database state fields

ALWAYS uses:
  ✅ fetch_algo_order_by_id(algo_id) — REST, single algo lookup
  ✅ fetch_pending_algo_orders(symbol) — REST, all algo orders for symbol
  ✅ privateGetTradeOrderAlgo — correct OKX endpoint for specific algo query

Read-only. Never creates, cancels, or modifies orders.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade

L = logger.bind(module="protection_verify")


# ———— Result Types ————

@dataclass
class VerifyResult:
    """Result of verifying a protection order on OKX REST."""
    exists: bool = False
    algo_id: str = ""
    algo_type: str = ""       # "sl" | "tp1"
    exchange_state: str = ""  # live / filled / cancelled / not_found
    trigger_price: float = 0.0
    amount: float = 0.0
    error: str = ""


def _norm_symbol(s: str) -> str:
    """Normalize to OKX instId: BTC-USDT-SWAP"""
    s = (s or "").upper().replace("/", "").replace(":USDT", "")
    if not s.endswith("USDT"):
        return s
    return f"{s[:-4]}-USDT-SWAP"


# ============================================================================
# REST-based Verification (NEVER WS)
# ============================================================================

async def _rest_get_algo_by_id(algo_id: str, symbol: str) -> dict | None:
    """
    Get a single algo order by algoId from OKX REST.
    Uses the CORRECT endpoint: privateGetTradeOrderAlgo (singular).
    """
    from exchange_engine.exchange import fetch_algo_order_by_id
    try:
        return await fetch_algo_order_by_id(algo_id, symbol, exchange="okx")
    except Exception as e:
        L.warning(f"[Verify] REST fetch_algo_by_id({algo_id[:16]}) failed: {e}")
        return None


async def _rest_get_pending_algos(symbol: str) -> list[dict]:
    """Get all pending algo orders from OKX REST, filtered by symbol."""
    from exchange_engine.exchange import fetch_pending_algo_orders
    try:
        short = symbol.upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")
        return await fetch_pending_algo_orders(short, exchange="okx")
    except Exception as e:
        L.warning(f"[Verify] REST fetch_pending_algos({symbol}) failed: {e}")
        return []


# ============================================================================
# verify_sl — REST-only
# ============================================================================

async def verify_sl(trade: Trade, session: Session = None) -> VerifyResult:
    """
    Verify if SL exists on OKX using REST API. READ-ONLY.

    1. If trade has sl_algo_id → REST fetch by algoId (CORRECT endpoint)
    2. If no algo_id or REST miss → REST fetch all pending algos for symbol
    3. Report what OKX actually has
    """
    inst_id = _norm_symbol(trade.pair)

    # Path 1: Query by known algo_id (fast, single REST call)
    if trade.sl_algo_id:
        algo = await _rest_get_algo_by_id(trade.sl_algo_id, inst_id)
        if algo is not None:
            state = algo.get("state", "unknown")
            ord_type = (algo.get("ordType") or "").lower()
            trigger = float(algo.get("triggerPx", 0))
            amt = float(algo.get("sz", 0))

            if state == "live" and ord_type in ("stoploss", "stop", "conditional", "move_order_stop"):
                return VerifyResult(
                    exists=True, algo_id=trade.sl_algo_id, algo_type="sl",
                    exchange_state="live", trigger_price=trigger, amount=amt,
                )
            elif state != "live":
                return VerifyResult(
                    exists=False, algo_id=trade.sl_algo_id, algo_type="sl",
                    exchange_state=state, error=f"SL not live: state={state}",
                )

    # Path 2: Query all pending algos for symbol
    pending = await _rest_get_pending_algos(trade.pair)
    for raw in pending:
        ord_type = (raw.get("ordType") or "").lower()
        state = raw.get("state", "")
        if ord_type in ("stoploss", "stop", "conditional", "move_order_stop") and state == "live":
            algo_id = raw.get("algoId", "")
            trigger = float(raw.get("triggerPx", 0))
            amt = float(raw.get("sz", 0))
            # Update reference if we found one but didn't have the ID
            if algo_id and algo_id != trade.sl_algo_id:
                trade.sl_algo_id = algo_id
                if session:
                    session.flush()
            return VerifyResult(
                exists=True, algo_id=algo_id, algo_type="sl",
                exchange_state="live", trigger_price=trigger, amount=amt,
            )

    # SL not found on OKX
    return VerifyResult(exists=False, algo_type="sl", exchange_state="not_found",
                       error="No live SL found on OKX")


# ============================================================================
# verify_tp — REST-only
# ============================================================================

async def verify_tp(trade: Trade, session: Session = None,
                    tp_index: int = 1) -> VerifyResult:
    """
    Verify if TP exists on OKX using REST API. READ-ONLY.

    Same pattern as verify_sl.
    """
    inst_id = _norm_symbol(trade.pair)
    from core.protection_targets import tp_price_for
    target_price = tp_price_for(trade, tp_index)
    if target_price is None:
        return VerifyResult(exists=True, algo_type=f"tp{tp_index}", exchange_state="not_required")
    tp_algo_id = trade.tp1_algo_id if tp_index == 1 else (
        trade.tp2_algo_id if tp_index == 2 else trade.tp3_algo_id
    )

    # Path 1: Query by known algo_id
    if tp_algo_id:
        algo = await _rest_get_algo_by_id(tp_algo_id, inst_id)
        if algo is not None:
            state = algo.get("state", "unknown")
            ord_type = (algo.get("ordType") or "").lower()
            trigger = float(algo.get("triggerPx", 0))
            amt = float(algo.get("sz", 0))

            price_matches = trigger > 0 and abs(trigger - target_price) / target_price < 0.001
            if state == "live" and ord_type == "takeprofit" and price_matches:
                return VerifyResult(
                    exists=True, algo_id=tp_algo_id, algo_type=f"tp{tp_index}",
                    exchange_state="live", trigger_price=trigger, amount=amt,
                )
            elif state != "live":
                return VerifyResult(
                    exists=False, algo_id=tp_algo_id, algo_type=f"tp{tp_index}",
                    exchange_state=state, error=f"TP not live: state={state}",
                )

    # Path 2: Query all pending algos
    pending = await _rest_get_pending_algos(trade.pair)
    for raw in pending:
        ord_type = (raw.get("ordType") or "").lower()
        state = raw.get("state", "")
        if ord_type == "takeprofit" and state == "live":
            algo_id = raw.get("algoId", "")
            trigger = float(raw.get("triggerPx", 0))
            amt = float(raw.get("sz", 0))
            if trigger <= 0 or abs(trigger - target_price) / target_price >= 0.001:
                continue
            field = "tp1_algo_id" if tp_index == 1 else "tp2_algo_id"
            if algo_id and algo_id != getattr(trade, field):
                setattr(trade, field, algo_id)
                if session:
                    session.flush()
            return VerifyResult(
                exists=True, algo_id=algo_id, algo_type=f"tp{tp_index}",
                exchange_state="live", trigger_price=trigger, amount=amt,
            )

    return VerifyResult(exists=False, algo_type=f"tp{tp_index}", exchange_state="not_found",
                       error="No live TP found on OKX")


# ============================================================================
# verify_all_protection — REST-only convenience
# ============================================================================

async def verify_all_protection(trade: Trade, session: Session = None) -> dict:
    """Verify both SL and TP using REST API. Returns {"sl": VerifyResult, "tp1": VerifyResult}."""
    sl_result = await verify_sl(trade, session)
    tp_index = 2 if trade.position_state == "tp1_filled" else 1
    tp_result = await verify_tp(trade, session, tp_index=tp_index)
    return {"sl": sl_result, f"tp{tp_index}": tp_result}


# ============================================================================
# verify_and_push_repair — kept for backward compat, simplified
# ============================================================================

async def verify_and_push_repair(trade: Trade, session: Session) -> int:
    """
    Verify protection and push to Repair Queue if needed.
    Kept for backward compatibility — new code should use Reconciler directly.
    """
    from core.repair_queue import repair_queue, RepairTask
    from datetime import datetime, timezone

    results = await verify_all_protection(trade, session)
    pushed = 0

    for algo_type, result in results.items():
        if not result.exists:
            task_type = "create_tp" if algo_type.startswith("tp") else f"create_{algo_type}"
            repair_queue.push(RepairTask(
                priority=2,
                created_at=datetime.now(timezone.utc).timestamp(),
                trade_id=trade.id,
                task_type=task_type,
                description=f"Coordinator: {algo_type} not found on OKX, needs creation",
            ))
            pushed += 1
            L.info(f"[Coordinator] Trade={trade.id} {algo_type} not found → pushed to Repair Queue")

    return pushed
