"""带单老师排行榜服务。

基于 trades 表已平仓订单，按老师聚合统计：
- profit:        总盈利金额
- profit_factor: 盈亏比 = 总盈利 / |总亏损|
- win_rate:      胜率 = 盈利单 / 总单
- roi:           总收益率 = 净盈利 / 总保证金
- risk_adjusted: 动态仓位分配榜 = 平均收益率 / 标准差（夏普思路，档位分配唯一依据）

动态仓位分配榜口径（详见 docs/dynamic-position-ranking.md）：
  收益率序列取 OKX pnlRatio（净收益率真相源），缺失回退 realized_profit/stake；
  Winsorize ±300% → r̄、σ(样本, ddof=1) → Score = clip(r̄/σ', ±10)，σ' = max(σ, 1e-4)；
  90 天榜 n<5（30/7 天榜 n<2）→ 未达标，置榜单末尾。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

from ..database import db
from ..models import bot_models
from .common import round2, safe_div, to_float, to_int, utc_now

# ———— 动态仓位分配榜参数（与需求文档保持一致） ————
SIGMA_FLOOR = 1e-4          # σ 地板，防除零
SCORE_CAP = 10.0            # Score 截断上限
RATIO_CAP = 3.0             # 单笔收益率 Winsorize 截断（±300%）
MIN_TRADES_90D = 5          # 90 天榜准入门槛
MIN_TRADES_OTHER = 2        # 30/7 天榜：能算 σ 即可


def _trades_table():
    return bot_models.get_table("trades")


def _aggregate_teacher(rows: list[dict]) -> dict:
    total_trades = len(rows)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    total_profit = sum(r["pnl"] for r in wins)
    total_loss = abs(sum(r["pnl"] for r in losses))
    total_stake = sum(r["stake"] for r in rows)

    profit_factor: float | None = None
    if total_loss > 0:
        profit_factor = round2(safe_div(total_profit, total_loss), 2)
    elif total_profit > 0:
        profit_factor = None  # 无亏损 → 无穷大

    hold_seconds = [r["hold"] for r in rows if r["hold"] is not None]

    return {
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round2(safe_div(len(wins), total_trades) * 100, 2),
        "total_profit": round2(total_profit),
        "total_loss": round2(total_loss),
        "net_profit": round2(total_profit - total_loss),
        "profit_factor": profit_factor,
        "avg_win": round2(safe_div(total_profit, len(wins))) if wins else 0.0,
        "avg_loss": round2(safe_div(total_loss, len(losses))) if losses else 0.0,
        "max_win": round2(max((r["pnl"] for r in wins), default=0.0)),
        "max_loss": round2(min((r["pnl"] for r in losses), default=0.0)),
        "roi": round2(safe_div(total_profit - total_loss, total_stake) * 100) if total_stake else 0.0,
        "avg_hold_hours": round2(safe_div(sum(hold_seconds), len(hold_seconds)) / 3600, 2) if hold_seconds else 0.0,
        "last_trade_time": max((r["close_time"] for r in rows if r["close_time"]), default=None),
    }


def _fetch_teacher_rows(days: int | None = None, teacher: str | None = None) -> list[dict]:
    """拉取已平仓订单（含老师、盈亏、持仓时长）。"""
    table = _trades_table()
    if table is None:
        return []
    teacher_col = bot_models.teacher_column()
    if teacher_col is None:
        return []
    conditions = [table.c.is_open.is_(False)]
    # 只统计有真实老师归属的带单交易；teacher 为空(NULL/空串)的
    # 对账/恢复记录不属于任何老师，不计入老师榜，避免出现虚假的"未知"老师
    conditions.append(table.c[teacher_col].is_not(None))
    conditions.append(table.c[teacher_col] != "")
    if days:
        conditions.append(table.c.close_date >= utc_now() - timedelta(days=days))
    if teacher:
        conditions.append(table.c[teacher_col] == teacher)
    # okx_pnl_ratio 列可能不存在（旧库未迁移），做防御
    try:
        has_okx_ratio = "okx_pnl_ratio" in table.c
    except Exception:  # noqa: BLE001
        has_okx_ratio = False
    cols = [
        table.c[teacher_col],
        table.c.realized_profit,
        table.c.close_profit_abs,
        table.c.stake_amount,
        table.c.open_date,
        table.c.close_date,
    ]
    if has_okx_ratio:
        cols.append(table.c.okx_pnl_ratio)
    session = db.bot_session()
    try:
        rows = session.execute(
            select(*cols).where(*conditions)
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"拉取老师交易失败: {exc}")
        return []
    finally:
        session.close()

    result = []
    for row in rows:
        pnl = (
            to_float(row.get("close_profit_abs"))
            if row.get("close_profit_abs") is not None
            else to_float(row.get("realized_profit"))
        )
        stake = to_float(row.get("stake_amount"))
        # 收益率口径：OKX pnlRatio 优先，回退 净盈亏/保证金
        ratio = None
        if has_okx_ratio:
            raw = row.get("okx_pnl_ratio")
            ratio = to_float(raw) if raw is not None else None
        if ratio is None and stake and stake > 0:
            ratio = safe_div(pnl, stake)
        open_dt, close_dt = row.get("open_date"), row.get("close_date")
        hold = None
        if open_dt is not None and close_dt is not None:
            try:
                hold = max(0, int((close_dt - open_dt).total_seconds()))
            except (TypeError, ValueError):
                hold = None
        result.append(
            {
                "teacher": row.get(teacher_col) or "",
                "pnl": pnl,
                "stake": stake,
                "ratio": ratio,
                "hold": hold,
                "close_time": close_dt,
            }
        )
    return result


def _sort_key(metrics: dict, sort_by: str) -> tuple:
    if sort_by == "profit":
        return (metrics["net_profit"], metrics["total_profit"], metrics["win_rate"])
    if sort_by == "profit_factor":
        # 无亏损（None=无穷大）排最前
        pf = metrics["profit_factor"]
        return (10**9 if pf is None else pf, metrics["net_profit"], metrics["win_rate"])
    if sort_by == "roi":
        return (metrics["roi"], metrics["net_profit"], metrics["win_rate"])
    return (metrics["win_rate"], metrics["net_profit"], metrics["total_profit"])


def get_risk_adjusted_ranking(period: int = 90, limit: int = 50) -> list[dict]:
    """动态仓位分配榜单：Score = r̄ / σ（夏普思路），档位分配的唯一依据。

    排序：达标组按 (Score, r̄, n, teacher) 降序；未达标组置末尾（n 降序 → r̄ 降序）。
    """
    rows = _fetch_teacher_rows(days=period)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped.setdefault(row["teacher"], []).append(row)

    min_trades = MIN_TRADES_90D if period >= 90 else MIN_TRADES_OTHER
    eligible: list[dict] = []
    tail: list[dict] = []
    for teacher, teacher_rows in grouped.items():
        metrics = _aggregate_teacher(teacher_rows)
        ratios = []
        for r in teacher_rows:
            v = r.get("ratio")
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f):
                ratios.append(f)
        n = len(ratios)
        w = [max(-RATIO_CAP, min(RATIO_CAP, x)) for x in ratios]
        mean = sigma = score = 0.0
        degenerate = n < 2
        if n >= 2:
            mean = sum(w) / n
            var = sum((x - mean) ** 2 for x in w) / (n - 1)
            sigma = math.sqrt(var)
            if sigma < SIGMA_FLOOR:
                degenerate = True
                score = SCORE_CAP if mean > 0 else (-SCORE_CAP if mean < 0 else 0.0)
            else:
                score = max(-SCORE_CAP, min(SCORE_CAP, mean / sigma))
        item = {
            **metrics,
            "teacher": teacher,
            "period_days": period,
            "mean_ratio": round2(mean, 4),
            "std_ratio": round2(sigma, 4),
            "risk_adjusted_score": round2(score, 4),
            "degenerate": degenerate,
            "ratio_trades": n,
            "eligible": n >= min_trades,
        }
        (eligible if item["eligible"] else tail).append(item)

    eligible.sort(
        key=lambda m: (m["risk_adjusted_score"], m["mean_ratio"], m["ratio_trades"], m["teacher"]),
        reverse=True,
    )
    tail.sort(key=lambda m: (-m["ratio_trades"], -m["mean_ratio"], m["teacher"]))
    ranking = eligible + tail
    ranking = ranking[: max(1, to_int(limit))]
    for rank, item in enumerate(ranking, start=1):
        item["rank"] = rank
    return ranking


def get_teacher_ranking(period: int = 30, sort_by: str = "profit", limit: int = 50) -> list[dict]:
    """排行榜。period: 7/30；sort_by: profit/profit_factor/win_rate。"""
    rows = _fetch_teacher_rows(days=period)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["teacher"], []).append(row)

    ranking: list[dict] = []
    for teacher, teacher_rows in grouped.items():
        metrics = _aggregate_teacher(teacher_rows)
        metrics["teacher"] = teacher
        metrics["period_days"] = period
        ranking.append(metrics)

    ranking.sort(key=lambda m: _sort_key(m, sort_by), reverse=True)
    ranking = ranking[: max(1, to_int(limit))]
    for rank, item in enumerate(ranking, start=1):
        item["rank"] = rank
    return ranking


def list_teachers(limit: int = 200) -> list[dict]:
    """所有老师列表（全周期聚合），用于下拉选择。"""
    rows = _fetch_teacher_rows()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["teacher"], []).append(row)
    result = []
    for teacher, teacher_rows in grouped.items():
        metrics = _aggregate_teacher(teacher_rows)
        metrics["teacher"] = teacher
        result.append(metrics)
    result.sort(key=lambda m: m["net_profit"], reverse=True)
    return result[: max(1, to_int(limit))]
