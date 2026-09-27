"""
WebSocket Connection Manager — singleton WS lifecycle.

Orchestrates:
  - WsStateMachine (state tracking)
  - WsHeartbeat (ping/pong)
  - ReconnectManager (backoff)
  - SubscriptionManager (channel subscriptions)
  - EventBus (data distribution)

This is the ONLY module that directly manages a WebSocket connection.
No other module may create or manage WS connections.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Optional, Any

import websockets
from loguru import logger

from .ws_state_machine import WsStateMachine, WsState
from .ws_heartbeat import WsHeartbeat
from .reconnect_manager import ReconnectManager
from .subscription_manager import SubscriptionManager
from .runtime_events import event_bus
from .exchange_health import health_monitor

L = logger.bind(module="ws_conn")

OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/private"
OKX_WS_PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"


class WsConnection:
    """
    Singleton WebSocket connection manager.

    Usage:
        conn = WsConnection()
        conn.set_credentials(api_key, api_secret, passphrase)
        conn.subscription_manager.add("positions", inst_type="SWAP")
        conn.subscription_manager.add("orders", inst_type="SWAP")
        conn.subscription_manager.add("account", inst_type="SWAP")
        conn.subscription_manager.add("balance_and_position", inst_type="SWAP")
        # NOTE: algo-orders channel does NOT support instType=SWAP.
        # Algo orders (SL/TP) are managed via REST API instead.

        await conn.start()
        # ... WS is connected, authenticated, subscribed, running ...
        await conn.stop()
    """

    MAX_RECONNECT_ATTEMPTS = 10  # Before entering FAILED state

    def __init__(self, exchange_name: str = "okx"):
        self._exchange_name = exchange_name

        # Components
        self.state_machine = WsStateMachine(exchange_name)
        self.heartbeat: Optional[WsHeartbeat] = None
        self.reconnect_mgr = ReconnectManager(base_delay=3.0, max_delay=30.0, jitter_pct=0.25)
        self.subscription_manager = SubscriptionManager(exchange_name)

        # WS objects
        self._ws: Any = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running: bool = False

        # Credentials
        self._api_key: str = ""
        self._api_secret: str = ""
        self._passphrase: str = ""

        # Public WS (for tickers)
        self._public_ws: Any = None
        self._public_ws_task: Optional[asyncio.Task] = None

    def set_credentials(self, api_key: str, api_secret: str, passphrase: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase

    # ======================================================================
    # Lifecycle
    # ======================================================================

    async def start(self) -> None:
        """Start the WS connection lifecycle."""
        self._running = True

        # Set up heartbeat
        self.heartbeat = WsHeartbeat(
            on_reconnect_needed=self._on_heartbeat_lost,
            ping_sender=self._send_ping,
            exchange_name=self._exchange_name,
        )

        # Set up subscription sender
        self.subscription_manager.set_send_fn(self._send_json)

        # Start main loop
        self._ws_task = asyncio.create_task(
            self._main_loop(), name=f"ws_{self._exchange_name}"
        )
        L.info(f"[{self._exchange_name}] WS Connection Manager started")

    async def stop(self) -> None:
        """Stop the WS connection."""
        self._running = False

        if self.heartbeat:
            await self.heartbeat.stop()

        # Cancel tasks
        for task in [self._ws_task, self._public_ws_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

        # Close WS
        await self._close_ws()

        self.state_machine.transition(WsState.DISCONNECTED, "stop() called")
        L.info(f"[{self._exchange_name}] WS Connection Manager stopped")

    # ======================================================================
    # Main Loop (State Machine-Driven)
    # ======================================================================

    async def _main_loop(self) -> None:
        """State machine-driven WS lifecycle. Replaces while-true try/except."""
        while self._running:
            state = self.state_machine.state

            if state == WsState.DISCONNECTED:
                self.state_machine.transition(WsState.CONNECTING, "startup")
                continue

            elif state == WsState.CONNECTING:
                ok = await self._do_connect()
                if ok:
                    self.state_machine.transition(WsState.LOGIN, "connected")
                else:
                    self.state_machine.transition(WsState.DISCONNECTED, "connect failed")
                    await self._sleep(2)
                continue

            elif state == WsState.LOGIN:
                ok = await self._do_login()
                if ok:
                    self.state_machine.transition(WsState.SUBSCRIBING, "login success")
                else:
                    self.state_machine.transition(WsState.DISCONNECTED, "login failed")
                    await self._sleep(3)
                continue

            elif state == WsState.SUBSCRIBING:
                self.subscription_manager.mark_all_pending()
                ok = await self.subscription_manager.subscribe_all()

                if ok:
                    self.state_machine.transition(WsState.SYNCING, "subscribed, waiting for snapshot")
                else:
                    self.state_machine.transition(WsState.DISCONNECTED, "subscribe failed")
                continue

            elif state == WsState.SYNCING:
                # Wait for all trackers to receive initial WS snapshot data
                ok = await self._do_sync()
                if ok:
                    self.state_machine.transition(WsState.RUNNING, "sync complete, all trackers fresh")
                    self.reconnect_mgr.reset()
                    self.heartbeat.start()
                    health_monitor.ws_state_change("RUNNING")
                    health_monitor.ws_subscriptions_update(
                        self.subscription_manager.get_channel_names()
                    )
                    await event_bus.publish("ws_state_change", {
                        "old_state": "SYNCING",
                        "new_state": "RUNNING",
                        "reason": "all trackers synced",
                    })
                else:
                    L.warning(f"[{self._exchange_name}] SYNCING timeout — proceeding to RUNNING anyway")
                    self.state_machine.transition(WsState.RUNNING, "sync timeout, proceeding")
                    self.reconnect_mgr.reset()
                    self.heartbeat.start()
                    health_monitor.ws_state_change("RUNNING")
                continue

            elif state == WsState.RUNNING:
                # Read messages until disconnect
                await self._read_loop()
                # _read_loop returns on disconnect
                if self._running:
                    self.state_machine.transition(WsState.RECONNECTING, "connection lost")
                continue

            elif state == WsState.RECONNECTING:
                await self.heartbeat.stop()
                health_monitor.ws_disconnected()
                await self._close_ws()

                self.reconnect_mgr.next_delay()
                if self.reconnect_mgr.attempt > self.MAX_RECONNECT_ATTEMPTS:
                    L.error(
                        f"[{self._exchange_name}] Max reconnect attempts "
                        f"({self.MAX_RECONNECT_ATTEMPTS}) exceeded → FAILED"
                    )
                    self.state_machine.transition(WsState.FAILED, "max retries")
                    await event_bus.publish("runtime_error", {
                        "error": "ws_max_reconnects",
                        "module": "ws_connection",
                    })
                    continue

                delay = self.reconnect_mgr.last_delay
                health_monitor.ws_state_change("RECONNECTING")
                L.info(f"[{self._exchange_name}] Reconnecting in {delay:.1f}s...")
                await self._sleep(delay)
                if self._running:
                    self.state_machine.transition(WsState.CONNECTING, "reconnect")
                    health_monitor.ws_reconnected()
                continue

            elif state == WsState.FAILED:
                # Wait and retry with long backoff
                L.error(f"[{self._exchange_name}] WS FAILED — retrying in 60s")
                await self._sleep(60)
                if self._running:
                    self.reconnect_mgr.reset()
                    self.state_machine.transition(WsState.CONNECTING, "retry from FAILED")
                continue

    # ======================================================================
    # Connection & Authentication
    # ======================================================================

    async def _do_connect(self) -> bool:
        """Establish WebSocket TCP connection."""
        try:
            await self._close_ws()
            self._ws = await websockets.connect(
                OKX_WS_URL,
                ping_interval=None,  # We handle ping ourselves
                ping_timeout=None,
                max_size=2 ** 20,
                close_timeout=5,
            )
            health_monitor.ws_connected(OKX_WS_URL)
            L.info(f"[{self._exchange_name}] WS TCP connected")
            return True
        except Exception as e:
            L.warning(f"[{self._exchange_name}] WS connect failed: {type(e).__name__}: {e}")
            return False

    async def _do_login(self) -> bool:
        """Send login and verify response."""
        timestamp = str(int(time.time()))
        sign = self._sign(timestamp)
        login_msg = {
            "op": "login",
            "args": [{
                "apiKey": self._api_key,
                "passphrase": self._passphrase,
                "timestamp": timestamp,
                "sign": sign,
            }],
        }
        try:
            await self._send_json(login_msg)
            resp = await asyncio.wait_for(self._ws.recv(), timeout=10)
            data = json.loads(resp)
            if data.get("event") == "login":
                if data.get("code") == "0":
                    L.success(f"[{self._exchange_name}] WS login success")
                    return True
                else:
                    L.error(f"[{self._exchange_name}] WS login failed: {data.get('msg', 'unknown')}")
                    return False
            else:
                L.error(f"[{self._exchange_name}] WS login unexpected response: {resp[:200]}")
                return False
        except asyncio.TimeoutError:
            L.error(f"[{self._exchange_name}] WS login timeout")
            return False
        except Exception as e:
            L.error(f"[{self._exchange_name}] WS login error: {type(e).__name__}: {e}")
            return False

    def _sign(self, timestamp: str) -> str:
        message = f"{timestamp}GET/users/self/verify"
        mac = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    # ======================================================================
    # Syncing — wait for initial WS snapshot data
    # ======================================================================

    SYNC_TIMEOUT = 30.0  # Max seconds to wait for tracker snapshots
    SYNC_POLL_INTERVAL = 0.5  # Check every 500ms

    async def _do_sync(self) -> bool:
        """
        Wait for all trackers to receive their initial WS snapshot.
        Returns True if all trackers are fresh within SYNC_TIMEOUT,
        False if timeout elapsed (proceed to RUNNING anyway).
        """
        from .position_tracker import position_tracker
        from .order_tracker import order_tracker
        from .balance_tracker import balance_tracker
        from .algo_order_tracker import algo_order_tracker

        deadline = time.time() + self.SYNC_TIMEOUT
        L.info(f"[{self._exchange_name}] SYNCING: waiting for tracker snapshots...")

        while time.time() < deadline:
            pos_fresh = position_tracker.is_fresh if position_tracker else False
            bal_fresh = balance_tracker.is_fresh if balance_tracker else False
            # Orders and algos may be empty initially (no open orders) — only require position + balance
            ord_fresh = order_tracker.is_fresh if order_tracker else False
            algo_fresh = algo_order_tracker.is_fresh if algo_order_tracker else False

            if pos_fresh and bal_fresh:
                L.info(
                    f"[{self._exchange_name}] SYNCING complete: "
                    f"pos_fresh={pos_fresh} bal_fresh={bal_fresh} "
                    f"ord_fresh={ord_fresh} algo_fresh={algo_fresh}"
                )
                return True

            await self._sleep(self.SYNC_POLL_INTERVAL)

        L.warning(
            f"[{self._exchange_name}] SYNCING timeout after {self.SYNC_TIMEOUT}s: "
            f"pos_fresh={position_tracker.is_fresh} bal_fresh={balance_tracker.is_fresh}"
        )
        return False

    # ======================================================================
    # Message Reading
    # ======================================================================

    async def _read_loop(self) -> None:
        """Read messages from WS. Returns on disconnect."""
        try:
            async for msg in self._ws:
                await self._on_message(msg)
        except websockets.ConnectionClosed as e:
            L.warning(f"[{self._exchange_name}] WS closed: code={e.code}")
        except (OSError, asyncio.TimeoutError) as e:
            L.warning(f"[{self._exchange_name}] WS network error: {type(e).__name__}: {e}")
        except Exception as e:
            L.error(f"[{self._exchange_name}] WS read error: {type(e).__name__}: {e}")

    async def _on_message(self, raw: str | bytes) -> None:
        """Process a single WS message."""
        # Decode bytes
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        # OKX sends plain text "ping" as keepalive — client must reply "pong"
        if isinstance(raw, str) and raw.strip() == "ping":
            try:
                await self._ws.send("pong")
            except Exception:
                pass
            if self.heartbeat:
                self.heartbeat.on_message()
            health_monitor.ws_message_received()
            return

        # Parse JSON
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            L.debug(f"[{self._exchange_name}] Non-JSON message ignored: {str(raw)[:100]}")
            return

        # Heartbeat: track message time
        if self.heartbeat:
            self.heartbeat.on_message()
        health_monitor.ws_message_received()

        event = msg.get("event", "")
        channel = (msg.get("arg", {}) or {}).get("channel", "")

        # Handle events
        if event == "login":
            # Login handled in _do_login, but can also arrive async
            pass

        elif event == "subscribe":
            self.subscription_manager.confirm(channel)

        elif event == "unsubscribe":
            L.info(f"[{self._exchange_name}] Unsubscribed from {channel}")

        elif event == "error":
            code = msg.get("code", "")
            err_msg = msg.get("msg", "")
            L.error(f"[{self._exchange_name}] WS error: code={code} msg={err_msg}")

            if code == "60010" or "login" in err_msg.lower():
                # Login expired, need to re-authenticate
                self.state_machine.transition(WsState.RECONNECTING,
                                              f"login error: {err_msg}")
                return

            await event_bus.publish("ws_state_change", {
                "error": True, "code": code, "msg": err_msg,
            })

        elif event == "notice":
            code = msg.get("code", "")
            if code == "64008":
                # OKX requests a new connection
                L.warning(f"[{self._exchange_name}] Received notice 64008 — proactive reconnect")
                self.state_machine.transition(WsState.RECONNECTING, "notice 64008")
                return
            else:
                L.info(f"[{self._exchange_name}] Notice: code={code} msg={msg.get('msg', '')}")

        elif event == "pong":
            if self.heartbeat:
                self.heartbeat.on_pong()
            health_monitor.ws_pong_received()

        elif event == "" and channel:
            # Data message
            data = msg.get("data", [])
            # NOTE: Do NOT return early on empty data — an empty snapshot
            # (e.g. no open positions) must still mark trackers as fresh,
            # otherwise sync will always time out.

            if channel == "positions":
                await self._handle_position_data(data)
            elif channel == "orders":
                await self._handle_order_data(data)
            elif channel == "account":
                await self._handle_account_data(data)
            elif channel == "balance_and_position":
                await self._handle_balance_position_data(data)
            elif channel == "algo-orders":
                await self._handle_algo_order_data(data)
            elif channel == "fills":
                await self._handle_fill_data(data)

    # ======================================================================
    # Data Handlers → EventBus
    # ======================================================================

    async def _handle_position_data(self, data: list[dict]) -> None:
        if not data:
            # Empty snapshot = no open positions. Mark tracker as fresh
            # so sync doesn't time out waiting for non-existent position data.
            from .position_tracker import position_tracker
            position_tracker.mark_empty_snapshot()
            return
        for p in data:
            try:
                contracts = float(p.get("pos", 0))
                side = (p.get("posSide") or "").lower()
                event_data = {
                    "symbol": p.get("instId", ""),
                    "side": side,
                    "contracts": contracts,
                    "entry_price": float(p.get("avgPx", 0)),
                    "mark_price": float(p.get("markPx", 0)),
                    "unrealized_pnl": float(p.get("upl", 0)),
                    "leverage": int(p.get("lever", 1)),
                    "liquidation_price": p.get("liqPx"),
                    "margin_mode": p.get("mgnMode", "cross"),
                    "update_time": p.get("uTime", ""),
                }
                await event_bus.publish("position_update", event_data)
            except (ValueError, TypeError, KeyError) as e:
                L.debug(f"[{self._exchange_name}] Position parse error: {e}")

    async def _handle_order_data(self, data: list[dict]) -> None:
        for o in data:
            try:
                event_data = {
                    "order_id": o.get("ordId", ""),
                    "algo_id": o.get("algoId", ""),
                    "client_order_id": o.get("clOrdId", ""),
                    "symbol": o.get("instId", ""),
                    "side": o.get("side", ""),
                    "order_type": o.get("ordType", ""),
                    "state": o.get("state", ""),
                    "price": float(o.get("px", 0)),
                    "amount": float(o.get("sz", 0)),
                    "filled": float(o.get("fillSz", 0)),
                    "avg_fill_price": float(o.get("fillPx", 0)),
                    "fee": float(o.get("fee", 0)),
                    "update_time": o.get("uTime", ""),
                    "reduce_only": o.get("reduceOnly", "") == "true",
                }
                await event_bus.publish("order_update", event_data)
            except (ValueError, TypeError, KeyError) as e:
                L.debug(f"[{self._exchange_name}] Order parse error: {e}")

    async def _handle_account_data(self, data: list[dict]) -> None:
        if not data:
            # Empty account snapshot — mark balance tracker as fresh
            from .balance_tracker import balance_tracker
            if not balance_tracker.is_initialized:
                # Don't overwrite if we already have data from another channel
                pass  # balance_and_position is the primary source; account is supplementary
            return
        for a in data:
            try:
                event_data = {
                    "currency": a.get("ccy", "USDT"),
                    "equity": float(a.get("eq", 0)),
                    "balance": float(a.get("totalEq", 0)),
                    "available": float(a.get("availEq", 0)),
                    "margin": float(a.get("isoEq", 0)),
                    "unrealized_pnl": float(a.get("upl", 0)),
                    "update_time": a.get("uTime", ""),
                }
                await event_bus.publish("balance_update", event_data)
            except (ValueError, TypeError, KeyError) as e:
                L.debug(f"[{self._exchange_name}] Account parse error: {e}")

    async def _handle_balance_position_data(self, data: list[dict]) -> None:
        """Combined balance_and_position channel."""
        for entry in data:
            try:
                # Always process balance data
                for bal in entry.get("balData", []):
                    await event_bus.publish("balance_update", {
                        "currency": bal.get("ccy", "USDT"),
                        "equity": float(bal.get("eq", 0)),
                        "balance": float(bal.get("totalEq", 0)),
                        "available": float(bal.get("availEq", 0)),
                        "margin": float(bal.get("isoEq", 0)),
                        "unrealized_pnl": float(bal.get("upl", 0)),
                        "update_time": entry.get("uTime", ""),
                    })

                # Process position data
                pos_data = entry.get("posData", [])
                if pos_data:
                    for pos in pos_data:
                        await event_bus.publish("position_update", {
                            "symbol": pos.get("instId", ""),
                            "side": (pos.get("posSide") or "").lower(),
                            "contracts": float(pos.get("pos", 0)),
                            "entry_price": float(pos.get("avgPx", 0)),
                            "mark_price": float(pos.get("markPx", 0)),
                            "unrealized_pnl": float(pos.get("upl", 0)),
                            "leverage": int(pos.get("lever", 1)),
                            "liquidation_price": pos.get("liqPx"),
                            "margin_mode": pos.get("mgnMode", "cross"),
                            "update_time": entry.get("uTime", ""),
                        })
                else:
                    # Empty posData = no open positions. Mark tracker as fresh
                    # so sync doesn't time out.
                    from .position_tracker import position_tracker
                    position_tracker.mark_empty_snapshot()
            except (ValueError, TypeError, KeyError) as e:
                L.debug(f"[{self._exchange_name}] Balance+Position parse error: {e}")

    async def _handle_algo_order_data(self, data: list[dict]) -> None:
        for a in data:
            try:
                event_data = {
                    "algo_id": a.get("algoId", ""),
                    "client_order_id": a.get("clOrdId", ""),
                    "symbol": a.get("instId", ""),
                    "order_type": a.get("ordType", ""),
                    "side": a.get("side", ""),
                    "state": a.get("state", ""),
                    "amount": float(a.get("sz", 0)),
                    "trigger_price": float(a.get("triggerPx", 0)),
                    "trigger_time": a.get("triggerTime", ""),
                    "update_time": a.get("uTime", ""),
                }
                await event_bus.publish("algo_order_update", event_data)
            except (ValueError, TypeError, KeyError) as e:
                L.debug(f"[{self._exchange_name}] Algo order parse error: {e}")

    async def _handle_fill_data(self, data: list[dict]) -> None:
        for f in data:
            try:
                event_data = {
                    "order_id": f.get("ordId", ""),
                    "trade_id": f.get("tradeId", ""),
                    "symbol": f.get("instId", ""),
                    "side": f.get("side", ""),
                    "price": float(f.get("fillPx", 0)),
                    "amount": float(f.get("fillSz", 0)),
                    "fee": float(f.get("fillFee", 0)),
                    "fee_currency": f.get("fillFeeCcy", "USDT"),
                    "time": f.get("fillTime", ""),
                }
                await event_bus.publish("fill_update", event_data)
            except (ValueError, TypeError, KeyError) as e:
                L.debug(f"[{self._exchange_name}] Fill parse error: {e}")

    # ======================================================================
    # Helpers
    # ======================================================================

    async def _send_json(self, data: dict) -> None:
        """Send JSON to WS."""
        if self._ws:
            await self._ws.send(json.dumps(data))

    async def _send_ping(self) -> None:
        """Send a ping frame. Raises on failure so heartbeat can detect it."""
        if not self._ws:
            raise ConnectionError("WebSocket not connected")
        pong = await self._ws.ping()
        await asyncio.wait_for(pong, timeout=PONG_TIMEOUT)
        if self.heartbeat:
            self.heartbeat.on_pong()

    async def _close_ws(self) -> None:
        """Close current WS connection."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._public_ws:
            try:
                await self._public_ws.close()
            except Exception:
                pass
            self._public_ws = None

    async def _on_heartbeat_lost(self) -> None:
        """Called when heartbeat detects connection loss."""
        L.error(f"[{self._exchange_name}] Heartbeat lost — forcing reconnect")
        if self.state_machine.state == WsState.RUNNING:
            self.state_machine.transition(WsState.RECONNECTING, "heartbeat lost")
            # Force close to break out of _read_loop
            await self._close_ws()

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep."""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            pass


PONG_TIMEOUT = 5  # Used in _send_ping
