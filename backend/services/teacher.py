"""老师详情服务：风险指标 + 收益曲线 + 交易历史。"""
from __future__ import annotations

import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..database import db
from ..models import bot_models
from .common import as_utc, round2, safe_div, to_float, to_int
from .ranking import _aggregate_teacher, _fetch_teacher_rows


def _daily_series(rows: list[dict]) -> OrderedDict[str, float]:
    """按日累计收益曲线。"""
    daily: dict[str, float] = {}
    for row in rows:
        close = row.get("close_time")
        if close is None:
            continue
        day = close.strftime("%Y-%m-%d") if hasattr(close, "strftime") else str(close)[:10]
        daily[day] = daily.get(day, 0.0) + row["pnl"]
    ordered = OrderedDict(sorted(daily.items()))
    cumulative = 0.0
    series: OrderedDict[str, float] = OrderedDict()
    for day, pnl in ordered.items():
        cumulative += pnl
        series[day] = round2(cumulative)
    return series


def _max_drawdown(series: list[float]) -> float:
    """最大回撤（按累计收益曲线，返回正数百分比/金额形式）。"""
    peak = -float("inf")
    max_dd = 0.0
    for value in series:
        peak = max(peak, value)
        if peak > 0:
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
    return round2(max_dd * 100)


def _sharpe_ratio(daily_pnls: list[float]) -> float:
    """夏普比率（日收益序列，无风险利率 0）。"""
    if len(daily_pnls) < 2:
        return 0.0
    mean = sum(daily_pnls) / len(daily_pnls)
    variance = sum((x - mean) ** 2 for x in daily_pnls) / (len(daily_pnls) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return round2(safe_div(mean, std) * math.sqrt(365), 2)


def get_teacher_detail(teacher: str, days: int = 30) -> dict:
    """老师详情。days=0 表示全周期。"""
    all_rows = _fetch_teacher_rows(teacher=teacher)
    metrics = _aggregate_teacher(all_rows)

    window_rows = [r for r in all_rows if r["close_time"] is not None]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if days and days > 0:
        cutoff = now - timedelta(days=days)
        window_rows = [r for r in window_rows if r["close_time"] >= cutoff]
    window_metrics = _aggregate_teacher(window_rows)

    series_7 = _daily_series([r for r in all_rows if r["close_time"] is not None])
    series_30 = _daily_series(
        [r for r in all_rows if r["close_time"] is not None and r["close_time"] >= now - timedelta(days=30)]
    )
    series_all = _daily_series([r for r in all_rows if r["close_time"] is not None])

    daily_pnls: dict[str, float] = {}
    for row in window_rows:
        day = row["close_time"].strftime("%Y-%m-%d")
        daily_pnls[day] = daily_pnls.get(day, 0.0) + row["pnl"]

    all_daily: dict[str, float] = {}
    for row in all_rows:
        if row["close_time"] is None:
            continue
        day = row["close_time"].strftime("%Y-%m-%d")
        all_daily[day] = all_daily.get(day, 0.0) + row["pnl"]

    cumulative_all = []
    cumulative_win = 0.0
    for day in sorted(all_daily):
        cumulative_win += all_daily[day]
        cumulative_all.append(cumulative_win)

    return {
        "teacher": teacher,
        "stats": metrics,
        "window_stats": window_metrics,
        "window_days": days,
        "risk": {
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "max_drawdown": _max_drawdown(cumulative_all),
            "sharpe_ratio": _sharpe_ratio(list(daily_pnls.values())),
            "avg_hold_hours": metrics["avg_hold_hours"],
            "avg_win": metrics["avg_win"],
            "avg_loss": metrics["avg_loss"],
            "max_win": metrics["max_win"],
            "max_loss": metrics["max_loss"],
        },
        "curves": {
            "d7": [{"date": k, "cumulative_pnl": v} for k, v in series_7.items()],
            "d30": [{"date": k, "cumulative_pnl": v} for k, v in series_30.items()],
            "all": [{"date": k, "cumulative_pnl": v} for k, v in series_all.items()],
        },
    }


def get_teacher_trades(
    teacher: str,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """某老师的历史交易（分页）。"""
    table = bot_models.get_table("trades")
    empty = {"items": [], "total": 0, "page": page, "page_size": page_size}
    if table is None:
        return empty
    teacher_col = bot_models.teacher_column()
    if teacher_col is None:
        return empty
    from sqlalchemy import func

    session = db.bot_session()
    try:
        total = session.execute(
            select(func.count(table.c.id)).where(
                table.c.is_open.is_(False),
                table.c[teacher_col] == teacher,
            )
        ).scalar() or 0
        page = max(1, to_int(page))
        page_size = min(200, max(1, to_int(page_size)))
        rows = session.execute(
            select(table)
            .where(table.c.is_open.is_(False), table.c[teacher_col] == teacher)
            .order_by(table.c.close_date.desc().nullslast(), table.c.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).mappings().all()
    except Exception:
        return empty
    finally:
        session.close()

    items = []
    for row in rows:
        pnl = (
            to_float(row.get("close_profit_abs"))
            if row.get("close_profit_abs") is not None
            else to_float(row.get("realized_profit"))
        )
        stake = to_float(row.get("stake_amount"))
        open_dt, close_dt = row.get("open_date"), row.get("close_date")
        hold = None
        if open_dt is not None and close_dt is not None:
            try:
                hold = max(0, int((close_dt - open_dt).total_seconds()))
            except (TypeError, ValueError):
                hold = None
        items.append(
            {
                "id": to_int(row.get("id")),
                "pair": row.get("pair"),
                "direction": "SHORT" if row.get("is_short") else "LONG",
                "open_time": as_utc(open_dt),
                "close_time": as_utc(close_dt),
                "open_price": round2(to_float(row.get("open_rate")), 6),
                "close_price": round2(to_float(row.get("close_rate")), 6),
                "pnl": round2(pnl),
                "pnl_pct": round2(safe_div(pnl, stake) * 100),
                "hold_seconds": hold,
                "is_profit": pnl > 0,
                "leverage": round2(to_float(row.get("leverage"), 1.0)),
                "exit_reason": row.get("exit_reason") or row.get("close_reason") or "",
            }
        )
    return {"items": items, "total": to_int(total), "page": page, "page_size": page_size}
