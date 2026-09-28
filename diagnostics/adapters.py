from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from database.models import Order, Trade
from .domain import ToolResult
from .runbooks import KeywordRunbook
from .tools import DiagnosticTool, ToolRegistry


def _exchange_error(source: str, exc: Exception) -> ToolResult:
    message = str(exc)[:500]
    lowered = message.lower()
    if "does not exist" in lowered or "not found" in lowered or "51400" in lowered:
        return ToolResult.not_found(source, {"message": message})
    return ToolResult.error(source, type(exc).__name__, message, retryable=True)


def build_read_only_tools(session_factory, log_path: Path, runbook_paths: list[Path]) -> ToolRegistry:
    async def query_order(args):
        try:
            from exchange_engine.exchange import get_exchange, _to_ccxt_symbol
            ex = get_exchange(args["exchange"])
            order = await ex.fetch_order(args["order_id"], _to_ccxt_symbol(args["symbol"], args["exchange"]))
            return ToolResult.ok("exchange_rest", {"order": order}) if order else ToolResult.not_found("exchange_rest")
        except Exception as exc:
            return _exchange_error("exchange_rest", exc)

    async def query_fills(args):
        try:
            from exchange_engine.exchange import get_exchange, _to_ccxt_symbol
            ex = get_exchange(args["exchange"])
            rows = await ex.fetch_my_trades(_to_ccxt_symbol(args["symbol"], args["exchange"]), limit=min(args["limit"], 100))
            selected = [row for row in rows if not args["order_id"] or str(row.get("order")) == args["order_id"]]
            return ToolResult.ok("exchange_rest", {"fills": selected[:args["limit"]]})
        except Exception as exc:
            return _exchange_error("exchange_rest", exc)

    async def query_positions(args):
        try:
            from exchange_engine.exchange import fetch_positions
            rows = await fetch_positions(args["symbol"] or None, exchange=args["exchange"])
            return ToolResult.ok("exchange_rest", {"positions": rows[:50], "snapshot_time": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            return _exchange_error("exchange_rest", exc)

    async def query_local(args):
        with session_factory() as session:
            trade = session.get(Trade, args["trade_id"])
            if not trade:
                return ToolResult.not_found("local_database")
            orders = session.query(Order).filter(Order.ft_trade_id == trade.id).limit(100).all()
            return ToolResult.ok("local_database", {"trade": {
                "id": trade.id, "pair": trade.pair, "position_state": trade.position_state,
                "is_open": trade.is_open, "amount": trade.amount, "repair_retry": trade.repair_retry,
            }, "orders": [{"id": row.order_id, "status": row.status, "filled": row.filled, "role": row.ft_order_role} for row in orders]})

    async def query_logs(args):
        if not log_path.exists():
            return ToolResult.not_found("local_log", {"path": str(log_path)})
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]
        matches = [line for line in lines if args["query"].lower() in line.lower()][-min(args["limit"], 200):]
        return ToolResult.ok("local_log", {"path": str(log_path), "lines": matches}) if matches else ToolResult.not_found("local_log", {"path": str(log_path)})

    runbooks = KeywordRunbook(runbook_paths, version="2026-09-28")

    async def query_runbook(args):
        return runbooks.search(args["query"], args["limit"])

    return ToolRegistry([
        DiagnosticTool("query_order", {"order_id": str, "symbol": str, "exchange": str}, query_order, description="Query one exchange order by id."),
        DiagnosticTool("query_fills", {"order_id": str, "symbol": str, "exchange": str, "limit": int}, query_fills, description="Query bounded exchange fills."),
        DiagnosticTool("query_positions", {"symbol": str, "exchange": str}, query_positions, description="Query exchange positions and observation time."),
        DiagnosticTool("query_local_trade", {"trade_id": int}, query_local, description="Read one local trade and its orders."),
        DiagnosticTool("search_logs", {"query": str, "limit": int}, query_logs, description="Search a bounded local log tail."),
        DiagnosticTool("search_runbook", {"query": str, "limit": int}, query_runbook, description="Keyword search versioned operational documentation."),
    ])
