"""
Startup Recovery — orchestrates the initial data synchronization.

Sequence:
  1. Wait for WS RUNNING state
  2. REST fetch_positions (one-time)
  3. REST fetch_open_orders (one-time)
  4. REST fetch_pending_algo_orders (one-time)
  5. Reconcile WS trackers with REST data
  6. Sync to database
  7. Fire runtime_ready event

After this, REST is ONLY used for writes. All reads come from WS trackers.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from .runtime_events import event_bus
from .exchange_health import health_monitor

L = logger.bind(module="startup")


async def startup_recover(
    session_factory,
    ws_connection,
    position_tracker,
    order_tracker,
    balance_tracker,
    algo_order_tracker,
    exchange_name: str = "okx",
    timeout: float = 60.0,
) -> dict:
    """
    Orchestrate startup recovery.

    1. Wait for WS RUNNING (with timeout)
    2. REST fetch all data (one-time supplement)
    3. Reconcile WS vs REST
    4. Sync to DB
    5. Fire ready event

    Returns: {"positions": int, "orders": int, "algo_orders": int, "balance_ok": bool}
    """
    L.info(f"[Startup] Beginning recovery for {exchange_name}...")

    # Step 1: Wait for WS to be RUNNING
    L.info("[Startup] Step 1/5: Waiting for WS RUNNING...")
    deadline = asyncio.get_event_loop().time() + timeout
    while ws_connection.state_machine.state.value != "RUNNING":
        if asyncio.get_event_loop().time() > deadline:
            L.warning(f"[Startup] WS not RUNNING after {timeout}s, proceeding with REST only")
            break
        await asyncio.sleep(0.5)

    L.info(f"[Startup] WS state: {ws_connection.state_machine.state.value}")

    # Step 2: REST fetch positions
    L.info("[Startup] Step 2/5: REST fetch positions...")
    try:
        from exchange_engine.exchange import fetch_positions
        positions = await fetch_positions(exchange=exchange_name)
        if positions:
            position_tracker.update_from_rest(positions)
            L.info(f"[Startup] REST positions: {len(positions)}")
        else:
            L.info("[Startup] REST positions: empty")
    except Exception as e:
        L.warning(f"[Startup] REST positions failed: {e}")
        positions = []

    # Step 3: REST fetch open orders
    L.info("[Startup] Step 3/5: REST fetch open orders...")
    try:
        from exchange_engine.exchange import fetch_open_orders
        open_orders = await fetch_open_orders(exchange=exchange_name)
        if open_orders:
            order_tracker.update_from_rest(open_orders)
            L.info(f"[Startup] REST open orders: {len(open_orders)}")
        else:
            L.info("[Startup] REST open orders: empty")
    except Exception as e:
        L.warning(f"[Startup] REST open orders failed: {e}")
        open_orders = []

    # Step 4: REST fetch algo orders
    L.info("[Startup] Step 4/5: REST fetch algo orders...")
    algo_orders = []
    try:
        from exchange_engine.exchange import get_exchange
        ex = get_exchange(exchange_name)
        # Get all symbols that have positions
        symbols = set()
        for pos in position_tracker.get_positions():
            sym = pos.get("symbol", "")
            if sym:
                symbols.add(sym)

        for sym in symbols:
            try:
                resp = await ex.privateGetTradeOrdersAlgoPending({
                    "instType": "SWAP",
                    "state": "live",
                })
                data = resp.get("data", []) if isinstance(resp, dict) else []
                if data:
                    # Filter by symbol if symbol-specific response
                    symbol_data = [d for d in data if d.get("instId") == sym] if symbols else data
                    algo_orders.extend(symbol_data or data)
            except Exception as e:
                L.debug(f"[Startup] REST algo for {sym}: {e}")

        if algo_orders:
            algo_order_tracker.update_from_rest(algo_orders)
            L.info(f"[Startup] REST algo orders: {len(algo_orders)}")
        else:
            L.info("[Startup] REST algo orders: empty")
    except Exception as e:
        L.warning(f"[Startup] REST algo orders failed: {e}")

    # Step 5: REST fetch balance
    L.info("[Startup] Step 5/5: REST fetch balance...")
    balance_ok = False
    try:
        from exchange_engine.exchange import fetch_balance
        bal = await fetch_balance(exchange=exchange_name)
        if bal:
            balance_tracker.update_from_rest(bal)
            L.info(f"[Startup] Balance: free={bal.get('free', 0):.0f}")
            balance_ok = True
    except Exception as e:
        L.warning(f"[Startup] REST balance failed: {e}")

    # Fire ready event
    result = {
        "positions": len(positions),
        "orders": len(open_orders),
        "algo_orders": len(algo_orders),
        "balance_ok": balance_ok,
    }

    health_monitor.start()
    await event_bus.publish("runtime_ready", result)

    L.success(f"[Startup] Recovery complete: {result}")
    return result
