"""Explicit callback adapters. No shell, process names or arbitrary command dispatch."""
from .execution import ExecutionHandler

ALLOWED = frozenset({"restart_telegram_consumer", "restart_websocket_worker", "restart_exit_manager",
                     "refresh_exchange_snapshot", "trigger_reconciliation", "reconnect_websocket"})


def recovery_handler(action, *, recover, fingerprint, safe, health, reconcile):
    if action not in ALLOWED:
        raise ValueError("recovery action is not whitelisted")

    async def execute(plan):
        await recover()
        return {"recovery_requested": True}

    async def verify(plan):
        checks = await health()
        required = ("alive", "authenticated", "subscribed", "snapshot_fresh", "exit_healthy")
        if not all(checks.get(key) is True for key in required):
            return {"status": "unknown", "health": checks}
        reconciled = await reconcile()
        return {"status": "satisfied" if reconciled is True else "unknown", "health": checks,
                "reconciled": reconciled is True}

    return ExecutionHandler(action, safe, fingerprint, execute, verify)
