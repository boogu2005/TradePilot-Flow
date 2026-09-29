"""One submission, bounded read-only recovery after an ambiguous transport failure."""
from __future__ import annotations

import asyncio

import ccxt


class OrderOutcomeUnknown(RuntimeError):
    def __init__(self, client_order_id: str):
        super().__init__(f"Order outcome unknown; reconcile client order {client_order_id} before any resubmission")
        self.client_order_id = client_order_id


async def submit_and_confirm(client, symbol, order_type, side, amount, price, params,
                             *, exchange="okx", trigger=False):
    client_id = str(params.get("clOrdId") or params.get("clientOrderId") or "")
    if not client_id:
        raise ValueError("stable client order id required before submission")
    try:
        return await client.create_order(symbol, order_type, side, amount, price, params)
    except (ccxt.NetworkError, asyncio.TimeoutError) as exc:
        from diagnostics.domain import Evidence, IncidentInput, utcnow
        from diagnostics.service import report_nowait
        evidence = [Evidence("exchange_transport", "submission_unknown", {"error_type": type(exc).__name__,
                    "client_order_id": client_id, "symbol": symbol, "exchange": exchange, "trigger": trigger})]
        steps = ["single_submission"]
        for attempt in range(2):
            steps.append(f"query_client_order_{attempt + 1}")
            try:
                order = await asyncio.wait_for(client.fetch_order(client_id, symbol,
                    params={"clOrdId": client_id, "trigger": trigger}), timeout=5)
                if isinstance(order, dict) and order.get("id") and order.get("status"):
                    return order
                evidence.append(Evidence("exchange_rest", "unknown", {"attempt": attempt + 1}))
            except Exception as query_error:  # noqa: BLE001 - absence after timeout is not permission to retry
                evidence.append(Evidence("exchange_rest", "not_found" if isinstance(query_error, ccxt.OrderNotFound) else "query_failed",
                                        {"error_type": type(query_error).__name__, "attempt": attempt + 1}))
            if attempt == 0:
                await asyncio.sleep(.25)
        report_nowait(IncidentInput(f"submission:{exchange}:{client_id}", "order_submission_unknown", "order",
                                   client_id, utcnow(), steps, evidence))
        raise OrderOutcomeUnknown(client_id) from exc
