"""
WebSocket State Machine — manages the full WS lifecycle.

States:
  DISCONNECTED  → Initial state, no connection
  CONNECTING    → TCP connection in progress
  LOGIN → Login sent, waiting for response
  SUBSCRIBING   → Subscribe requests sent, waiting for confirmations
  RUNNING       → Fully operational, receiving data
  RECONNECTING  → Connection lost, waiting for backoff
  FAILED        → Max retries exceeded or unrecoverable error

Transitions are GUARDED — invalid transitions are logged and rejected.
The state machine drives the WS lifecycle, replacing the old while-true loop.
"""
from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Optional, Callable, Awaitable

from loguru import logger

L = logger.bind(module="ws_sm")


class WsState(str, Enum):
    """OKX WebSocket lifecycle states — matches official OKX connection flow."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    LOGIN = "LOGIN"
    SUBSCRIBING = "SUBSCRIBING"
    SYNCING = "SYNCING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


# Valid transitions
TRANSITIONS: dict[WsState, set[WsState]] = {
    WsState.DISCONNECTED:    {WsState.CONNECTING},
    WsState.CONNECTING:      {WsState.LOGIN, WsState.DISCONNECTED, WsState.FAILED},
    WsState.LOGIN:           {WsState.SUBSCRIBING, WsState.DISCONNECTED, WsState.FAILED},
    WsState.SUBSCRIBING:     {WsState.SYNCING, WsState.DISCONNECTED, WsState.FAILED},
    WsState.SYNCING:         {WsState.RUNNING, WsState.DISCONNECTED, WsState.FAILED},
    WsState.RUNNING:         {WsState.RECONNECTING, WsState.DISCONNECTED},
    WsState.RECONNECTING:    {WsState.CONNECTING, WsState.DISCONNECTED, WsState.FAILED},
    WsState.FAILED:          {WsState.CONNECTING, WsState.DISCONNECTED},
}

# Callback: async (old_state, new_state, reason) → None
StateCallback = Callable[[WsState, WsState, str], Awaitable[None]]


class WsStateMachine:
    """
    Deterministic WS lifecycle state machine.

    Usage:
        sm = WsStateMachine()
        sm.on_state_change(my_callback)

        sm.transition(WsState.CONNECTING)    # DISCONNECTED → CONNECTING
        sm.transition(WsState.LOGIN)         # CONNECTING → LOGIN
        ...
    """

    def __init__(self, exchange_name: str = "okx"):
        self._state: WsState = WsState.DISCONNECTED
        self._exchange_name = exchange_name
        self._callbacks: list[StateCallback] = []
        self._state_history: list[tuple[WsState, float, str]] = []  # (state, timestamp, reason)
        self._enter_time: float = time.time()

    @property
    def state(self) -> WsState:
        return self._state

    @property
    def state_value(self) -> str:
        return self._state.value

    @property
    def is_running(self) -> bool:
        return self._state == WsState.RUNNING

    @property
    def is_connected(self) -> bool:
        return self._state in (WsState.RUNNING, WsState.SUBSCRIBING)

    def on_state_change(self, callback: StateCallback) -> None:
        """Register a callback for state changes."""
        self._callbacks.append(callback)

    def transition(self, new_state: WsState, reason: str = "") -> bool:
        """
        Attempt a state transition. Returns True if valid and executed.
        Logs and returns False if the transition is invalid.
        """
        old = self._state

        if new_state not in TRANSITIONS.get(old, set()):
            L.warning(
                f"[{self._exchange_name}] Invalid WS transition: "
                f"{old.value} → {new_state.value} (reason={reason})"
            )
            return False

        self._state = new_state
        self._state_history.append((new_state, time.time(), reason))
        self._enter_time = time.time()

        if len(self._state_history) > 100:
            self._state_history = self._state_history[-50:]

        # Log transition — "WS STATE:" prefix for easy monitoring
        if new_state in (WsState.RUNNING, WsState.FAILED):
            L.success(f"[{self._exchange_name}] WS STATE: {old.value} → {new_state.value} ({reason})")
        elif new_state == WsState.RECONNECTING:
            L.warning(f"[{self._exchange_name}] WS STATE: {old.value} → {new_state.value} ({reason})")
        else:
            L.info(f"[{self._exchange_name}] WS STATE: {old.value} → {new_state.value} ({reason})")

        # Fire callbacks
        for cb in self._callbacks:
            try:
                asyncio.create_task(cb(old, new_state, reason))
            except Exception as e:
                L.debug(f"[{self._exchange_name}] State callback error: {e}")

        return True

    def time_in_state(self) -> float:
        """Seconds spent in current state."""
        return time.time() - self._enter_time

    def get_history(self, limit: int = 10) -> list[dict]:
        """Recent state transitions."""
        return [
            {"state": s.value, "timestamp": ts, "reason": r}
            for s, ts, r in self._state_history[-limit:]
        ]

    def reset(self) -> None:
        """Force reset to DISCONNECTED (for testing)."""
        self._state = WsState.DISCONNECTED
        self._state_history.clear()
