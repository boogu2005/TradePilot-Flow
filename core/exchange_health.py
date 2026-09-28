"""
Exchange Health Monitor — structured runtime health metrics.

Tracks:
  - WS state, RTT, last message/pong, reconnect count
  - REST latency, error rate, circuit breaker states, queue size
  - Tracker freshness (position count, order count, last update time)

Provides structured health snapshots for logging, monitoring, and alerting.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

L = logger.bind(module="health")


@dataclass
class WsHealth:
    connected: bool = False
    state: str = "DISCONNECTED"
    last_message_ago: float = float("inf")
    last_pong_ago: float = float("inf")
    last_ping_sent_ago: float = float("inf")
    reconnect_count: int = 0
    rtt_ms: float = 0.0
    subscriptions: list[str] = field(default_factory=list)
    ws_url: str = ""


@dataclass
class RestHealth:
    healthy: bool = True
    circuit_breaker_states: dict[str, str] = field(default_factory=dict)
    queue_size: int = 0
    avg_latency_ms: float = 0.0
    failures_last_hour: int = 0
    successes_last_hour: int = 0
    total_requests: int = 0


@dataclass
class TrackerHealth:
    positions_count: int = 0
    orders_count: int = 0
    algo_orders_count: int = 0
    last_ws_update_ago: float = float("inf")
    last_rest_sync_ago: float = float("inf")


@dataclass
class RuntimeHealth:
    state: str = "STOPPED"
    uptime_seconds: float = 0.0
    ws: WsHealth = field(default_factory=WsHealth)
    rest: RestHealth = field(default_factory=RestHealth)
    trackers: TrackerHealth = field(default_factory=TrackerHealth)


class HealthMonitor:
    """
    Collects and reports runtime health metrics.
    Thread-safe (asyncio single-threaded, no locks needed).
    """

    def __init__(self):
        self._start_time: float = 0.0

        # WS
        self._ws_connected: bool = False
        self._ws_state: str = "DISCONNECTED"
        self._ws_last_message: float = 0.0
        self._ws_last_pong: float = 0.0
        self._ws_last_ping_sent: float = 0.0
        self._ws_reconnect_count: int = 0
        self._ws_rtt_samples: list[float] = []
        self._ws_subscriptions: list[str] = []
        self._ws_url: str = ""

        # REST
        self._rest_latency_samples: list[float] = []
        self._rest_failure_timestamps: list[float] = []
        self._rest_success_timestamps: list[float] = []
        self._rest_total: int = 0
        self._rest_queue_size: int = 0
        self._rest_cb_states: dict[str, str] = {}

        # Trackers
        self._positions_count: int = 0
        self._orders_count: int = 0
        self._algo_orders_count: int = 0
        self._last_ws_update: float = 0.0
        self._last_rest_sync: float = 0.0

    def start(self) -> None:
        self._start_time = time.time()
        L.info("[Health] Monitor started")

    # === WS updates ===

    def ws_connected(self, url: str) -> None:
        self._ws_connected = True
        self._ws_url = url

    def ws_disconnected(self) -> None:
        self._ws_connected = False

    def ws_state_change(self, state: str) -> None:
        self._ws_state = state

    def ws_message_received(self) -> None:
        self._ws_last_message = time.time()

    def ws_pong_received(self) -> None:
        now = time.time()
        self._ws_last_pong = now
        if self._ws_last_ping_sent > 0:
            rtt = (now - self._ws_last_ping_sent) * 1000
            self._ws_rtt_samples.append(rtt)
            if len(self._ws_rtt_samples) > 100:
                self._ws_rtt_samples = self._ws_rtt_samples[-50:]

    def ws_ping_sent(self) -> None:
        self._ws_last_ping_sent = time.time()

    def ws_reconnected(self) -> None:
        self._ws_reconnect_count += 1

    def ws_subscriptions_update(self, channels: list[str]) -> None:
        self._ws_subscriptions = list(channels)

    # === REST updates ===

    def rest_request_completed(self, latency_ms: float, success: bool) -> None:
        self._rest_total += 1
        self._rest_latency_samples.append(latency_ms)
        if len(self._rest_latency_samples) > 200:
            self._rest_latency_samples = self._rest_latency_samples[-100:]
        now = time.time()
        if success:
            self._rest_success_timestamps.append(now)
        else:
            self._rest_failure_timestamps.append(now)
        # Prune old entries
        cutoff = now - 3600
        self._rest_success_timestamps = [t for t in self._rest_success_timestamps if t > cutoff]
        self._rest_failure_timestamps = [t for t in self._rest_failure_timestamps if t > cutoff]

    def rest_queue_size(self, size: int) -> None:
        self._rest_queue_size = size

    def rest_circuit_breaker_states(self, states: dict[str, str]) -> None:
        self._rest_cb_states = dict(states)

    # === Tracker updates ===

    def tracker_positions(self, count: int) -> None:
        self._positions_count = count

    def tracker_orders(self, count: int) -> None:
        self._orders_count = count

    def tracker_algo_orders(self, count: int) -> None:
        self._algo_orders_count = count

    def tracker_ws_update(self) -> None:
        self._last_ws_update = time.time()

    def tracker_rest_sync(self) -> None:
        self._last_rest_sync = time.time()

    # === Snapshot ===

    def snapshot(self) -> RuntimeHealth:
        now = time.time()

        avg_rtt = (
            sum(self._ws_rtt_samples) / len(self._ws_rtt_samples)
            if self._ws_rtt_samples else 0.0
        )
        avg_latency = (
            sum(self._rest_latency_samples) / len(self._rest_latency_samples)
            if self._rest_latency_samples else 0.0
        )

        return RuntimeHealth(
            state=self._ws_state,
            uptime_seconds=now - self._start_time if self._start_time > 0 else 0.0,
            ws=WsHealth(
                connected=self._ws_connected,
                state=self._ws_state,
                last_message_ago=now - self._ws_last_message if self._ws_last_message > 0 else float("inf"),
                last_pong_ago=now - self._ws_last_pong if self._ws_last_pong > 0 else float("inf"),
                last_ping_sent_ago=now - self._ws_last_ping_sent if self._ws_last_ping_sent > 0 else float("inf"),
                reconnect_count=self._ws_reconnect_count,
                rtt_ms=round(avg_rtt, 1),
                subscriptions=self._ws_subscriptions,
                ws_url=self._ws_url,
            ),
            rest=RestHealth(
                healthy=len(self._rest_failure_timestamps) < max(len(self._rest_success_timestamps), 1),
                circuit_breaker_states=self._rest_cb_states,
                queue_size=self._rest_queue_size,
                avg_latency_ms=round(avg_latency, 1),
                failures_last_hour=len(self._rest_failure_timestamps),
                successes_last_hour=len(self._rest_success_timestamps),
                total_requests=self._rest_total,
            ),
            trackers=TrackerHealth(
                positions_count=self._positions_count,
                orders_count=self._orders_count,
                algo_orders_count=self._algo_orders_count,
                last_ws_update_ago=round(now - self._last_ws_update, 1) if self._last_ws_update > 0 else float("inf"),
                last_rest_sync_ago=round(now - self._last_rest_sync, 1) if self._last_rest_sync > 0 else float("inf"),
            ),
        )

    def log_health(self) -> None:
        """Log a one-line health summary."""
        h = self.snapshot()
        L.info(
            f"[Health] state={h.state} "
            f"ws={'OK' if h.ws.connected else 'DOWN'} "
            f"rtt={h.ws.rtt_ms}ms "
            f"reconn={h.ws.reconnect_count} "
            f"rest={'OK' if h.rest.healthy else 'DEGRADED'} "
            f"pos={h.trackers.positions_count} "
            f"orders={h.trackers.orders_count} "
            f"algo={h.trackers.algo_orders_count} "
            f"ws_age={h.trackers.last_ws_update_ago}s"
        )


# Global singleton
health_monitor = HealthMonitor()


# ======================================================================
# Watchdog — Pure Health Monitor (Principle 5)
# ======================================================================

class WatchdogMonitor:
    """
    Health Monitor — reports Runtime status. NEVER acts.

    Watchdog ONLY:
      - Prints Runtime state
      - Prints current WS state
      - Prints last message time
      - Prints last pong time
      - Prints reconnect count
      - Prints tracker sync status
      - Prints repair queue length
      - Prints circuit breaker state

    Watchdog NEVER:
      - Calls REST
      - Triggers sync
      - Triggers repair
      - Triggers reconnect
    """

    def __init__(self, interval: float = 30.0):
        self._interval = interval

    async def run(self, shutdown_event) -> None:
        """Main watchdog loop — reports health every `interval` seconds."""
        import asyncio
        L.info(f"[Watchdog] Started (interval={self._interval}s, report-only mode)")

        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

            if shutdown_event.is_set():
                break

            try:
                self._report()
            except Exception as e:
                L.error(f"[Watchdog] Report error: {e}")

        L.info("[Watchdog] Stopped")

    def _report(self) -> None:
        """Print a structured health report."""
        h = health_monitor.snapshot()

        # Get repair queue size (lazy import to avoid circular deps)
        try:
            from .repair_queue import repair_queue
            repair_q_size = repair_queue.size
        except Exception:
            repair_q_size = "N/A"

        # Get circuit breaker state
        try:
            from .circuit_breaker import circuit_breaker
            cb_state = circuit_breaker.state("okx")
        except Exception:
            cb_state = "N/A"

        L.info(
            f"[Watchdog] "
            f"WS={h.ws.state} "
            f"last_msg={_fmt_age(h.ws.last_message_ago)} "
            f"last_pong={_fmt_age(h.ws.last_pong_ago)} "
            f"rtt={h.ws.rtt_ms}ms "
            f"reconn={h.ws.reconnect_count} "
            f"pos={h.trackers.positions_count} "
            f"ord={h.trackers.orders_count} "
            f"algo={h.trackers.algo_orders_count} "
            f"ws_age={_fmt_age(h.trackers.last_ws_update_ago)} "
            f"rest_q={h.rest.queue_size} "
            f"rest_fail={h.rest.failures_last_hour} "
            f"repair_q={repair_q_size} "
            f"cb={cb_state}"
        )


def _fmt_age(age: float) -> str:
    """Format an age value for logging."""
    if age == float("inf") or age is None:
        return "never"
    if age < 0:
        return "0s"
    if age < 60:
        return f"{age:.0f}s"
    if age < 3600:
        return f"{age / 60:.0f}m"
    return f"{age / 3600:.1f}h"
