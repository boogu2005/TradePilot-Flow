"""Live callbacks, registered by main; all mutations require reviewed plans."""
from __future__ import annotations

import asyncio
import time

from database.models import Trade

from .approval import ApprovalService
from .domain import stable_digest, utcnow
from .execution import ControlledExecutor, ExecutionHandler
from .telemetry import observe


class RuntimeControl:
    def __init__(self, tasks, listener, heartbeat, shutdown, sessions, exchange, runtime):
        self.tasks, self.listener, self.heartbeat = tasks, listener, heartbeat
        self.shutdown, self.sessions, self.exchange, self.runtime = shutdown, sessions, exchange, runtime
        self.snapshot = None
        self.snapshot_at = 0.0

    def _local(self):
        with self.sessions() as session:
            return [{"symbol": self.exchange._to_ccxt_symbol(t.pair, t.exchange or "okx"),
                     "side": "short" if t.is_short else "long", "contracts": float(t.amount or 0),
                     "stop_loss": float(t.stop_loss or 0), "sl_reference": t.sl_algo_id}
                    for t in Trade.get_active_trades(session) if (t.amount or 0) > 0]

    async def fresh(self):
        client = self.exchange.get_exchange("okx")
        results = await asyncio.wait_for(asyncio.gather(
            client.fetch_positions(symbols=None, params={"instType": "SWAP"}),
            client.fetch_open_orders(limit=100),
            *[client.fetch_open_orders(limit=100, params={"trigger": True, "ordType": kind})
              for kind in ("conditional", "oco", "trigger", "move_order_stop", "iceberg", "twap")],
        ), 20)
        if any(not isinstance(rows, list) for rows in results):
            raise TypeError("invalid exchange response")
        if any(len(rows) >= 100 for rows in results[1:]):
            raise ValueError("order snapshot may be incomplete; manual pagination required")
        positions, orders = results[0], [order for rows in results[1:] for order in rows]
        local = await asyncio.to_thread(self._local)
        self.snapshot = {"positions": positions, "orders": orders, "local": local}
        self.snapshot_at = time.monotonic()
        observe("rest", "NORMAL")
        return self.snapshot

    async def reconcile_readonly(self):
        data = await self.fresh()
        def quantities(rows):
            result: dict[tuple[str, str], float] = {}
            for row in rows:
                quantity = float(row.get("contracts") or 0)
                if quantity > 0:
                    key = (row["symbol"], row["side"])
                    result[key] = result.get(key, 0.) + quantity
            return result
        expected, actual = quantities(data["local"]), quantities(data["positions"])
        matched = expected.keys() == actual.keys() and all(abs(value - actual[key]) <= 1e-8 for key, value in expected.items())
        observe("state_match", "NORMAL" if matched else "UNKNOWN", local=expected.__len__(), exchange=actual.__len__())
        return matched

    async def fingerprint(self, plan):
        data = await self.fresh()
        positions = sorted((p["symbol"], p["side"], str(p.get("contracts"))) for p in data["positions"] if float(p.get("contracts") or 0))
        orders = sorted((str(o["id"]), stable_digest({key: o.get(key) for key in
            ("symbol", "status", "filled", "amount", "price", "side", "type", "triggerPrice", "stopLossPrice", "takeProfitPrice", "info")})) for o in data["orders"])
        return stable_digest({"positions": positions, "orders": orders, "local": sorted(data["local"], key=lambda row: (row["symbol"], row["side"]))})

    async def safe(self, plan):
        if plan.target.type != "runtime" or plan.target.id != "okx":
            return False, "unsupported recovery target"
        if not plan.evidence_ids or not plan.preconditions or not plan.verification or not plan.expected_result:
            return False, "recovery plan is missing evidence or business conditions"
        if self.shutdown.is_set() or plan.parameters:
            return False, "shutdown or unexpected parameters"
        data = await self.fresh()
        if plan.action.startswith("restart_"):
            empty = not data["orders"] and not data["local"] and not any(float(p.get("contracts") or 0) for p in data["positions"])
            return empty, "worker restart requires no active orders or exposure"
        return True, "fresh snapshot available; reviewed action"

    async def recover(self, action):
        if action in ("restart_websocket_worker", "reconnect_websocket"):
            await self.runtime.reconnect_transport()
        elif action in ("restart_telegram_consumer", "restart_exit_manager"):
            name = "信号消费者" if action == "restart_telegram_consumer" else "订单监控器"
            await self.tasks.restart_registered(name)
        elif action == "refresh_exchange_snapshot":
            data = await self.fresh()
            if self.runtime.position_tracker is None:
                raise RuntimeError("position tracker unavailable")
            self.runtime.position_tracker.update_from_rest(data["positions"])
        elif action == "trigger_reconciliation":
            # The legacy reconciler can cancel/create orders: never auto-authorized.
            from core.reconciler import reconcile
            with self.sessions() as session:
                await reconcile(session)
        else:
            raise ValueError("unregistered recovery")
        return {"requested": action}

    async def verify(self, plan):
        matched = await self.reconcile_readonly()
        health = self.runtime.get_health()
        ws = health.get("ws", {})
        connected = ws.get("connected") is True and ws.get("state") == "RUNNING" and bool(ws.get("subscriptions"))
        workers = self.tasks.is_running("订单监控器") and self.tasks.is_running("信号消费者")
        consumer_age = time.time() - self.heartbeat.get("consumer_last_tick", 0)
        monitor_age = time.time() - self.heartbeat.get("monitor_last_tick", 0)
        data = self.snapshot or {"orders": [], "local": []}
        order_ids = {str(order.get("id")) for order in data["orders"]}
        protected = all(row["stop_loss"] <= 0 or str(row["sl_reference"]) in order_ids for row in data["local"])
        healthy = matched and protected and connected and workers and consumer_age < 120 and monitor_age < 120
        return {"status": "satisfied" if healthy else "unknown", "matched": matched,
                "ws_authenticated_subscribed": connected, "workers_running": workers, "protection_present": protected,
                "heartbeats_fresh": consumer_age < 120 and monitor_age < 120, "observed_at": utcnow().isoformat()}

    def executor(self, repository):
        from .remediation import ALLOWED
        def handler(action):
            async def execute(_):
                return await self.recover(action)
            return ExecutionHandler(action, self.safe, self.fingerprint, execute, self.verify, timeout_seconds=45)
        return ControlledExecutor(repository, ApprovalService(repository), [handler(action) for action in sorted(ALLOWED)])
