"""
Protection Manager v5 — Cancel, Cleanup, Event-Driven Setup.

OKX IS THE ONLY SOURCE OF TRUTH.

Module split (v5):
  - core/protection_creator.py   — SINGLE entry point for all SL/TP creation (REST pre-flight)
  - exit/protection_verify.py    — READ-ONLY REST-based verification
  - exit/protection.py           — Cancel, Cleanup, Event-Driven Setup (this file)

Key Design:
  1. ProtectionCreator.create_sl / create_tp → REST pre-flight → create only if missing
  2. verify_sl / verify_tp → REST-only (NEVER WS)
  3. Cancel is AlgoID-based
  4. Cleanup is event-driven (position closed → cancel all algos)
  5. ensure_protection() called ONCE after entry fill (event-driven, not loop)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade
from core.exchange_runtime import runtime

L = logger.bind(module="protection_v5")


# ———— Re-exports for backward compat ————
from core.protection_creator import (
    protection_creator,
    ProtectionCreator,
    CreateResult,
)

from exit.protection_verify import (
    verify_sl,
    verify_tp,
    verify_all_protection,
    verify_and_push_repair,
    VerifyResult,
)


# Backward-compat module-level wrappers (delegate to ProtectionCreator singleton)
async def create_sl(trade: Trade, session: Session) -> CreateResult:
    return await protection_creator.create_sl(trade, session)


async def create_tp(trade: Trade, session: Session) -> CreateResult:
    return await protection_creator.create_tp(trade, session)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# CANCEL — AlgoID-based
# ============================================================================

async def cancel_sl(trade: Trade, session: Session) -> bool:
    """
    Cancel SL order by its AlgoID.
    After cancel: clear reference. Returns True if cancelled or already gone.
    """
    if not trade.sl_algo_id:
        return True

    try:
        await runtime.cancel_algo_order(trade.sl_algo_id, trade.pair)
    except Exception as e:
        err_str = str(e)
        if "51400" in err_str or "order does not exist" in err_str.lower():
            pass
        else:
            L.warning(f"[CancelSL] Trade={trade.id} cancel failed: {err_str[:120]}")
            return False

    # Update DB order records
    for o in (trade.orders or []):
        if o.order_id == trade.sl_algo_id and o.ft_is_open:
            o.ft_is_open = False
            o.status = "canceled"

    L.info(f"[CancelSL] Trade={trade.id} cancelled, algoId={trade.sl_algo_id}")
    trade.sl_algo_id = None
    session.flush()
    return True


async def cancel_tp(trade: Trade, session: Session, tp_index: int = 1) -> bool:
    """
    Cancel TP order by its AlgoID.
    Returns True if cancelled or already gone.
    """
    tp_algo_id = trade.tp1_algo_id if tp_index == 1 else (
        trade.tp2_algo_id if tp_index == 2 else trade.tp3_algo_id
    )

    if not tp_algo_id:
        return True

    try:
        await runtime.cancel_algo_order(tp_algo_id, trade.pair)
    except Exception as e:
        err_str = str(e)
        if "51400" in err_str or "order does not exist" in err_str.lower():
            pass
        else:
            L.warning(f"[CancelTP] Trade={trade.id} cancel failed: {err_str[:120]}")
            return False

    for o in (trade.orders or []):
        if o.order_id == tp_algo_id and o.ft_is_open:
            o.ft_is_open = False
            o.status = "canceled"

    L.info(f"[CancelTP] Trade={trade.id} TP{tp_index} cancelled, algoId={tp_algo_id}")

    if tp_index == 1:
        trade.tp1_algo_id = None
    elif tp_index == 2:
        trade.tp2_algo_id = None
    elif tp_index == 3:
        trade.tp3_algo_id = None
    session.flush()
    return True


# ============================================================================
# STARTUP VERIFY — v5: REST-only, single pass, no repair queue
# ============================================================================

async def startup_verify_all_trades(session: Session) -> dict:
    """
    Startup verification of ALL active trades using OKX REST API.
    Pushes missing protection to Repair Queue for one-time fix.

    Exchange is the source of truth.
    """
    from core.repair_queue import repair_queue, RepairTask

    active_trades = Trade.get_active_trades(session)
    if not active_trades:
        L.info("[StartupVerify] No active trades")
        return {"verified": 0, "needs_repair": 0, "failed": 0, "details": []}

    L.info(f"[StartupVerify] Verifying {len(active_trades)} active trades via OKX REST...")

    verified = 0
    needs_repair = 0
    failed = 0
    details = []

    for trade in active_trades:
        try:
            # partial_tp 状态：仓位已进入移动止盈止损，由 TrailingChecker 管理退出
            # 只验证/修复 SL（最终保障），不验证/修复 TP
            # tp1_filled 状态：TP1 已完成，但 TP2 可能仍需补挂，需要验证 TP
            is_partial_tp = getattr(trade, 'position_state', None) == "partial_tp"

            sl_result = await verify_sl(trade, session)
            sl_ok = sl_result.exists

            from core.protection_targets import tp_price_for
            tp_index = 2 if trade.position_state == "tp1_filled" else 1
            if is_partial_tp or tp_price_for(trade, tp_index) is None:
                # partial_tp: 只关心 SL，TP 不需要（所有 TP 已完成）
                tp_ok = True  # 标记为 OK，跳过 TP 修复
            else:
                tp_result = await verify_tp(trade, session, tp_index=tp_index)
                tp_ok = tp_result.exists

            if sl_ok and tp_ok:
                verified += 1
                details.append({"pair": trade.pair, "state": "verified"})
            else:
                needs_repair += 1
                details.append({
                    "pair": trade.pair,
                    "state": "needs_repair",
                    "sl": "ok" if sl_ok else "missing",
                    "tp": "ok" if tp_ok else ("skipped_partial_tp" if is_partial_tp else "missing"),
                })

                if not sl_ok:
                    repair_queue.push(RepairTask(
                        priority=1, created_at=_utc_now().timestamp(),
                        trade_id=trade.id, task_type="create_sl",
                        description=f"Startup: SL not found on OKX for {trade.pair}"
                    ))
                if not tp_ok and not is_partial_tp:
                    repair_queue.push(RepairTask(
                        priority=1, created_at=_utc_now().timestamp(),
                        trade_id=trade.id, task_type="create_tp",
                        description=f"Startup: TP not found on OKX for {trade.pair}"
                    ))

        except Exception as e:
            failed += 1
            details.append({"pair": trade.pair, "state": "error", "error": str(e)[:120]})
            L.error(f"[StartupVerify] {trade.pair} error: {e}")

    session.commit()

    L.info(f"[StartupVerify] Complete: verified={verified} needs_repair={needs_repair} failed={failed}")
    if needs_repair > 0:
        L.info(f"[StartupVerify] {needs_repair} trades need repair, pushed to queue (size={repair_queue.size})")

    return {"verified": verified, "needs_repair": needs_repair,
            "failed": failed, "details": details}


def _ccxt_to_okx_inst_id(symbol: str) -> str:
    """CCXT pair → OKX instId: XAU/USDT:USDT → XAU-USDT-SWAP"""
    s = symbol.upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT-SWAP"
    return s


# ============================================================================
# CLEANUP
# ============================================================================

_ALGO_CLEANUP_LOCK: set[str] = set()


async def cleanup_trade_orders(
    trade_or_symbol: Trade | str,
    session: Session | None = None,
    exchange: str = "okx",
) -> bool:
    """
    Cancel all residual algo orders when position is closed.
    Event-driven, idempotent.

    BugFix v6: Pass Trade object through to _do_cleanup so we can:
      1. Convert CCXT pair to OKX instId for cancel API
      2. Clear trade.sl_algo_id / tp1_algo_id after cancel
      3. Fallback: cancel by stored algo_id even if REST fetch fails
    """
    trade = trade_or_symbol if isinstance(trade_or_symbol, Trade) else None
    symbol = trade.pair if trade else trade_or_symbol
    trade_id = trade.id if trade else None

    if symbol in _ALGO_CLEANUP_LOCK:
        return True
    _ALGO_CLEANUP_LOCK.add(symbol)
    try:
        return await _do_cleanup(symbol, trade, trade_id, session, exchange)
    finally:
        _ALGO_CLEANUP_LOCK.discard(symbol)


async def _do_cleanup(symbol: str, trade: Trade | None, trade_id: int | None,
                       session: Session | None, exchange: str) -> bool:
    """
    Cancel residual algo orders + clear DB references.

    Strategy (dual path):
      Path A (REST): fetch_pending_algo_orders → cancel each by algoId
      Path B (Fallback): if REST fails or returns empty, cancel by stored algo_ids
    """
    from exchange_engine.exchange import fetch_pending_algo_orders
    from database.models import Order as OrderModel

    okx_inst_id = _ccxt_to_okx_inst_id(symbol)
    cancelled_ids: set[str] = set()

    # —— Path A: REST fetch + cancel ——
    try:
        pending = await fetch_pending_algo_orders(symbol, exchange="okx")
    except Exception as e:
        L.warning(f"[Cleanup] REST fetch failed for {symbol}: {e}")
        pending = None  # signal fallback needed

    if pending:
        L.info(f"[Cleanup] {symbol} {len(pending)} residual algo orders, cancelling...")
        for p in pending:
            aid = p.get("algoId", "")
            if aid:
                try:
                    await runtime.cancel_algo_order(aid, okx_inst_id)
                    cancelled_ids.add(aid)
                except Exception as e:
                    L.warning(f"[Cleanup] {symbol} cancel algoId={aid} failed: {e}")

    # —— Path B: Fallback — cancel by stored algo_ids (REST-independent) ——
    if pending is None or not pending:
        if trade:
            for algo_id_attr in ("sl_algo_id", "tp1_algo_id", "tp2_algo_id", "tp3_algo_id"):
                stored = getattr(trade, algo_id_attr, None)
                if stored and stored not in cancelled_ids:
                    try:
                        await runtime.cancel_algo_order(stored, okx_inst_id)
                        cancelled_ids.add(stored)
                        L.info(f"[Cleanup] {symbol} fallback cancel {algo_id_attr}={stored}")
                    except Exception as e:
                        err_s = str(e)
                        if "51400" in err_s or "51001" in err_s:
                            cancelled_ids.add(stored)  # already gone = success
                        else:
                            L.warning(f"[Cleanup] {symbol} fallback cancel {algo_id_attr}={stored} failed: {e}")

    # —— Sync DB: Order records + Trade algo_id references ——
    if session and trade_id is not None:
        # Clear Order.ft_is_open
        db_orders = session.query(OrderModel).filter(
            OrderModel.ft_trade_id == trade_id,
            OrderModel.ft_is_open.is_(True),
        ).all()
        for o in db_orders:
            if o.ft_order_role in ("stoploss", "tp", "trailing_stop"):
                o.ft_is_open = False
                o.status = "canceled"

        # BugFix: Clear Trade.algo_id references (were never cleaned before)
        if trade:
            trade.sl_algo_id = None
            trade.tp1_algo_id = None
            trade.tp2_algo_id = None
            trade.tp3_algo_id = None

        if db_orders or (trade and trade.sl_algo_id is None):
            session.commit()

    return len(cancelled_ids) > 0 or pending == []  # success if cancelled or confirmed empty


async def ensure_no_algo_orders(symbol: str, exchange: str = "okx") -> bool:
    """Pre-entry check: cancel any residual algo orders before opening position."""
    from exchange_engine.exchange import fetch_pending_algo_orders
    okx_inst_id = _ccxt_to_okx_inst_id(symbol)
    try:
        pending = await fetch_pending_algo_orders(symbol, exchange="okx")
    except Exception:
        return True  # API failure → don't block entry

    if not pending:
        return True

    L.warning(f"[Cleanup] {symbol} pre-entry: {len(pending)} residual orders, cleaning...")
    for p in pending:
        aid = p.get("algoId", "")
        if aid:
            try:
                await runtime.cancel_algo_order(aid, okx_inst_id)
            except Exception:
                pass

    return True
