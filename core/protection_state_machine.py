"""
Protection Guards v5 — Minimal concurrency control.

OKX IS THE ONLY SOURCE OF TRUTH.

This module NO LONGER tracks:
  ❌ Per-algo state (NONE/CREATING/WAIT_CONFIRM/ACTIVE/FAILED)
  ❌ has_sl / has_tp / sl_created / tp_created markers
  ❌ _setup_done flag
  ❌ protection_state transitions for SL/TP lifecycle
  ❌ derive_algo_state_from_db / derive_initial_state

Database stores only DESIRED state (target prices).
Whether SL/TP exists on OKX is determined by REST API, never by DB.

This module NOW provides:
  ✅ repair_lock — mutex to prevent concurrent repair of same trade
  ✅ cooldown guard — rate limiting for repair actions
  ✅ retry limit — prevent infinite repair loops
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from enum import Enum

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade

L = logger.bind(module="protection_guards")

# ———— Guard Settings ————
REPAIR_COOLDOWN_SECONDS = 60
MAX_REPAIR_RETRIES = 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ============================================================================
# Repair Guards (concurrency + rate limiting only, NOT state tracking)
# ============================================================================

def is_repair_allowed(trade: Trade) -> bool:
    """
    Check if repair is allowed for this trade.
    Guards: repair_lock, cooldown, retry limit.
    Does NOT check OKX state — that's done by the caller via REST.
    """
    if trade.repair_lock:
        return False

    if trade.last_repair_time is not None:
        elapsed = (_utc_now() - _ensure_utc(trade.last_repair_time)).total_seconds()
        if elapsed < REPAIR_COOLDOWN_SECONDS:
            return False

    if trade.repair_retry >= MAX_REPAIR_RETRIES:
        return False

    return True


def acquire_repair_lock(trade: Trade, session: Session) -> bool:
    """Try to acquire the repair lock. Returns True if acquired."""
    if trade.repair_lock:
        return False
    trade.repair_lock = True
    session.flush()
    return True


def release_repair_lock(trade: Trade, session: Session) -> None:
    """Release the repair lock."""
    trade.repair_lock = False
    session.flush()


def record_repair_attempt(trade: Trade, session: Session) -> int:
    """Record a repair attempt. Returns new retry count."""
    trade.last_repair_time = _utc_now()
    trade.repair_retry += 1
    session.flush()

    if trade.repair_retry >= MAX_REPAIR_RETRIES:
        L.warning(
            f"[Guards] Trade={trade.id} {trade.pair} "
            f"repair retries exhausted ({MAX_REPAIR_RETRIES}), requires manual intervention"
        )

    return trade.repair_retry


def reset_repair_retry(trade: Trade, session: Session) -> None:
    """Reset retry counter after successful repair."""
    trade.repair_retry = 0
    trade.last_repair_time = None
    session.flush()
