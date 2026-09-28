"""
Position Tracker — SINGLE source of truth for all position data.

Consumes WS position updates via EventBus.
Provides O(1) lookup by (symbol, side).
No REST polling. No separate cache layers.
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from .runtime_events import event_bus
from .exchange_health import health_monitor

L = logger.bind(module="position_tracker")


class PositionTracker:
    """
    The ONLY position cache in the system.

    Populated by WS (position_update events).
    All modules read from here. No module fetches positions via REST.
    """

    def __init__(self):
        self._positions: dict[tuple[str, str], dict] = {}  # (normalized_symbol, side) → position
        self._positions_raw: list[dict] = []
        self._last_update: float = 0.0
        self._count: int = 0
        self._initialized: bool = False  # True after first WS data received

        # Subscribe to EventBus
        event_bus.subscribe("position_update", self._on_position_update)

    # ======================================================================
    # Symbol normalization — ONE canonical format for all stores and lookups
    # ======================================================================
    # OKX WS pushes "BTC-USDT-SWAP", REST may return "BTC/USDT:USDT" or "BTCUSDT",
    # trade.pair is "BTC/USDT:USDT". All get normalized to "BTCUSDT" (short form).

    @staticmethod
    def _norm_symbol(s: str) -> str:
        """Normalize ANY symbol format to short form: BTCUSDT"""
        return (s or "").upper() \
            .replace("/", "").replace(":USDT", "") \
            .replace("-USDT-SWAP", "USDT").replace("-USDC-SWAP", "USDC") \
            .replace("-SWAP", "")

    async def _on_position_update(self, data: dict) -> None:
        """Handle WS position update."""
        symbol = self._norm_symbol(data.get("symbol", ""))
        side = data.get("side", "long")
        contracts = data.get("contracts", 0)

        if contracts <= 0:
            # Position closed
            self._positions.pop((symbol, side), None)
        else:
            self._positions[(symbol, side)] = {
                "symbol": data.get("symbol", ""),  # Keep original for display
                "side": side,
                "contracts": contracts,
                "entry_price": data.get("entry_price", 0),
                "mark_price": data.get("mark_price", 0),
                "unrealized_pnl": data.get("unrealized_pnl", 0),
                "leverage": data.get("leverage", 1),
                "liquidation_price": data.get("liquidation_price"),
                "margin_mode": data.get("margin_mode", "cross"),
                "update_time": data.get("update_time", ""),
            }

        self._positions_raw = list(self._positions.values())
        self._count = len(self._positions)
        self._last_update = time.time()
        self._initialized = True  # Mark initialized on first WS data

        health_monitor.tracker_positions(self._count)
        health_monitor.tracker_ws_update()

    def get_position(self, symbol: str, side: str | None = None) -> dict | None:
        """
        Get a single position by symbol and optional side.
        Handles ALL input formats: BTC/USDT:USDT, BTCUSDT, BTC-USDT-SWAP
        """
        sym = self._norm_symbol(symbol)

        if side:
            key = (sym, side.lower())
            pos = self._positions.get(key)
            if pos is not None:
                return pos
            # v6: exact key miss with side → fall through to side-agnostic search
            # (e.g. if WS sent "long" but caller asks for "short" on a net-mode position)

        # Search without side
        for (s, sd), pos in self._positions.items():
            if s == sym:
                return pos

        return None

    def get_positions(self, side: str | None = None) -> list[dict]:
        """Get all tracked positions, optionally filtered by side."""
        if side:
            return [p for p in self._positions.values() if p["side"] == side.lower()]
        return self._positions_raw

    def get_position_count(self) -> int:
        return self._count

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
        return self.last_update_ago < 30

    @property
    def is_initialized(self) -> bool:
        """True if tracker has ever received data (WS or REST)."""
        return self._initialized

    def snapshot(self) -> dict:
        return {
            "count": self._count,
            "last_update_ago": round(self.last_update_ago, 1),
            "positions": self._positions_raw,
        }

    def update_from_rest(self, positions: list[dict]) -> None:
        """
        Bulk update from REST (startup recovery only).
        Does NOT replace WS data if WS is fresher.
        """
        if self._last_update > 0 and self.last_update_ago < 5:
            L.debug("PositionTracker: WS data is fresh, skipping REST update")
            return

        for p in positions:
            sym = self._norm_symbol(p.get("symbol", ""))
            side = (p.get("side") or "long").lower()
            contracts = p.get("contracts", 0)
            if contracts > 0:
                self._positions[(sym, side)] = {
                    "symbol": p.get("symbol", ""),  # Keep original
                    "side": side,
                    "contracts": contracts,
                    "entry_price": p.get("entry_price", 0),
                    "mark_price": p.get("mark_price", 0),
                    "unrealized_pnl": p.get("unrealized_pnl", 0),
                    "leverage": p.get("leverage", 1),
                    "liquidation_price": p.get("liquidation_price"),
                    "margin_mode": p.get("margin_mode", "cross"),
                }

        self._positions_raw = list(self._positions.values())
        self._count = len(self._positions)
        if positions:
            self._last_update = time.time()
            health_monitor.tracker_rest_sync()

    def mark_empty_snapshot(self) -> None:
        """
        Mark tracker as initialized even when there are zero positions.

        Called when WS sends an empty positions snapshot (no open positions).
        Without this, is_fresh stays False forever and sync always times out
        for accounts with no positions.
        """
        self._last_update = time.time()
        self._initialized = True
        health_monitor.tracker_positions(0)
        health_monitor.tracker_ws_update()

    def reset(self) -> None:
        self._positions.clear()
        self._positions_raw.clear()
        self._count = 0
        self._last_update = 0.0


# Global singleton
position_tracker = PositionTracker()
