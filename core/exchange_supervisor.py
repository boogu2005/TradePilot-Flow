"""
Exchange Supervisor — thin lifecycle wrapper for the legacy CCXT exchange module.

This module NO LONGER does health checks, reconnection, or REST polling.
All of that is now handled by ExchangeRuntime + WsConnection.

The supervisor's only remaining jobs:
  1. Start/stop the legacy exchange module during app lifecycle
  2. Register exchange cleanup with AppLifecycle
  3. Provide wait_until_connected() for startup sequencing
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional
from loguru import logger

from exchange_engine import exchange as ex
from .lifecycle import AppLifecycle
from .states import ServiceRegistry, ModuleState, MOD_EXCHANGE


class ExchangeSupervisor:
    """Thin lifecycle wrapper — delegates health/reconnect to ExchangeRuntime."""

    def __init__(self, shutdown_event: asyncio.Event, lifecycle: AppLifecycle):
        self.shutdown_event = shutdown_event
        self._lifecycle = lifecycle
        self._task: Optional[asyncio.Task] = None
        self._exchange_name = "okx"
        self._ever_connected = False

        # Register exchange cleanup with lifecycle
        lifecycle.register(
            f"exchange:{self._exchange_name}",
            self._close_exchange,
            timeout=15.0,
        )

    # —————— Start / Stop ——————

    def start(self) -> None:
        """Start the supervisor (non-blocking)."""
        if self._task is not None and not self._task.done():
            logger.warning("[ExchangeSupervisor] Already started")
            return
        ServiceRegistry.set_state(MOD_EXCHANGE, ModuleState.INITIALIZING)
        self._task = asyncio.create_task(self._run(), name="ExchangeSupervisor")
        logger.info("[ExchangeSupervisor] Started (thin mode — delegates to Runtime)")

    async def stop(self) -> None:
        """Stop the supervisor."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        ServiceRegistry.set_state(MOD_EXCHANGE, ModuleState.STOPPED)
        logger.info("[ExchangeSupervisor] Stopped")

    # —————— Main loop (minimal) ——————

    async def _run(self) -> None:
        """Monitor exchange connectivity and auto-connect on startup/reconnect."""
        while not self.shutdown_event.is_set():
            try:
                if self._is_connected():
                    if not self._ever_connected:
                        self._ever_connected = True
                        ServiceRegistry.set_state(MOD_EXCHANGE, ModuleState.CONNECTED)
                        logger.success("[ExchangeSupervisor] Exchange connected")
                    # Sleep until shutdown or disconnect
                    await self._sleep(30)
                else:
                    ServiceRegistry.set_state(MOD_EXCHANGE, ModuleState.CONNECTING)
                    # —— 主动建立连接 ——
                    await self._try_connect()
                    if not self._is_connected():
                        await self._sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[ExchangeSupervisor] Error: {type(e).__name__}: {e}")
                await self._sleep(10)

    async def _try_connect(self) -> None:
        """Attempt to initialize or reconnect the exchange."""
        cfg = ex._exchange_configs.get(self._exchange_name)
        if not cfg:
            logger.debug("[ExchangeSupervisor] No cached credentials, skipping connect")
            return

        # Try reusing existing instance first, then full reconnect
        if self._exchange_name in ex._exchanges:
            reused = await ex.try_reuse_exchange(self._exchange_name)
            if reused:
                return
            await ex.reconnect_exchange(self._exchange_name)
        else:
            try:
                await ex.init_exchange(
                    self._exchange_name,
                    cfg["api_key"], cfg["api_secret"],
                    cfg["passphrase"], cfg["testnet"],
                )
            except Exception as e:
                logger.warning(
                    f"[ExchangeSupervisor] init_exchange failed: "
                    f"{type(e).__name__}: {e}"
                )

    # —————— Connectivity ——————

    def _is_connected(self) -> bool:
        """Check if the legacy exchange is connected."""
        return self._exchange_name in ex.get_active_exchanges()

    async def _close_exchange(self) -> None:
        """Close exchange (called by AppLifecycle on shutdown)."""
        await ex.close_all_exchanges()

    # —————— Utility ——————

    async def _sleep(self, seconds: float) -> None:
        """Sleep that wakes early on shutdown."""
        try:
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def wait_until_connected(self, timeout: float = 60.0) -> bool:
        """Wait for exchange to be connected (used during startup)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_connected():
                return True
            if self.shutdown_event.is_set():
                return False
            await self._sleep(2.0)
        return self._is_connected()
