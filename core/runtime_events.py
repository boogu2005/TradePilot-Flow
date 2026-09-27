"""
Runtime EventBus — decouples WebSocket data from business logic.

All WS messages flow through the EventBus. Trackers subscribe to relevant
channels. Application modules register callbacks. No module talks to WS directly.

Channels:
  position_update   — {symbol, side, contracts, entry_price, mark_price, ...}
  order_update      — {order_id, symbol, side, type, state, filled, price, ...}
  algo_order_update — {algo_id, symbol, type, state, side, sz, trigger_px, ...}
  balance_update    — {free, total, unrealized_pnl, ...}
  account_update    — {event, ...}
  fill_update       — {order_id, symbol, side, price, amount, fee, ...}
  ws_state_change   — {old_state, new_state, reason}
  runtime_ready     — {}  — fired once after startup recovery completes
  runtime_error     — {error, module}
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Callable, Awaitable, Any

from loguru import logger

L = logger.bind(module="eventbus")

# Callback type: async callable receiving dict
Callback = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    """
    Simple publish-subscribe event bus for Runtime events.

    - Synchronous dispatch (all subscribers are notified in order)
    - Each subscriber's callback is awaited — slow subscribers delay others
    - Subscriber errors are logged but never propagate (one bad subscriber doesn't break others)
    """

    def __init__(self):
        self._subscribers: dict[str, list[Callback]] = defaultdict(list)
        self._event_counts: dict[str, int] = defaultdict(int)

    def subscribe(self, channel: str, callback: Callback) -> None:
        """Register a callback for a channel. Idempotent for same callback."""
        if callback not in self._subscribers[channel]:
            self._subscribers[channel].append(callback)
            L.debug(f"[EventBus] +subscribe: {channel} (total={len(self._subscribers[channel])})")

    def unsubscribe(self, channel: str, callback: Callback) -> None:
        """Remove a callback from a channel."""
        if callback in self._subscribers[channel]:
            self._subscribers[channel].remove(callback)
            L.debug(f"[EventBus] -unsubscribe: {channel} (total={len(self._subscribers[channel])})")

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        """
        Publish an event to all subscribers of a channel.
        Subscriber errors are logged, never raised.
        """
        self._event_counts[channel] += 1
        subscribers = self._subscribers.get(channel, [])
        if not subscribers:
            return

        for cb in subscribers:
            try:
                await cb(data)
            except Exception as e:
                L.warning(f"[EventBus] {channel} subscriber error: {type(e).__name__}: {e}")

    def publish_sync(self, channel: str, data: dict[str, Any]) -> None:
        """
        Synchronous publish — fires the event in the background.
        Use when the caller is not in an async context.
        """
        asyncio.create_task(self.publish(channel, data))

    @property
    def subscriber_count(self) -> dict[str, int]:
        return {ch: len(cbs) for ch, cbs in self._subscribers.items()}

    @property
    def event_counts(self) -> dict[str, int]:
        return dict(self._event_counts)

    def reset(self) -> None:
        """Reset all state (testing only)."""
        self._subscribers.clear()
        self._event_counts.clear()


# Global singleton
event_bus = EventBus()
