"""
Circuit Breaker — prevents API storms during exchange outages.

Monitors OKX API error rate. When consecutive failures exceed threshold:
  CLOSED → OPEN (all API calls blocked)
After recovery timeout:
  OPEN → HALF_OPEN (limited calls allowed)
If calls succeed in HALF_OPEN:
  HALF_OPEN → CLOSED
If calls fail in HALF_OPEN:
  HALF_OPEN → OPEN

Prevents the "infinite create" problem when OKX returns errors.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from loguru import logger

L = logger.bind(module="circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal operation
    OPEN = "open"            # Blocked — too many failures
    HALF_OPEN = "half_open"  # Testing if recovery succeeded


# Error patterns that count as failures
FAILURE_PATTERNS = {
    "51299",     # OKX: frequently traded contract, rate limit
    "500",       # Internal server error
    "502",       # Bad gateway
    "503",       # Service unavailable
    "504",       # Gateway timeout
    "429",       # Rate limit exceeded
    "timeout",
    "network",
    "RateLimitExceeded",
    "ExchangeNotAvailable",
    "DDoSProtection",
    "RequestTimeout",
    "ConnectionError",
    "NetworkError",
}


@dataclass
class BreakerState:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    opened_at: float = 0.0
    half_open_requests: int = 0


class CircuitBreaker:
    """
    Per-exchange circuit breaker.

    FAILURE_THRESHOLD: consecutive failures to open circuit
    RECOVERY_TIMEOUT: seconds before attempting half-open
    HALF_OPEN_MAX: max requests allowed in half-open state
    """

    FAILURE_THRESHOLD = 5
    RECOVERY_TIMEOUT = 120  # 2 minutes
    HALF_OPEN_MAX = 2

    def __init__(self):
        self._breakers: dict[str, BreakerState] = {}

    def _get(self, exchange: str) -> BreakerState:
        if exchange not in self._breakers:
            self._breakers[exchange] = BreakerState()
        return self._breakers[exchange]

    def record_success(self, exchange: str = "okx") -> None:
        """Record a successful API call."""
        b = self._get(exchange)
        b.last_success_time = time.time()

        if b.state == CircuitState.HALF_OPEN:
            b.half_open_requests += 1
            if b.half_open_requests >= self.HALF_OPEN_MAX:
                self._close(exchange)
        elif b.state == CircuitState.CLOSED:
            # Reset failure count on success
            if b.consecutive_failures > 0:
                b.consecutive_failures = 0

    def record_failure(self, exchange: str = "okx", error_msg: str = "") -> None:
        """Record a failed API call."""
        b = self._get(exchange)
        b.consecutive_failures += 1
        b.last_failure_time = time.time()

        if b.state == CircuitState.HALF_OPEN:
            self._open(exchange)
        elif b.state == CircuitState.CLOSED and b.consecutive_failures >= self.FAILURE_THRESHOLD:
            self._open(exchange)

    def is_open(self, exchange: str = "okx") -> bool:
        """
        Check if circuit is open (all calls should be blocked).
        Also handles transition OPEN → HALF_OPEN when recovery timeout elapses.
        """
        b = self._get(exchange)

        if b.state == CircuitState.CLOSED:
            return False

        if b.state == CircuitState.OPEN:
            elapsed = time.time() - b.opened_at
            if elapsed >= self.RECOVERY_TIMEOUT:
                self._half_open(exchange)
                return False
            return True

        # HALF_OPEN — allow limited calls
        return False

    def is_repair_allowed(self, exchange: str = "okx") -> bool:
        """
        Check if repair operations are allowed.
        In CLOSED or HALF_OPEN: allowed.
        In OPEN: blocked.
        """
        return not self.is_open(exchange)

    def state(self, exchange: str = "okx") -> str:
        """Get current circuit state as string."""
        return self._get(exchange).state.value

    def stats(self, exchange: str = "okx") -> dict:
        """Get breaker statistics."""
        b = self._get(exchange)
        return {
            "state": b.state.value,
            "consecutive_failures": b.consecutive_failures,
            "last_failure_time": b.last_failure_time,
            "last_success_time": b.last_success_time,
            "opened_at": b.opened_at,
        }

    def reset(self, exchange: str = "okx") -> None:
        """Force-reset the circuit breaker (for testing)."""
        self._breakers[exchange] = BreakerState()

    def _open(self, exchange: str) -> None:
        b = self._get(exchange)
        b.state = CircuitState.OPEN
        b.opened_at = time.time()
        b.half_open_requests = 0
        L.warning(
            f"[CircuitBreaker] {exchange}: CIRCUIT OPEN — "
            f"{b.consecutive_failures} consecutive failures, "
            f"recovery in {self.RECOVERY_TIMEOUT}s"
        )

    def _half_open(self, exchange: str) -> None:
        b = self._get(exchange)
        b.state = CircuitState.HALF_OPEN
        b.half_open_requests = 0
        L.info(f"[CircuitBreaker] {exchange}: HALF_OPEN — testing recovery")

    def _close(self, exchange: str) -> None:
        b = self._get(exchange)
        b.state = CircuitState.CLOSED
        b.consecutive_failures = 0
        b.half_open_requests = 0
        L.success(f"[CircuitBreaker] {exchange}: CIRCUIT CLOSED — recovered")


# Global singleton
circuit_breaker = CircuitBreaker()


def is_failure_error(error_msg: str) -> bool:
    """Check if an error message matches a known failure pattern."""
    msg_lower = error_msg.lower()
    for pattern in FAILURE_PATTERNS:
        if pattern.lower() in msg_lower:
            return True
    return False
