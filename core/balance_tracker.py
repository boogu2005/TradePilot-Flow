"""
Balance Tracker — SINGLE source of truth for account balance.

Consumes WS balance updates via EventBus.
Replaces the need for REST fetch_balance() polling.
"""
from __future__ import annotations

import time

from loguru import logger

from .runtime_events import event_bus
from .exchange_health import health_monitor

L = logger.bind(module="balance_tracker")


class BalanceTracker:
    """
    The ONLY balance cache in the system.

    Populated by WS (balance_update events from account or balance_and_position channels).
    All modules read from here.
    """

    def __init__(self):
        self._balance: dict = {
            "free": 0.0,
            "total": 0.0,
            "equity": 0.0,
            "available": 0.0,
            "margin": 0.0,
            "unrealized_pnl": 0.0,
            "currency": "USDT",
        }
        self._last_update: float = 0.0
        self._initialized: bool = False  # True after first WS data received

        event_bus.subscribe("balance_update", self._on_balance_update)

    async def _on_balance_update(self, data: dict) -> None:
        self._balance = {
            "free": data.get("available", data.get("free", self._balance.get("free", 0))),
            "total": data.get("balance", data.get("total", self._balance.get("total", 0))),
            "equity": data.get("equity", data.get("balance", data.get("total", 0))),
            "available": data.get("available", data.get("free", 0)),
            "margin": data.get("margin", self._balance.get("margin", 0)),
            "unrealized_pnl": data.get("unrealized_pnl", self._balance.get("unrealized_pnl", 0)),
            "currency": data.get("currency", "USDT"),
        }
        self._last_update = time.time()
        self._initialized = True  # Mark initialized on first WS data

    def get_balance(self) -> dict:
        """Get current balance snapshot."""
        return dict(self._balance)

    @property
    def free(self) -> float:
        return self._balance.get("free", 0)

    @property
    def total(self) -> float:
        return self._balance.get("total", 0)

    @property
    def equity(self) -> float:
        return self._balance.get("equity", 0)

    @property
    def unrealized_pnl(self) -> float:
        return self._balance.get("unrealized_pnl", 0)

    @property
    def last_update_ago(self) -> float:
        if self._last_update == 0:
            return float("inf")
        return time.time() - self._last_update

    @property
    def is_fresh(self) -> bool:
        """True if tracker has received WS data and it's recent."""
        if not self._initialized:
            return False
        return self.last_update_ago < 20

    def update_from_rest(self, balance: dict) -> None:
        """Update from REST (startup recovery only)."""
        if self._last_update > 0 and self.last_update_ago < 5:
            return
        self._balance = {
            "free": balance.get("free", self._balance.get("free", 0)),
            "total": balance.get("total", self._balance.get("total", 0)),
            "equity": balance.get("equity", balance.get("total", 0)),
            "available": balance.get("available", balance.get("free", 0)),
            "margin": balance.get("margin", self._balance.get("margin", 0)),
            "unrealized_pnl": balance.get("unrealized_pnl", self._balance.get("unrealized_pnl", 0)),
            "currency": balance.get("currency", "USDT"),
        }
        self._last_update = time.time()
        health_monitor.tracker_rest_sync()

    def reset(self) -> None:
        self._balance = {"free": 0.0, "total": 0.0, "equity": 0.0, "available": 0.0,
                        "margin": 0.0, "unrealized_pnl": 0.0, "currency": "USDT"}
        self._last_update = 0.0


# Global singleton
balance_tracker = BalanceTracker()
