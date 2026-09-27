"""
WebSocket Heartbeat — custom ping/pong management.

OKX requires: ping within 30s of last message, disconnect if no pong within 30s.

This heartbeat:
  - Sends ping if no message received in 25s
  - Expects pong within 5s of ping
  - If 2 consecutive pings miss pong → trigger reconnect callback
  - Every received message resets the timer

Replaces reliance on websockets library's built-in ping_interval.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Awaitable, Optional

from loguru import logger

L = logger.bind(module="heartbeat")

PING_INTERVAL = 25  # Send ping if no message for 25s
PONG_TIMEOUT = 5    # Expect pong within 5s
MAX_MISSED_PONGS = 2  # Reconnect after 2 missed pongs


class WsHeartbeat:
    """
    Custom WebSocket heartbeat manager.

    Usage:
        hb = WsHeartbeat(on_reconnect_needed=my_reconnect_fn)
        asyncio.create_task(hb.run())

        # On every received message:
        hb.on_message()

        # On pong:
        hb.on_pong()

        # Send ping manually (or let the heartbeat do it):
        # The heartbeat sends ping automatically via the ping_sender callback
    """

    def __init__(
        self,
        on_reconnect_needed: Callable[[], Awaitable[None]],
        ping_sender: Optional[Callable[[], Awaitable[None]]] = None,
        exchange_name: str = "okx",
    ):
        self._on_reconnect = on_reconnect_needed
        self._ping_sender = ping_sender
        self._exchange_name = exchange_name

        self._last_message_time: float = time.time()
        self._last_ping_sent: float = 0.0
        self._last_pong_time: float = time.time()
        self._missed_pongs: int = 0
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start the heartbeat loop."""
        self._running = True
        self._last_message_time = time.time()
        self._last_pong_time = time.time()
        self._missed_pongs = 0
        self._task = asyncio.create_task(self._run(), name=f"hb_{self._exchange_name}")
        L.info(f"[Heartbeat] Started (ping={PING_INTERVAL}s, timeout={PONG_TIMEOUT}s)")

    async def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        L.info(f"[Heartbeat] Stopped")

    def on_message(self) -> None:
        """Call on every received WS message. Resets the ping timer."""
        self._last_message_time = time.time()

    def on_pong(self) -> None:
        """Call when a pong is received."""
        self._last_pong_time = time.time()
        self._missed_pongs = 0
        if self._last_ping_sent > 0:
            rtt = (self._last_pong_time - self._last_ping_sent) * 1000
            L.debug(f"[Heartbeat] pong received, RTT={rtt:.0f}ms")

    def on_ping_sent(self) -> None:
        """Call when a ping is sent."""
        self._last_ping_sent = time.time()

    async def _run(self) -> None:
        """Main heartbeat loop — checks every 1 second."""
        while self._running:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

            now = time.time()

            # Check if we need to send a ping
            time_since_last = now - max(self._last_message_time, self._last_ping_sent)
            if time_since_last >= PING_INTERVAL:
                if self._ping_sender:
                    try:
                        await self._ping_sender()
                        self.on_ping_sent()
                        L.debug(f"[Heartbeat] ping sent (idle={time_since_last:.0f}s)")
                    except Exception as e:
                        L.warning(f"[Heartbeat] ping send failed: {e}")
                        self._missed_pongs += 1
                else:
                    L.debug(f"[Heartbeat] would ping but no sender (idle={time_since_last:.0f}s)")

            # Check if we've missed too many pongs
            if self._last_ping_sent > 0:
                time_since_ping = now - self._last_ping_sent
                time_since_pong = now - self._last_pong_time

                # If ping was sent > PONG_TIMEOUT ago AND no pong since then
                if time_since_ping > PONG_TIMEOUT and self._last_pong_time < self._last_ping_sent:
                    self._missed_pongs += 1
                    L.warning(
                        f"[Heartbeat] missed pong #{self._missed_pongs} "
                        f"(ping_sent={time_since_ping:.0f}s ago, "
                        f"last_pong={time_since_pong:.0f}s ago)"
                    )
                    self._last_ping_sent = 0  # Reset so we don't count the same ping twice

            # Trigger reconnect if too many missed pongs
            if self._missed_pongs >= MAX_MISSED_PONGS:
                L.error(
                    f"[Heartbeat] {self._missed_pongs} missed pongs, "
                    f"triggering reconnect"
                )
                self._missed_pongs = 0
                if self._on_reconnect:
                    try:
                        await self._on_reconnect()
                    except Exception as e:
                        L.error(f"[Heartbeat] reconnect callback error: {e}")

    @property
    def last_message_ago(self) -> float:
        return time.time() - self._last_message_time

    @property
    def last_pong_ago(self) -> float:
        return time.time() - self._last_pong_time

    @property
    def missed_pongs(self) -> int:
        return self._missed_pongs
