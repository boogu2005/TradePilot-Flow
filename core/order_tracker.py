"""
Order Tracker — SINGLE source of truth for all order data.

Consumes WS order updates via EventBus.
Provides O(1) lookup by order_id.
Replaces the need for REST fetch_order() polling.
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from .runtime_events import event_bus
from .exchange_health import health_monitor

L = logger.bind(module="order_tracker")


class OrderTracker:
    """
    The ONLY order cache in the system.

    Populated by WS (order_update events).
    All modules read from here. No module fetches orders via REST.
    """

    def __init__(self):
        self._orders: dict[str, dict] = {}  # order_id → order
        self._last_update: float = 0.0
        self._initialized: bool = False  # True after first WS data received

        event_bus.subscribe("order_update", self._on_order_update)

    async def _on_order_update(self, data: dict) -> None:
        order_id = data.get("order_id", "")
        if not order_id:
            return

        state = data.get("state", "")

        if state in ("canceled", "cancelled", "filled", "expired"):
            # Terminal state — keep in cache for reference but mark closed
            existing = self._orders.get(order_id, {})
            existing.update(data)
            existing["_closed"] = True
            self._orders[order_id] = existing
        else:
            self._orders[order_id] = {**data, "_closed": False}

        self._last_update = time.time()
        self._initialized = True  # Mark initialized on first WS data
        health_monitor.tracker_orders(len(self._orders))
        health_monitor.tracker_ws_update()

    def get_order(self, order_id: str) -> dict | None:
        return self._orders.get(order_id)

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Get all open (non-terminal) orders, optionally filtered by symbol."""
        result = []
        norm_sym = (symbol or "").upper().replace("/", "").replace(":USDT", "")
        for o in self._orders.values():
            if o.get("_closed"):
                continue
            if norm_sym:
                o_sym = o.get("symbol", "").upper().replace("/", "").replace(":USDT", "")
                if o_sym != norm_sym:
                    continue
            result.append(o)
        return result

    def get_orders_by_symbol(self, symbol: str) -> list[dict]:
        norm_sym = symbol.upper().replace("/", "").replace(":USDT", "")
        return [
            o for o in self._orders.values()
            if o.get("symbol", "").upper().replace("/", "").replace(":USDT", "") == norm_sym
        ]

    @property
    def order_count(self) -> int:
        return len(self._orders)

    @property
    def open_order_count(self) -> int:
        return len(self.get_open_orders())

    @property
    def last_update_ago(self) -> float:
        if self._last_update == 0:
            return float("inf")
        return time.time() - self._last_update

    @property
    def is_fresh(self) -> bool:
        """True if tracker has received WS data (may be empty if no open orders)."""
        if not self._initialized:
            return False
        return True  # Order data is event-driven; once we get any update, it's fresh

    def update_from_rest(self, orders: list[dict]) -> None:
        """Bulk update from REST (startup recovery only)."""
        for o in orders:
            oid = o.get("id", o.get("ordId", ""))
            if not oid:
                continue
            self._orders[oid] = {
                "order_id": oid,
                "symbol": o.get("symbol", o.get("instId", "")),
                "side": o.get("side", ""),
                "order_type": o.get("type", o.get("ordType", "")),
                "state": o.get("status", o.get("state", "")),
                "price": float(o.get("price", o.get("px", 0))),
                "amount": float(o.get("amount", o.get("sz", 0))),
                "filled": float(o.get("filled", o.get("fillSz", 0))),
                "_closed": o.get("status", o.get("state", "")) in (
                    "canceled", "cancelled", "filled", "closed", "expired"
                ),
            }
        if orders:
            self._last_update = time.time()
            health_monitor.tracker_rest_sync()

    def reset(self) -> None:
        self._orders.clear()
        self._last_update = 0.0


# Global singleton
order_tracker = OrderTracker()
