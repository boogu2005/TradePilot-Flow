"""图表数据服务：资金曲线、每日盈亏、收益分布。"""
from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta

from loguru import logger
from sqlalchemy import select

from ..database import db
from ..models import bot_models
from .account import get_account_history
from .common import as_utc, round2, to_float, utc_now


def _trades_table():
    return bot_models.get_table("trades")


def get_equity_curve(days: int = 30) -> dict:
    """账户资金曲线。优先快照数据，回退到累计已实现盈亏。"""
    snapshots = get_account_history(days=days)
    if snapshots:
        return {
            "source": snapshots[0]["source"],
            "points": [
                {"time": s["time"], "balance": s["balance"], "equity": s["equity"], "unrealized_pnl": s["unrealized_pnl"]}
                for s in snapshots
            ],
        }

    # 回退：按日累计已实现盈亏
    table = _trades_table()
    daily: OrderedDict[str, dict] = OrderedDict()
    if table is not None:
        session = db.bot_session()
        try:
            from_date = utc_now() - timedelta(days=days)
            rows = session.execute(
                select(table.c.close_date, table.c.realized_profit, table.c.close_profit_abs)
                .where(table.c.is_open.is_(False), table.c.close_date >= from_date)
                .order_by(table.c.close_date)
            ).mappings().all()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"资金曲线回退数据失败: {exc}")
            rows = []
        finally:
            session.close()
        for row in rows:
            day = row.get("close_date")
            if day is None:
                continue
            key = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day)[:10]
            pnl = (
                to_float(row.get("close_profit_abs"))
                if row.get("close_profit_abs") is not None
                else to_float(row.get("realized_profit"))
            )
            if key not in daily:
                daily[key] = {"date": key, "pnl": 0.0}
            daily[key]["pnl"] += pnl

    cumulative = 0.0
    points = []
    for item in daily.values():
        cumulative += item["pnl"]
        points.append({"date": item["date"], "pnl": round2(item["pnl"]), "cumulative": round2(cumulative)})
    return {"source": "trades_fallback", "points": points}


def get_daily_pnl(days: int = 30) -> dict:
    """每日盈亏柱状图数据。"""
    table = _trades_table()
    daily: OrderedDict[str, dict] = OrderedDict()
    if table is not None:
        session = db.bot_session()
        try:
            from_date = utc_now() - timedelta(days=days)
            rows = session.execute(
                select(table.c.close_date, table.c.realized_profit, table.c.close_profit_abs)
                .where(table.c.is_open.is_(False), table.c.close_date >= from_date)
            ).mappings().all()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"每日盈亏数据失败: {exc}")
            rows = []
        finally:
            session.close()
        for row in rows:
            day = row.get("close_date")
            if day is None:
                continue
            key = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day)[:10]
            pnl = (
                to_float(row.get("close_profit_abs"))
                if row.get("close_profit_abs") is not None
                else to_float(row.get("realized_profit"))
            )
            entry = daily.setdefault(
                key, {"date": key, "pnl": 0.0, "wins": 0, "losses": 0, "count": 0}
            )
            entry["pnl"] += pnl
            entry["count"] += 1
            if pnl > 0:
                entry["wins"] += 1
            elif pnl < 0:
                entry["losses"] += 1
    result = list(daily.values())
    for item in result:
        item["pnl"] = round2(item["pnl"])
    return {"days": days, "items": result}


def get_pnl_distribution(bins: int = 10) -> dict:
    """盈亏金额分布（直方图）。"""
    table = _trades_table()
    if table is None:
        return {"bins": [], "counts": []}
    session = db.bot_session()
    try:
        pnls = [
            to_float(r.get("close_profit_abs"))
            if r.get("close_profit_abs") is not None
            else to_float(r.get("realized_profit"))
            for r in session.execute(
                select(table.c.realized_profit, table.c.close_profit_abs).where(
                    table.c.is_open.is_(False)
                )
            ).mappings().all()
        ]
    except Exception:  # noqa: BLE001
        return {"bins": [], "counts": []}
    finally:
        session.close()
    if not pnls:
        return {"bins": [], "counts": []}
    lo, hi = min(pnls), max(pnls)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    width = (hi - lo) / bins
    edges = [lo + width * i for i in range(bins + 1)]
    labels = [round2((edges[i] + edges[i + 1]) / 2) for i in range(bins)]
    counts = [0] * bins
    for p in pnls:
        idx = min(bins - 1, max(0, int((p - lo) / width)))
        counts[idx] += 1
    return {"bins": labels, "counts": counts}
