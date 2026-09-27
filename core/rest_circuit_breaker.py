"""
Per-Operation REST Circuit Breaker.

Unlike the general circuit_breaker.py (which protects the entire OKX connection),
this provides INDEPENDENT circuit breakers per REST operation type.

Operations: create_order, cancel_order, fetch_order, fetch_positions, fetch_balance,
            fetch_ticker, fetch_algo, modify_order, set_leverage

Each operation has its own failure counter and can open independently.
When "create_order" fails 5 times, only create_order is paused — cancel and fetch continue.

States: CLOSED → OPEN → HALF_OPEN → CLOSED
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from loguru import logger

L = logger.bind(module="rest_cb")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# P2: 订单已不存在/已撤销的错误码与文案。这些不是真正的 API 故障--
# 撤单/查询一个已不在的订单是其目标的自然终态，不应计入熔断器，
# 否则批量清理已成交/已撤销订单时会误开熔断器，阻断后续正常撤单。
BENIGN_ERROR_PATTERNS = ("51400", "51001", "does not exist", "order not found", "already cancelled", "already canceled")


def is_benign_error(error: str) -> bool:
    """Return True if the error represents an already-gone order (not a real API failure)."""
    if not error:
        return False
    low = error.lower()
    return any(p in low for p in BENIGN_ERROR_PATTERNS)


OPERATIONS = [
    "create_order",
    "cancel_order",
    "fetch_order",
    "fetch_positions",
    "fetch_balance",
    "fetch_ticker",
    "fetch_algo",
    "modify_order",
    "set_leverage",
]


@dataclass
class OpBreaker:
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    successes: int = 0
    last_failure_time: float = 0.0
    opened_at: float = 0.0
    half_open_attempts: int = 0


class RestCircuitBreaker:
    """
    Per-operation circuit breaker for REST API calls.

    FAILURE_THRESHOLD = 5 consecutive failures to OPEN
    RECOVERY_TIMEOUT  = 60s before HALF_OPEN
    HALF_OPEN_MAX     = 2 requests in HALF_OPEN
    """

    FAILURE_THRESHOLD = 5
    RECOVERY_TIMEOUT = 60
    HALF_OPEN_MAX = 2

    def __init__(self):
        self._breakers: dict[str, OpBreaker] = {op: OpBreaker() for op in OPERATIONS}

    def is_allowed(self, operation: str) -> bool:
        """
        Check if an operation is allowed.
        CLOSED → allowed
        OPEN → blocked (unless recovery timeout elapsed → HALF_OPEN)
        HALF_OPEN → allowed (limited)
        """
        b = self._breakers.get(operation)
        if b is None:
            return True  # Unknown operations are allowed

        if b.state == BreakerState.CLOSED:
            return True

        if b.state == BreakerState.OPEN:
            if time.time() - b.opened_at >= self.RECOVERY_TIMEOUT:
                b.state = BreakerState.HALF_OPEN
                b.half_open_attempts = 0
                L.info(f"[RestCB] {operation}: OPEN → HALF_OPEN (recovery timeout)")
                return True
            return False

        # HALF_OPEN
        return b.half_open_attempts < self.HALF_OPEN_MAX

    def record_success(self, operation: str) -> None:
        """Record a successful API call."""
        b = self._breakers.get(operation)
        if b is None:
            return

        if b.state == BreakerState.HALF_OPEN:
            b.half_open_attempts += 1
            b.successes += 1
            if b.half_open_attempts >= self.HALF_OPEN_MAX:
                b.state = BreakerState.CLOSED
                b.failures = 0
                L.success(f"[RestCB] {operation}: HALF_OPEN → CLOSED (recovered)")
        elif b.state == BreakerState.CLOSED:
            b.failures = 0  # Reset on success
            b.successes += 1

    def record_failure(self, operation: str, error: str = "") -> None:
        """Record a failed API call."""
        # P2: 订单已不存在（51400/51001 等）不计入熔断器（见 is_benign_error）
        if is_benign_error(error):
            return
        b = self._breakers.get(operation)
        if b is None:
            return

        b.failures += 1
        b.last_failure_time = time.time()

        if b.state == BreakerState.HALF_OPEN:
            b.state = BreakerState.OPEN
            b.opened_at = time.time()
            L.warning(f"[RestCB] {operation}: HALF_OPEN → OPEN (failure: {error[:80]})")
        elif b.state == BreakerState.CLOSED and b.failures >= self.FAILURE_THRESHOLD:
            b.state = BreakerState.OPEN
            b.opened_at = time.time()
            L.warning(
                f"[RestCB] {operation}: CLOSED → OPEN "
                f"({b.failures} failures, recovery in {self.RECOVERY_TIMEOUT}s: {error[:80]})"
            )

    def get_state(self, operation: str) -> str:
        b = self._breakers.get(operation)
        return b.state.value if b else "unknown"

    def get_all_states(self) -> dict[str, str]:
        return {op: self.get_state(op) for op in OPERATIONS}

    def get_stats(self) -> dict:
        return {
            op: {
                "state": b.state.value,
                "failures": b.failures,
                "successes": b.successes,
            }
            for op, b in self._breakers.items()
        }

    def reset(self, operation: str | None = None) -> None:
        """Reset breaker(s). If operation is None, reset all."""
        if operation:
            self._breakers[operation] = OpBreaker()
        else:
            self._breakers = {op: OpBreaker() for op in OPERATIONS}


# Global singleton
rest_circuit_breaker = RestCircuitBreaker()
