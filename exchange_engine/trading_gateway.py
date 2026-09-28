"""
Trading Gateway — REST API 网关

提供 HTTP API 供 Node.js Project Agent 查询交易状态。
所有请求默认只读。

启动方式：
    python -m exchange_engine.trading_gateway
    或
    python -m uvicorn exchange_engine.trading_gateway:app --host 127.0.0.1 --port 9802

API：
  GET  /health               — 健康检查
  GET  /api/v1/positions     — 持仓查询
  GET  /api/v1/orders        — 订单查询
  GET  /api/v1/signals       — 信号查询
  GET  /api/v1/risk          — 风控状态
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Optional

try:
    from fastapi import FastAPI, Query
    import uvicorn
except ImportError:
    # Fallback: 无 FastAPI 时的简单 HTTP 服务器
    FastAPI = None

app = FastAPI(title="Trading Gateway", version="1.0.0") if FastAPI else None

# ---- 数据库导入（延迟加载，避免启动时依赖问题） ----
_db_initialized: bool = False

# 项目根目录（trading_gateway.py 位于 exchange_engine/ 下，根目录为其父目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_session():
    """每次调用创建新的数据库会话。"""
    global _db_initialized
    if not _db_initialized:
        try:
            sys.path.insert(0, _PROJECT_ROOT)
            from database.db import init_db
            init_db()
            _db_initialized = True
        except Exception as e:
            return None
    try:
        from database.db import get_session
        return get_session()
    except Exception:
        return None


# ======================================================================
# Health
# ======================================================================

@app.get("/health")
async def health():
    """健康检查。"""
    return {
        "ok": True,
        "service": "trading-gateway",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ======================================================================
# Positions
# ======================================================================

@app.get("/api/v1/positions")
async def get_positions(symbol: Optional[str] = Query(None)):
    """查询当前持仓。"""
    session = _get_session()
    if session is None:
        return {"positions": [], "note": "数据库不可用"}

    try:
        from database.models import Trade

        # 查所有活跃 trade
        trades = Trade.get_active_trades(session)
        result = []
        for t in trades:
            if not t.is_open or t.amount <= 0:
                continue
            if symbol and symbol.upper() not in t.pair.upper():
                continue

            # 获取当前价格（从交易所）
            current_price = None
            pnl_pct = None
            try:
                from exchange_engine import exchange as ex
                ticker = await ex.fetch_ticker(t.pair, exchange=t.exchange or "okx")
                current_price = ticker["last"]
                pnl_pct = round(t.calc_profit_ratio(current_price) * 100, 2)
            except Exception:
                pass

            result.append({
                "id": t.id,
                "exchange": t.exchange,
                "pair": t.pair,
                "direction": "SHORT" if t.is_short else "LONG",
                "amount": t.amount,
                "entry_price": t.open_rate,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "leverage": t.leverage,
                "stop_loss": t.stop_loss,
                "open_date": t.open_date.isoformat() if t.open_date else None,
                "exit_mode": t.exit_mode,
                "position_state": t.position_state,
            })

        session.close()
        return {"positions": result, "total": len(result)}

    except Exception as e:
        if session:
            session.rollback()
            session.close()
        return {"positions": [], "error": str(e)}


# ======================================================================
# Orders
# ======================================================================

@app.get("/api/v1/orders")
async def get_orders(
    symbol: Optional[str] = Query(None),
    order_id: Optional[str] = Query(None),
    limit: int = Query(20, le=100),
):
    """查询订单记录。"""
    session = _get_session()
    if session is None:
        return {"orders": [], "note": "数据库不可用"}

    try:
        from database.models import Order

        query = session.query(Order)
        if symbol:
            query = query.filter(Order.ft_pair.contains(symbol.upper()))
        if order_id:
            query = query.filter(Order.order_id == order_id)
        query = query.order_by(Order.id.desc()).limit(limit)

        orders = []
        for o in query.all():
            orders.append({
                "id": o.id,
                "trade_id": o.ft_trade_id,
                "pair": o.ft_pair,
                "side": o.ft_order_side,
                "type": o.order_type,
                "price": o.price,
                "amount": o.ft_amount,
                "filled": o.filled,
                "status": o.status,
                "is_open": o.ft_is_open,
                "order_date": o.order_date.isoformat() if o.order_date else None,
            })

        session.close()
        return {"orders": orders, "total": len(orders)}

    except Exception as e:
        if session:
            session.rollback()
            session.close()
        return {"orders": [], "error": str(e)}


# ======================================================================
# Signals
# ======================================================================

@app.get("/api/v1/signals")
async def get_signals(limit: int = Query(10, le=50)):
    """查询最近交易信号。"""
    session = _get_session()
    if session is None:
        return {"signals": [], "note": "数据库不可用"}

    try:
        from database.models import SignalLog

        logs = (
            session.query(SignalLog)
            .filter(SignalLog.is_trading_signal.is_(True))
            .order_by(SignalLog.id.desc())
            .limit(limit)
            .all()
        )

        signals = []
        for log in logs:
            signals.append({
                "id": log.id,
                "signal_id": log.signal_id,
                "group": log.tg_group_title,
                "sender": log.tg_sender_name,
                "pair": log.pair,
                "direction": log.direction,
                "is_trading": log.is_trading_signal,
                "error": log.error,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            })

        session.close()
        return {"signals": signals, "total": len(signals)}

    except Exception as e:
        if session:
            session.rollback()
            session.close()
        return {"signals": [], "error": str(e)}


# ======================================================================
# Risk
# ======================================================================

@app.get("/api/v1/risk")
async def get_risk():
    """查询风控状态。"""
    session = _get_session()
    if session is None:
        return {"status": "unknown", "note": "数据库不可用"}

    try:
        from database.models import Trade
        from datetime import date

        today_str = date.today().isoformat()

        # 活跃持仓数
        active = Trade.get_open_trade_count(session)

        # 今日盈亏
        from sqlalchemy import func, select
        today_pnl = session.execute(
            select(func.coalesce(func.sum(Trade.close_profit_abs), 0.0))
            .where(Trade.is_open.is_(False), Trade.close_date >= today_str)
        ).scalar()

        # 交易所余额
        exchange_balance = None
        try:
            from exchange_engine import exchange as ex
            bal = await ex.fetch_balance()
            exchange_balance = bal.get("total", 0)
        except Exception:
            pass

        # 熔断器状态
        from core.daily_loss_breaker import daily_loss_breaker
        breaker_status = daily_loss_breaker.status()

        session.close()
        return {
            "status": "ok",
            "active_positions": active,
            "today_pnl_usdt": round(today_pnl or 0, 2),
            "exchange_balance_usdt": round(exchange_balance or 0, 2),
            "circuit_breaker": {
                "tripped": breaker_status["tripped"],
                "daily_loss_pct": breaker_status["daily_loss_pct"],
                "max_loss_pct": breaker_status["max_loss_pct"],
                "day_start_equity": breaker_status["day_start_equity"],
                "current_equity": breaker_status["current_equity"],
                "reason": breaker_status["trip_reason"],
            },
        }

    except Exception as e:
        if session:
            session.rollback()
            session.close()
        return {"status": "error", "error": str(e)}


# ======================================================================
# Main
# ======================================================================

def main():
    """启动 Gateway 服务。"""
    port = int(os.getenv("TRADING_GATEWAY_PORT", "9802"))
    host = os.getenv("TRADING_GATEWAY_HOST", "127.0.0.1")

    if FastAPI is None:
        print("[ERROR] Trading Gateway 需要 FastAPI: pip install fastapi uvicorn")
        sys.exit(1)

    print(f"Trading Gateway starting on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
