"""
Position Manager — DEPRECATED thin wrapper around Runtime's PositionTracker.

All position data now comes from core.exchange_runtime.runtime.
This module is kept for backward compatibility only.

The WS connection, Watchdog, and REST fallback have been REMOVED.
Those responsibilities now live in core/ws_connection.py and core/exchange_runtime.py.

Usage (deprecated — prefer runtime.get_position()):
    from exchange_engine.position_manager import position_manager
    pos = position_manager.get_position("BTCUSDT", "long")
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger


class PositionManager:
    """
    DEPRECATED — delegates all position queries to Runtime's PositionTracker.

    No WS. No Watchdog. No REST fallback. Just a pass-through.
    """

    def __init__(self):
        self._exchange_name: str = "okx"
        self._running: bool = False

    # ======================================================================
    # Lifecycle (no-op — Runtime handles this)
    # ======================================================================

    async def start(self, api_key: str = "", api_secret: str = "", passphrase: str = "",
                    exchange_name: str = "okx") -> None:
        """No-op. Runtime handles WS + Watchdog + REST initialization."""
        self._exchange_name = exchange_name
        self._running = True
        logger.info(f"[{exchange_name}] PositionManager: delegating to Runtime (thin mode)")

    async def stop(self) -> None:
        """No-op. Runtime handles cleanup."""
        self._running = False
        logger.info(f"[{self._exchange_name}] PositionManager stopped (thin mode)")

    # ======================================================================
    # Public interface — delegates to Runtime
    # ======================================================================

    def get_snapshot(self) -> dict[tuple[str, str], dict]:
        """Delegate to Runtime's PositionTracker."""
        from core.exchange_runtime import runtime
        tracker = runtime.position_tracker
        if tracker is None:
            return {}
        # Reconstruct snapshot format from tracker data
        positions = tracker.get_positions()
        result = {}
        for p in positions:
            sym = p.get("symbol", "")
            side = p.get("side", "long")
            result[(sym, side)] = {
                "contracts": p.get("contracts", 0),
                "entry_price": p.get("entry_price", 0),
                "side": side,
                "leverage": p.get("leverage", 1),
                "margin_mode": p.get("margin_mode", "cross"),
            }
        return result

    def get_snapshot_raw(self) -> list[dict]:
        """Delegate to Runtime's PositionTracker."""
        from core.exchange_runtime import runtime
        tracker = runtime.position_tracker
        if tracker is None:
            return []
        return tracker.get_positions()

    def get_position(self, symbol: str, side: str | None = None) -> dict | None:
        """Delegate to Runtime.get_position()."""
        from core.exchange_runtime import runtime
        return runtime.get_position(symbol, side)

    def is_healthy(self) -> bool:
        """Delegate to Runtime's tracker freshness."""
        from core.exchange_runtime import runtime
        tracker = runtime.position_tracker
        if tracker is None:
            return False
        return tracker.is_fresh

    def get_age(self) -> float:
        """Seconds since last tracker update."""
        from core.exchange_runtime import runtime
        tracker = runtime.position_tracker
        if tracker is None:
            return float("inf")
        return tracker.last_update_ago

    def get_metadata(self) -> dict:
        """Health metadata from Runtime."""
        from core.exchange_runtime import runtime
        tracker = runtime.position_tracker
        if tracker is None:
            return {"position_count": 0, "ws_connected": False}
        return {
            "refresh_count": 0,
            "failure_count": 0,
            "last_update_time": tracker._last_update if hasattr(tracker, '_last_update') else 0,
            "age_seconds": self.get_age(),
            "position_count": tracker.get_position_count(),
            "ws_connected": runtime.ws_state == "RUNNING",
            "idle": False,
            "version": 0,
        }

    def get_ws_health(self) -> dict:
        """WS health from Runtime."""
        from core.exchange_runtime import runtime
        return {
            "connected": runtime.ws_state == "RUNNING",
            "last_message_time": 0,
            "last_pong_time": 0,
            "last_connect_time": 0,
            "disconnect_reason": "",
            "channels": ["positions", "orders"],
        }

    # ======================================================================
    # Deprecated methods — no-ops
    # ======================================================================

    def exit_idle(self) -> None:
        """No-op. Idle state is managed by Runtime."""
        pass

    async def rest_refresh(self) -> bool:
        """No-op. Refreshes are NOT done via this path anymore."""
        return True


# Global singleton — kept for backward compatibility
position_manager = PositionManager()
