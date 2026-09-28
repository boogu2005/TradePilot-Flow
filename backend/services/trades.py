"""历史交易查询服务（分页 + 搜索）。"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, select

from ..database import db
from ..models import bot_models
from .common import as_utc, round2, safe_div, to_float, to_int


def _trades_table():
    return bot_models.get_table("trades")


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _hold_seconds(open_dt, close_dt) -> int | None:
    if open_dt is None or close_dt is None:
        return None
    diff = close_dt - open_dt
    return max(0, int(diff.total_seconds()))


def list_trades(
    teacher: str | None = None,
    pair: str | None = None,
    start: str | None = None,
    end: str | None = None,
    direction: str | None = None,
    result: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """返回 {items, total, page, page_size}。"""
    table = _trades_table()
    empty = {"items": [], "total": 0, "page": page, "page_size": page_size}
    if table is None:
        return empty

    teacher_col = bot_models.teacher_column()
    conditions = []
    if teacher:
        if teacher_col:
            conditions.append(table.c[teacher_col].ilike(f"%{teacher}%"))
        else:
            return empty
    if pair:
        conditions.append(table.c.pair.ilike(f"%{pair}%"))
    if direction:
        direction = direction.strip().upper()
        if direction in ("LONG", "BUY"):
            conditions.append(table.c.is_short.is_(False))
        elif direction in ("SHORT", "SELL"):
            conditions.append(table.c.is_short.is_(True))
    if result:
        result = result.strip().lower()
        if result in ("win", "profit"):
            conditions.append(table.c.realized_profit > 0)
        elif result in ("loss", "lose"):
            conditions.append(table.c.realized_profit < 0)
    if start:
        dt = _parse_dt(start)
        if dt:
            conditions.append(table.c.close_date >= dt)
    if end:
        dt = _parse_dt(end)
        if dt:
            conditions.append(table.c.close_date < dt)

    session = db.bot_session()
    try:
        total = session.execute(
            select(func.count(table.c.id)).where(*conditions)
        ).scalar() or 0
        page = max(1, to_int(page))
        page_size = min(200, max(1, to_int(page_size)))
        stmt = (
            select(table)
            .where(*conditions)
            .order_by(table.c.close_date.desc().nullslast(), table.c.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = session.execute(stmt).mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"查询历史交易失败: {exc}")
        return empty
    finally:
        session.close()

    items = []
    for row in rows:
        open_dt = _parse_dt(row.get("open_date")) or _parse_dt(row.get("opened_at"))
        close_dt = _parse_dt(row.get("close_date"))
        pnl = to_float(row.get("close_profit_abs")) if row.get("close_profit_abs") is not None else to_float(row.get("realized_profit"))
        stake = to_float(row.get("stake_amount"))
        items.append(
            {
                "id": to_int(row.get("id")),
                "teacher": row.get("teacher") or row.get("teacher_name") or "—",
                "pair": row.get("pair") or "—",
                "direction": "SHORT" if row.get("is_short") else "LONG",
                "open_time": as_utc(open_dt),
                "close_time": as_utc(close_dt),
                "open_price": round2(to_float(row.get("open_rate")), 6),
                "close_price": round2(to_float(row.get("close_rate")), 6),
                "pnl": round2(pnl),
                # 收益率：OKX pnlRatio 为唯一真相源（净盈亏/保证金）；缺失时回退 pnl/stake
                "pnl_pct": (
                    round2(to_float(row.get("okx_pnl_ratio")) * 100)
                    if row.get("okx_pnl_ratio") is not None
                    else round2(safe_div(pnl, stake) * 100)
                ),
                "hold_seconds": _hold_seconds(open_dt, close_dt),
                "is_profit": pnl > 0,
                "leverage": round2(to_float(row.get("leverage"), 1.0)),
                "margin": round2(to_float(row.get("margin")) or stake),
                "exit_reason": row.get("exit_reason") or row.get("close_reason") or row.get("exit_type") or "",
                "strategy": row.get("strategy") or "",
            }
        )
    return {"items": items, "total": to_int(total), "page": page, "page_size": page_size}
