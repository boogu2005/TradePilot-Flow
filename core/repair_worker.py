"""
Repair Worker v5 — Background task that processes the Repair Queue.

Key design changes (v5):
  - Processes only create_sl / create_tp tasks (verify_and_repair removed)
  - Each task goes through ProtectionCreator (which does REST pre-flight)
  - Circuit breaker protection
  - Repair lock for concurrency
  - No more verify→create→verify chains

Monitor/Coordinator push tasks → Repair Worker processes them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from database.db import get_session
from database.models import Trade
from .repair_queue import repair_queue, RepairTask
from .protection_state_machine import (
    is_repair_allowed,
    acquire_repair_lock,
    release_repair_lock,
    record_repair_attempt,
    reset_repair_retry,
)
from .circuit_breaker import circuit_breaker
from .protection_state_machine import MAX_REPAIR_RETRIES

L = logger.bind(module="repair_worker")

MAX_ITEMS_PER_CYCLE = 3
POLL_INTERVAL = 1.0


async def run_repair_worker(shutdown_event: asyncio.Event) -> None:
    """
    Main repair worker loop. Runs as a background asyncio task.
    Polls the repair queue, processes tasks with circuit breaker and rate limiting.
    """
    from .exchange_runtime import runtime

    L.info("[RepairWorker] Started (v5)")

    while not shutdown_event.is_set():
        try:
            if not runtime.is_ready:
                await asyncio.sleep(2)
                continue

            if circuit_breaker.is_open("okx"):
                L.warning("[RepairWorker] Circuit breaker open, skipping")
                await asyncio.sleep(5)
                continue

            processed = 0
            while processed < MAX_ITEMS_PER_CYCLE and not repair_queue.is_empty:
                task = repair_queue.pop()
                if task is None:
                    break

                try:
                    await _process_task(task)
                except Exception as e:
                    L.error(f"[RepairWorker] Task error: {e}")

                processed += 1

            await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception as e:
            L.error(f"[RepairWorker] Unexpected error: {e}")
            await asyncio.sleep(5)

    L.info("[RepairWorker] Stopped")


async def _process_task(task: RepairTask) -> None:
    """Process a single repair task with all guards."""
    session = None
    try:
        if task.trade_id is None:
            L.warning(f"[RepairWorker] Task has NULL trade_id: type={task.task_type}, skipping")
            return

        session = get_session()
        trade = session.get(Trade, task.trade_id)

        if trade is None:
            L.warning(f"[RepairWorker] Trade={task.trade_id} not found, skipping")
            return

        trade_ex = trade.exchange or "okx"

        # Guard: circuit breaker
        if circuit_breaker.is_open(trade_ex):
            task.priority = 3
            repair_queue.push(task)
            return

        # Guard: repair_allowed (cooldown, retry limit, lock)
        if not is_repair_allowed(trade):
            return

        # Acquire lock
        if not acquire_repair_lock(trade, session):
            return

        try:
            if task.task_type == "create_sl":
                await _do_create_sl(trade, session)
            elif task.task_type == "create_tp":
                await _do_create_tp(trade, session)
            elif task.task_type == "cancel_sl":
                await _do_cancel_sl(trade, session)
            elif task.task_type == "cancel_tp":
                await _do_cancel_tp(trade, session)
            elif task.task_type in ("verify_sl", "verify_tp", "verify_and_repair"):
                # v5: verify tasks are no-ops — verification is now done inline
                # in the Reconciler via REST. These tasks should no longer be pushed.
                L.debug(f"[RepairWorker] Legacy task type '{task.task_type}' — skipping (v5)")
            else:
                L.warning(f"[RepairWorker] Unknown task type: {task.task_type}")
        finally:
            release_repair_lock(trade, session)

    except Exception as e:
        L.error(f"[RepairWorker] Error processing task {task}: {e}")
        if session:
            try:
                session.rollback()
            except Exception:
                pass
    finally:
        if session:
            try:
                session.close()
            except Exception:
                pass


async def _do_create_sl(trade: Trade, session: Session) -> None:
    """Create SL via ProtectionCreator (REST pre-flight included)."""
    from core.protection_creator import protection_creator

    result = await protection_creator.create_sl(trade, session)
    if result.success:
        reset_repair_retry(trade, session)
        L.success(f"[RepairWorker] Trade={trade.id} SL created: {result.algo_id}")
    else:
        retries = record_repair_attempt(trade, session)
        L.warning(f"[RepairWorker] Trade={trade.id} SL creation failed: {result.error}")
        if retries >= MAX_REPAIR_RETRIES:
            from diagnostics.service import report_protection_repair_exhausted
            report_protection_repair_exhausted(trade, "create_sl", result.error or "unknown")

    session.commit()


async def _do_create_tp(trade: Trade, session: Session) -> None:
    """Create TP via ProtectionCreator (REST pre-flight included)."""
    from core.protection_creator import protection_creator

    tp_index = 2 if trade.position_state == "tp1_filled" else 1
    result = await protection_creator.create_tp(trade, session, tp_index=tp_index)
    if result.success:
        reset_repair_retry(trade, session)
        L.success(f"[RepairWorker] Trade={trade.id} TP created: {result.algo_id}")
    else:
        retries = record_repair_attempt(trade, session)
        L.warning(f"[RepairWorker] Trade={trade.id} TP creation failed: {result.error}")
        if retries >= MAX_REPAIR_RETRIES:
            from diagnostics.service import report_protection_repair_exhausted
            report_protection_repair_exhausted(trade, "create_tp", result.error or "unknown")

    session.commit()


async def _do_cancel_sl(trade: Trade, session: Session) -> None:
    """Cancel SL."""
    from exit.protection import cancel_sl
    ok = await cancel_sl(trade, session)
    if ok:
        reset_repair_retry(trade, session)
    else:
        retries = record_repair_attempt(trade, session)
        if retries >= MAX_REPAIR_RETRIES:
            from diagnostics.service import report_protection_repair_exhausted
            report_protection_repair_exhausted(trade, "cancel_sl", "cancel returned false")
    session.commit()


async def _do_cancel_tp(trade: Trade, session: Session) -> None:
    """Cancel TP."""
    from exit.protection import cancel_tp
    ok = await cancel_tp(trade, session)
    if ok:
        reset_repair_retry(trade, session)
    else:
        retries = record_repair_attempt(trade, session)
        if retries >= MAX_REPAIR_RETRIES:
            from diagnostics.service import report_protection_repair_exhausted
            report_protection_repair_exhausted(trade, "cancel_tp", "cancel returned false")
    session.commit()
