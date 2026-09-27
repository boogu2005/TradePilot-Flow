"""
Algo Order Tracker — SINGLE source of truth for algo orders (SL/TP/trigger).

Consumes WS algo-order updates via EventBus.
Provides O(1) lookup by algo_id.
Replaces the need for REST fetch_pending_algo_orders() and fetch_algo_order_by_id().
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from .runtime_events import event_bus
from .exchange_health import health_monitor

L = logger.bind(module="algo_tracker")


class AlgoOrderTracker:
    """
    The ONLY algo order cache in the system.

    Populated by WS (algo_order_update events).
    All modules read from here. No REST fetch_algo_* calls.
    """

    def __init__(self):
        self._algo_orders: dict[str, dict] = {}  # algo_id → algo order
        self._last_update: float = 0.0
        self._initialized: bool = False  # True after first WS data received

        event_bus.subscribe("algo_order_update", self._on_algo_order_update)

    async def _on_algo_order_update(self, data: dict) -> None:
        algo_id = data.get("algo_id", "")
        if not algo_id:
            return

        state = data.get("state", "")

        if state in ("canceled", "cancelled", "filled", "expired", "triggered"):
            existing = self._algo_orders.get(algo_id, {})
            existing.update(data)
            existing["_closed"] = True
            self._algo_orders[algo_id] = existing
        else:
            self._algo_orders[algo_id] = {**data, "_closed": False}

        self._last_update = time.time()
        self._initialized = True  # Mark initialized on first WS data
        health_monitor.tracker_algo_orders(len(self._algo_orders))
        health_monitor.tracker_ws_update()

    def get_algo_order(self, algo_id: str) -> dict | None:
        """Get a single algo order by its algoId. O(1)."""
        return self._algo_orders.get(algo_id)

    def get_live_algo_orders(self, symbol: str | None = None) -> list[dict]:
        """Get all live (active) algo orders, optionally filtered by symbol."""
        result = []
        norm_sym = (symbol or "").upper().replace("/", "").replace(":USDT", "")
        for a in self._algo_orders.values():
            if a.get("_closed"):
                continue
            if norm_sym:
                a_sym = a.get("symbol", "").upper().replace("/", "").replace(":USDT", "")
                if a_sym != norm_sym:
                    continue
            result.append(a)
        return result

    def get_algo_orders_by_symbol(self, symbol: str) -> list[dict]:
        """Get all algo orders for a symbol (including closed)."""
        norm_sym = symbol.upper().replace("/", "").replace(":USDT", "")
        return [
            a for a in self._algo_orders.values()
            if a.get("symbol", "").upper().replace("/", "").replace(":USDT", "") == norm_sym
        ]

    def has_live_algo(self, algo_id: str) -> bool:
        """Check if an algo order is live on exchange."""
        a = self._algo_orders.get(algo_id)
        return a is not None and not a.get("_closed", False) and a.get("state") == "live"

    @property
    def algo_count(self) -> int:
        return len(self._algo_orders)

    @property
    def live_count(self) -> int:
        return len(self.get_live_algo_orders())

    @property
    def last_update_ago(self) -> float:
        if self._last_update == 0:
            return float("inf")
        return time.time() - self._last_update

    @property
    def is_fresh(self) -> bool:
        """True if tracker has received WS data (may be empty if no algo orders)."""
        if not self._initialized:
            return False
        return True  # Algo order data is event-driven; once we get any update, it's fresh

    def update_from_rest(self, algo_orders: list[dict]) -> None:
        """Bulk update from REST (startup recovery only)."""
        for a in algo_orders:
            aid = a.get("algoId", "")
            if not aid:
                continue
            self._algo_orders[aid] = {
                "algo_id": aid,
                "symbol": a.get("instId", ""),
                "order_type": a.get("ordType", ""),
                "side": a.get("side", ""),
                "state": a.get("state", ""),
                "amount": float(a.get("sz", 0)),
                "trigger_price": float(a.get("triggerPx", 0)),
                "_closed": a.get("state", "") not in ("live", "pending"),
            }
        if algo_orders:
            self._last_update = time.time()
            health_monitor.tracker_rest_sync()

    def reset(self) -> None:
        self._algo_orders.clear()
        self._last_update = 0.0


# Global singleton
algo_order_tracker = AlgoOrderTracker()
