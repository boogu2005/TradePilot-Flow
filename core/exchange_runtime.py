"""
Exchange Runtime — UNIFIED entry point for ALL exchange operations.

This is the ONLY module that:
  - Manages WebSocket connections
  - Makes REST API calls
  - Provides position/order/algo/balance data
  - Creates/cancels/modifies orders and algo orders

Rules enforced by this module:
  1. Only ExchangeRuntime imports exchange_engine.exchange
  2. Only ExchangeRuntime can call REST APIs
  3. Only ExchangeRuntime manages the WS connection
  4. All modules must go through Runtime for ANY exchange operation

Usage:
    from core.exchange_runtime import runtime

    # Lifecycle
    await runtime.start(api_key, api_secret, passphrase)
    await runtime.shutdown()

    # Trading (REST, queued, rate-limited, circuit-breaker-protected)
    order = await runtime.place_order(symbol, type, side, amount, price)
    await runtime.cancel_order(order_id, symbol)
    await runtime.cancel_algo_order(algo_id, symbol)
    await runtime.create_algo_order(...)

    # Data (WS-first, zero REST during normal operation)
    pos = runtime.get_position("BTCUSDT", "long")
    positions = runtime.get_positions()
    orders = runtime.get_orders("BTCUSDT")
    algo = runtime.get_algo_order("algo_id_123")
    balance = runtime.get_balance()

    # Health
    health = runtime.get_health()
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional, Any

from loguru import logger

L = logger.bind(module="runtime")


class ExchangeRuntime:
    """
    Unified Exchange Runtime — the SINGLE entry point for all exchange operations.

    Singleton — use the global `runtime` instance.
    """

    def __init__(self):
        self._exchange_name = "okx"
        self._started = False
        self._ready = False

        # Lazy imports to avoid circular deps
        self._ws_conn = None
        self._position_tracker = None
        self._order_tracker = None
        self._balance_tracker = None
        self._algo_tracker = None
        self._rest_scheduler = None
        self._health = None

    # ======================================================================
    # Lifecycle
    # ======================================================================

    async def start(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        exchange_name: str = "okx",
        wait_ready: bool = True,
        ready_timeout: float = 60.0,
    ) -> bool:
        """
        Start the Exchange Runtime.

        1. Initialize REST layer (reuse existing exchange_engine)
        2. Initialize WS connection with all channels
        3. Start REST scheduler
        4. Run startup recovery
        5. Mark ready

        Returns True if Runtime is ready for trading.
        """
        if self._started:
            L.warning("[Runtime] Already started")
            return self._ready

        self._exchange_name = exchange_name
        self._started = True

        # Lazy-init components
        from .ws_connection import WsConnection
        from .position_tracker import position_tracker
        from .order_tracker import order_tracker
        from .balance_tracker import balance_tracker
        from .algo_order_tracker import algo_order_tracker
        from .rest_scheduler import rest_scheduler
        from .exchange_health import health_monitor

        self._ws_conn = WsConnection(exchange_name)
        self._position_tracker = position_tracker
        self._order_tracker = order_tracker
        self._balance_tracker = balance_tracker
        self._algo_tracker = algo_order_tracker
        self._rest_scheduler = rest_scheduler
        self._health = health_monitor

        # Step 1: Start REST scheduler
        await self._rest_scheduler.start()

        # Step 2: Set up WS subscriptions
        sub_mgr = self._ws_conn.subscription_manager
        sub_mgr.add("positions", inst_type="SWAP")
        sub_mgr.add("orders", inst_type="SWAP")
        sub_mgr.add("account", inst_type="SWAP")
        sub_mgr.add("balance_and_position", inst_type="SWAP")
        # NOTE: algo-orders channel does NOT support instType=SWAP (only SPOT/MARGIN/FUTURES).
        # Algo orders (SL/TP) are managed via REST API instead.

        # Step 3: Set credentials and start WS
        self._ws_conn.set_credentials(api_key, api_secret, passphrase)
        await self._ws_conn.start()

        # Step 4: Wait for WS to be RUNNING, then run startup recovery
        if wait_ready:
            from .startup_recover import startup_recover
            from database.db import get_session

            result = await startup_recover(
                session_factory=get_session,
                ws_connection=self._ws_conn,
                position_tracker=self._position_tracker,
                order_tracker=self._order_tracker,
                balance_tracker=self._balance_tracker,
                algo_order_tracker=self._algo_tracker,
                exchange_name=exchange_name,
                timeout=ready_timeout,
            )
            self._ready = True
            L.success(f"[Runtime] Ready: {result}")
            return True
        else:
            self._ready = True
            return True

    async def shutdown(self) -> None:
        """Gracefully shutdown the Runtime."""
        self._ready = False
        self._started = False

        if self._ws_conn:
            await self._ws_conn.stop()

        if self._rest_scheduler:
            await self._rest_scheduler.stop()

        L.info("[Runtime] Shutdown complete")

    # ======================================================================
    # Data Access (WS-first, zero REST)
    # ======================================================================

    def get_position(self, symbol: str, side: str | None = None) -> dict | None:
        """Get a position from the PositionTracker (WS data). Zero HTTP."""
        if not self._position_tracker:
            return None
        return self._position_tracker.get_position(symbol, side)

    def get_positions(self, side: str | None = None) -> list[dict]:
        """Get all positions from the PositionTracker (WS data). Zero HTTP."""
        if not self._position_tracker:
            return []
        return self._position_tracker.get_positions(side)

    def get_position_count(self) -> int:
        if not self._position_tracker:
            return 0
        return self._position_tracker.get_position_count()

    def get_order(self, order_id: str) -> dict | None:
        """Get an order from the OrderTracker (WS data). Zero HTTP."""
        if not self._order_tracker:
            return None
        return self._order_tracker.get_order(order_id)

    def get_orders(self, symbol: str | None = None) -> list[dict]:
        """Get open orders from the OrderTracker (WS data). Zero HTTP."""
        if not self._order_tracker:
            return []
        return self._order_tracker.get_open_orders(symbol)

    def get_algo_order(self, algo_id: str) -> dict | None:
        """Get an algo order from the AlgoOrderTracker (WS data). Zero HTTP."""
        if not self._algo_tracker:
            return None
        return self._algo_tracker.get_algo_order(algo_id)

    def has_live_algo(self, algo_id: str) -> bool:
        """Check if an algo order is live (WS data). Zero HTTP."""
        if not self._algo_tracker:
            return False
        return self._algo_tracker.has_live_algo(algo_id)

    def get_live_algo_orders(self, symbol: str | None = None) -> list[dict]:
        """Get live algo orders (WS data). Zero HTTP."""
        if not self._algo_tracker:
            return []
        return self._algo_tracker.get_live_algo_orders(symbol)

    def get_balance(self) -> dict:
        """Get balance from the BalanceTracker (WS data). Zero HTTP."""
        if not self._balance_tracker:
            return {"free": 0, "total": 0}
        return self._balance_tracker.get_balance()

    # ======================================================================
    # Trading Operations (REST, queued, rate-limited, circuit-breaker-protected)
    # ======================================================================

    async def place_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        reduce_only: bool = False,
        pos_side: str = "",
        **extra_params,
    ) -> dict:
        """
        Place an order on the exchange.
        Queued, rate-limited, circuit-breaker-protected.
        """
        from exchange_engine.exchange import create_order

        async def _do():
            return await create_order(
                symbol, order_type, side, amount, price,
                reduce_only=reduce_only, exchange=self._exchange_name,
                pos_side=pos_side, **extra_params,
            )

        return await self._rest_scheduler.schedule(
            "create_order", _do, priority=1,
            label=f"place_order({symbol}, {order_type}, {side})",
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an order. Queued, rate-limited, circuit-breaker-protected."""
        from exchange_engine.exchange import cancel_order as _cancel

        async def _do():
            await _cancel(order_id, symbol, exchange=self._exchange_name)
            return True

        try:
            return await self._rest_scheduler.schedule(
                "cancel_order", _do, priority=1,
                label=f"cancel_order({order_id[:16]})",
            )
        except Exception as e:
            L.warning(f"[Runtime] cancel_order failed: {e}")
            return False

    async def create_algo_order(
        self,
        algo_type: str,  # "stoploss" or "takeprofit"
        symbol: str,
        side: str,
        amount: float,
        trigger_price: float,
        client_order_id: str = "",
    ) -> dict:
        """Create an algo order (SL or TP). Queued, rate-limited, circuit-breaker-protected."""
        from exchange_engine.exchange import create_stoploss_order, create_tp_order

        async def _do():
            if algo_type == "stoploss":
                return await create_stoploss_order(
                    symbol, side, amount, trigger_price,
                    client_order_id=client_order_id,
                    exchange=self._exchange_name,
                )
            else:
                return await create_tp_order(
                    symbol, side, amount, trigger_price,
                    client_order_id=client_order_id,
                    exchange=self._exchange_name,
                )

        return await self._rest_scheduler.schedule(
            "create_order", _do, priority=1,
            label=f"create_{algo_type}({symbol})",
        )

    async def cancel_algo_order(self, algo_id: str, symbol: str) -> bool:
        """Cancel an algo order. Queued, rate-limited, circuit-breaker-protected."""
        from exchange_engine.exchange import cancel_algo_order_by_id

        async def _do():
            return await cancel_algo_order_by_id(algo_id, symbol, exchange=self._exchange_name)

        try:
            return await self._rest_scheduler.schedule(
                "cancel_order", _do, priority=1,
                label=f"cancel_algo({algo_id[:16]})",
            )
        except Exception as e:
            L.warning(f"[Runtime] cancel_algo_order failed: {e}")
            return False

    async def set_leverage(self, symbol: str, leverage: int, side: str = "long") -> bool:
        """Set leverage. Queued, rate-limited."""
        from exchange_engine.exchange import set_leverage as _set_lev

        async def _do():
            return await _set_lev(symbol, leverage, exchange=self._exchange_name, side=side)

        try:
            return await self._rest_scheduler.schedule(
                "set_leverage", _do, priority=1,
                label=f"set_leverage({symbol}, {leverage}x)",
            )
        except Exception as e:
            L.warning(f"[Runtime] set_leverage failed: {e}")
            return False

    async def fetch_ticker(self, symbol: str) -> dict:
        """Get ticker. Uses REST with 1s cache."""
        from exchange_engine.exchange import fetch_ticker

        async def _do():
            return await fetch_ticker(symbol, exchange=self._exchange_name)

        return await self._rest_scheduler.schedule(
            "fetch_ticker", _do, priority=3,
            label=f"fetch_ticker({symbol})",
        )

    # ======================================================================
    # Health & Status
    # ======================================================================

    def get_health(self) -> dict:
        """Get structured runtime health."""
        if not self._health:
            return {"state": "NOT_STARTED"}
        h = self._health.snapshot()
        return {
            "state": h.state,
            "uptime_seconds": h.uptime_seconds,
            "ws": {
                "connected": h.ws.connected,
                "state": h.ws.state,
                "rtt_ms": h.ws.rtt_ms,
                "reconnect_count": h.ws.reconnect_count,
                "subscriptions": h.ws.subscriptions,
            },
            "rest": {
                "healthy": h.rest.healthy,
                "queue_size": h.rest.queue_size,
                "circuit_breakers": h.rest.circuit_breaker_states,
                "failures_1h": h.rest.failures_last_hour,
            },
            "trackers": {
                "positions": h.trackers.positions_count,
                "orders": h.trackers.orders_count,
                "algo_orders": h.trackers.algo_orders_count,
                "last_ws_update_ago": h.trackers.last_ws_update_ago,
            },
        }

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def ws_state(self) -> str:
        if not self._ws_conn:
            return "NOT_STARTED"
        return self._ws_conn.state_machine.state_value

    @property
    def position_tracker(self):
        return self._position_tracker

    @property
    def order_tracker(self):
        return self._order_tracker

    @property
    def balance_tracker(self):
        return self._balance_tracker

    @property
    def algo_tracker(self):
        return self._algo_tracker


# Global singleton — THE single entry point for all exchange operations
runtime = ExchangeRuntime()
