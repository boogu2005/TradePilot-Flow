"""
Reconnect Manager — jittered exponential backoff for WS reconnection.

Produces delays: ~3s, ~6s, ~12s, ~24s, ~30s, ~30s, ...
With ±25% random jitter to avoid thundering herd.

Also tracks total reconnects and provides reset on successful RUNNING state.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from loguru import logger

L = logger.bind(module="reconnect")


@dataclass
class ReconnectState:
    attempt: int = 0
    total_reconnects: int = 0
    base_delay: float = 3.0
    max_delay: float = 30.0
    jitter_pct: float = 0.25
    last_delay: float = 0.0
    last_attempt_time: float = 0.0


class ReconnectManager:
    """
    Manages reconnection backoff with jitter.

    Usage:
        mgr = ReconnectManager()
        delay = mgr.next_delay()  # → ~3s (with jitter)
        await asyncio.sleep(delay)
        # reconnect...
        mgr.reset()  # on successful RUNNING
    """

    def __init__(self, base_delay: float = 3.0, max_delay: float = 30.0, jitter_pct: float = 0.25):
        self._state = ReconnectState(
            base_delay=base_delay,
            max_delay=max_delay,
            jitter_pct=jitter_pct,
        )

    def next_delay(self) -> float:
        """
        Calculate the next reconnect delay with jitter.
        Exponential: base * 2^attempt, capped at max_delay.
        """
        self._state.attempt += 1
        self._state.total_reconnects += 1
        self._state.last_attempt_time = time.time()

        # Exponential: 3, 6, 12, 24, 30, 30...
        exp = self._state.base_delay * (2 ** (self._state.attempt - 1))
        delay = min(exp, self._state.max_delay)

        # Jitter: ±jitter_pct%
        jitter = delay * self._state.jitter_pct * (random.random() * 2 - 1)
        delay = max(1.0, delay + jitter)
        delay = min(self._state.max_delay * 1.5, delay)

        self._state.last_delay = delay

        L.info(
            f"[Reconnect] attempt={self._state.attempt} "
            f"total={self._state.total_reconnects} "
            f"delay={delay:.1f}s (base={exp:.0f}s)"
        )
        return delay

    def reset(self) -> None:
        """Reset attempt counter (call on successful RUNNING)."""
        self._state.attempt = 0
        self._state.last_delay = 0.0

    @property
    def attempt(self) -> int:
        return self._state.attempt

    @property
    def total_reconnects(self) -> int:
        return self._state.total_reconnects

    @property
    def last_delay(self) -> float:
        return self._state.last_delay

    def stats(self) -> dict:
        return {
            "attempt": self._state.attempt,
            "total_reconnects": self._state.total_reconnects,
            "last_delay": round(self._state.last_delay, 1),
        }
