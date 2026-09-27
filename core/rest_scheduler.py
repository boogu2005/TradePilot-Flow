"""
REST Scheduler — priority queue for all REST API calls.

Features:
  - Priority queue: trading ops (pri=1) > fetch ops (pri=3)
  - Rate limiter: per-operation token bucket
  - Circuit breaker: per-operation (delegates to RestCircuitBreaker)
  - Queue size tracking for health monitor

All REST calls go through this scheduler. No module calls REST directly.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from loguru import logger

from .rest_circuit_breaker import rest_circuit_breaker
from .exchange_health import health_monitor

L = logger.bind(module="rest_scheduler")

# Rate limits (per operation, per second)
DEFAULT_RATE_LIMITS: dict[str, float] = {
    "create_order": 5.0,
    "cancel_order": 10.0,
    "fetch_order": 10.0,
    "fetch_positions": 2.0,
    "fetch_balance": 2.0,
    "fetch_ticker": 5.0,
    "fetch_algo": 5.0,
    "modify_order": 2.0,
    "set_leverage": 2.0,
}


@dataclass(order=True)
class RestRequest:
    priority: int
    created_at: float = field(compare=False)
    operation: str = field(compare=False)
    coro_factory: Callable[[], Awaitable[Any]] = field(compare=False)
    future: asyncio.Future = field(compare=False)
    label: str = field(compare=False)


class RestScheduler:
    """
    Priority-queue-based REST request scheduler.

    Trading operations (create/cancel/modify) get priority=1.
    Query operations (fetch) get priority=3.
    Emergency operations get priority=0 (jump the queue).
    """

    def __init__(self, max_queue_size: int = 200):
        import heapq
        self._heap: list[RestRequest] = []
        self._max_size = max_queue_size
        self._rate_trackers: dict[str, list[float]] = {}  # operation → [timestamps]
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._process_loop(), name="rest_scheduler")
        L.info("[RestScheduler] Started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        L.info("[RestScheduler] Stopped")

    async def schedule(
        self,
        operation: str,
        coro_factory: Callable[[], Awaitable[Any]],
        priority: int = 2,
        label: str = "",
    ) -> Any:
        """
        Schedule a REST call. Returns the result or raises the exception.

        Args:
            operation: e.g. "create_order", "fetch_balance"
            coro_factory: async callable that performs the REST call
            priority: 0=emergency, 1=trading, 2=normal, 3=query
            label: human-readable label for logging

        Returns: the result of coro_factory

        Raises: RuntimeError if circuit breaker is open
        """
        # Check circuit breaker
        if not rest_circuit_breaker.is_allowed(operation):
            raise RuntimeError(
                f"Circuit breaker OPEN for {operation}. "
                f"State: {rest_circuit_breaker.get_state(operation)}"
            )

        # Check rate limit
        await self._check_rate_limit(operation)

        import heapq
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        req = RestRequest(
            priority=priority,
            created_at=time.time(),
            operation=operation,
            coro_factory=coro_factory,
            future=future,
            label=label,
        )

        heapq.heappush(self._heap, req)
        health_monitor.rest_queue_size(len(self._heap))

        # If queue is full, drop lowest priority
        if len(self._heap) > self._max_size:
            self._heap.sort(key=lambda r: (r.priority, r.created_at))
            dropped = self._heap.pop()
            if not dropped.future.done():
                dropped.future.set_exception(
                    RuntimeError(f"REST queue full, request dropped: {dropped.operation}")
                )
            heapq.heapify(self._heap)
            L.warning(f"[RestScheduler] Queue full, dropped: {dropped.operation}")

        return await future

    async def _process_loop(self) -> None:
        """Process queued REST requests."""
        import heapq
        max_concurrent = 3  # Max concurrent REST calls

        while self._running:
            # Process up to max_concurrent at a time
            pending: list[RestRequest] = []
            for _ in range(min(max_concurrent, len(self._heap))):
                if self._heap:
                    pending.append(heapq.heappop(self._heap))

            if not pending:
                await asyncio.sleep(0.1)
                continue

            health_monitor.rest_queue_size(len(self._heap))

            # Execute in parallel
            async def execute(req: RestRequest) -> None:
                try:
                    start = time.perf_counter()
                    result = await req.coro_factory()
                    elapsed = (time.perf_counter() - start) * 1000

                    rest_circuit_breaker.record_success(req.operation)
                    health_monitor.rest_request_completed(elapsed, True)

                    if not req.future.done():
                        req.future.set_result(result)
                except Exception as e:
                    elapsed = (time.perf_counter() - time.time()) * 1000
                    rest_circuit_breaker.record_failure(req.operation, str(e))
                    health_monitor.rest_request_completed(elapsed, False)

                    if not req.future.done():
                        req.future.set_exception(e)

            tasks = [asyncio.create_task(execute(r)) for r in pending]
            await asyncio.gather(*tasks, return_exceptions=True)

            health_monitor.rest_queue_size(len(self._heap))

    async def _check_rate_limit(self, operation: str) -> None:
        """Wait if rate limit would be exceeded."""
        max_rate = DEFAULT_RATE_LIMITS.get(operation, 5.0)
        now = time.time()
        cutoff = now - 1.0

        if operation not in self._rate_trackers:
            self._rate_trackers[operation] = []

        # Prune old entries
        self._rate_trackers[operation] = [
            t for t in self._rate_trackers[operation] if t > cutoff
        ]

        if len(self._rate_trackers[operation]) >= max_rate:
            # Need to wait
            oldest = min(self._rate_trackers[operation])
            wait = oldest + 1.0 - now
            if wait > 0:
                L.debug(f"[RestScheduler] Rate limit {operation}: waiting {wait:.1f}s")
                await asyncio.sleep(wait)

        self._rate_trackers[operation].append(time.time())

    @property
    def queue_size(self) -> int:
        return len(self._heap)

    def stats(self) -> dict:
        return {
            "queue_size": len(self._heap),
            "circuit_breakers": rest_circuit_breaker.get_all_states(),
            "rate_limits": {
                op: len(timestamps)
                for op, timestamps in self._rate_trackers.items()
            },
        }


# Global singleton
rest_scheduler = RestScheduler()
