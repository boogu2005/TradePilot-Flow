"""
Retry Scheduler — exponential backoff for repair retries.

After retry N fails, schedule retry N+1 with:
  retry 1 → 30s delay
  retry 2 → 60s delay
  retry 3 → 120s delay
  retry 4+ → MANUAL_REQUIRED (give up)

This replaces the old "retry 10 times with no backoff" pattern.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade
from .protection_state_machine import (
    MAX_REPAIR_RETRIES,
)
from .repair_queue import RepairTask, repair_queue

L = logger.bind(module="retry_scheduler")


RETRY_BACKOFF: dict[int, int] = {
    1: 30,   # seconds
    2: 60,   # seconds
    3: 120,  # seconds
}


def schedule_retry(trade: Trade, session: Session, task_type: str = "verify_and_repair") -> int:
    """
    Schedule a repair retry with exponential backoff.

    Returns: new retry count, or -1 if retries exhausted.
    """
    retry_n = trade.repair_retry + 1

    if retry_n > MAX_REPAIR_RETRIES:
        L.warning(
            f"[RetryScheduler] Trade={trade.id} {trade.pair} "
            f"retries exhausted ({MAX_REPAIR_RETRIES}), requires manual intervention"
        )
        repair_queue.remove_trade(trade.id)
        return -1

    delay = RETRY_BACKOFF.get(retry_n, 120)
    trade.repair_retry = retry_n
    trade.verify_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
    session.flush()

    L.info(
        f"[RetryScheduler] Trade={trade.id} {trade.pair} "
        f"retry {retry_n}/{MAX_REPAIR_RETRIES} scheduled in {delay}s"
    )

    return retry_n


async def schedule_delayed_repair(
    trade_id: int,
    task_type: str,
    delay_seconds: int,
    description: str = "",
    priority: int = 2,
) -> None:
    """
    Push a repair task to the queue after a delay.
    Used for deferred retries with backoff.
    """
    await asyncio.sleep(delay_seconds)

    task = RepairTask(
        priority=priority,
        created_at=datetime.now(timezone.utc).timestamp(),
        trade_id=trade_id,
        task_type=task_type,
        description=description,
    )
    pushed = repair_queue.push(task)
    if pushed:
        L.debug(f"[RetryScheduler] Trade={trade_id} delayed repair pushed after {delay_seconds}s")
    else:
        L.debug(f"[RetryScheduler] Trade={trade_id} duplicate repair skipped")
