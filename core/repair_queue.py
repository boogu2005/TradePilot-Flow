"""
Repair Queue — in-memory priority queue for protection repair tasks.

Deduplicates by (trade_id, task_type) so the same issue isn't queued twice.
Additionally, enforces a per-trade cooldown after pop to prevent
immediate re-push from the Monitor/Coordinator.
If the bot restarts, startup_verify_all_trades rebuilds the queue.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

L = logger.bind(module="repair_queue")

# Minimum seconds before the same (trade_id, task_type) can be re-pushed after pop
REQUEUE_COOLDOWN_SECONDS = 60.0


@dataclass(order=True)
class RepairTask:
    """A single repair task for the Repair Worker."""
    priority: int
    created_at: float = field(compare=False)
    trade_id: int = field(compare=False)
    task_type: str = field(compare=False)  # "create_sl" | "create_tp" | "verify_sl" | "verify_tp" | "cancel_sl" | "cancel_tp" | "verify_and_repair"
    description: str = field(compare=False, default="")


class RepairQueue:
    """
    In-memory priority queue for repair tasks.
    Priority: 1=high, 2=normal, 3=low.
    Deduplicated by (trade_id, task_type).
    Cooldown after pop prevents immediate re-push.
    """

    def __init__(self, max_size: int = 1000):
        import heapq
        self._heap: list[RepairTask] = []
        self._lock = False  # simple asyncio-safe flag
        self._seen: set[tuple[int, str]] = set()  # (trade_id, task_type) dedup
        self._recently_popped: dict[tuple[int, str], float] = {}  # (trade_id, task_type) → pop_time
        self._max_size = max_size

    def push(self, task: RepairTask) -> bool:
        """
        Add a task to the queue. Returns False if duplicate or in cooldown.
        Dedup key: (trade_id, task_type).
        Cooldown: same key cannot be re-pushed within REQUEUE_COOLDOWN_SECONDS of pop.
        """
        key = (task.trade_id, task.task_type)

        # Check dedup: already in queue
        if key in self._seen:
            L.debug(f"[RepairQueue] Duplicate skipped: trade={task.trade_id} type={task.task_type}")
            return False

        # Check cooldown: recently popped
        popped_at = self._recently_popped.get(key)
        if popped_at is not None:
            elapsed = time.time() - popped_at
            if elapsed < REQUEUE_COOLDOWN_SECONDS:
                L.debug(
                    f"[RepairQueue] Cooldown active: trade={task.trade_id} "
                    f"type={task.task_type} elapsed={elapsed:.1f}s < {REQUEUE_COOLDOWN_SECONDS}s"
                )
                return False
            else:
                # Cooldown expired, clean up
                del self._recently_popped[key]

        if len(self._heap) >= self._max_size:
            L.warning(f"[RepairQueue] Queue full ({self._max_size}), dropping: {task}")
            return False

        import heapq
        heapq.heappush(self._heap, task)
        self._seen.add(key)
        L.debug(f"[RepairQueue] Pushed: trade={task.trade_id} type={task.task_type} pri={task.priority}")
        return True

    def pop(self) -> RepairTask | None:
        """Remove and return the highest-priority task (lowest priority number)."""
        import heapq
        if not self._heap:
            return None
        task = heapq.heappop(self._heap)
        key = (task.trade_id, task.task_type)
        self._seen.discard(key)
        # Record pop time for cooldown
        self._recently_popped[key] = time.time()
        # Cleanup old entries
        self._cleanup_cooldowns()
        return task

    def _cleanup_cooldowns(self) -> None:
        """Remove expired cooldown entries."""
        now = time.time()
        stale = [
            k for k, t in self._recently_popped.items()
            if now - t > REQUEUE_COOLDOWN_SECONDS * 2
        ]
        for k in stale:
            del self._recently_popped[k]

    def has_task(self, trade_id: int, task_type: str) -> bool:
        """Check if a specific task is already queued."""
        return (trade_id, task_type) in self._seen

    def remove_trade(self, trade_id: int) -> int:
        """Remove all tasks for a specific trade. Returns count removed."""
        import heapq
        before = len(self._heap)
        self._heap = [t for t in self._heap if t.trade_id != trade_id]
        heapq.heapify(self._heap)
        removed = before - len(self._heap)
        # Clean up _seen entries for this trade
        keys_to_remove = {k for k in self._seen if k[0] == trade_id}
        self._seen -= keys_to_remove
        return removed

    @property
    def size(self) -> int:
        return len(self._heap)

    @property
    def is_empty(self) -> bool:
        return len(self._heap) == 0

    def snapshot(self) -> list[dict]:
        """Return a snapshot of current queue state for debugging."""
        import heapq
        # Make a copy and pop all to get ordered
        temp = list(self._heap)
        result = []
        while temp:
            t = heapq.heappop(temp)
            result.append({
                "trade_id": t.trade_id,
                "task_type": t.task_type,
                "priority": t.priority,
                "description": t.description,
            })
        return result


# Global singleton
repair_queue = RepairQueue()
