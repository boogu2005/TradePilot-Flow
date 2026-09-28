"""
信号追踪器 — 按带单群分开记忆信号，统计各群胜率、盈亏、响应速度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import func, Integer, Float
from sqlalchemy.orm import Session

from database.models import Trade, SignalLog
from .groups import SIGNAL_GROUPS


@dataclass
class GroupStats:
    """单个群的信号统计数据."""
    group_name: str
    total_signals: int = 0
    trading_signals: int = 0
    executed_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    last_signal_at: datetime | None = None
    recent_directions: list[dict] = field(default_factory=list)  # 最近10条方向


def get_all_group_stats(session: Session, days: int = 7) -> dict[str, GroupStats]:
    """获取所有群的信号统计 — 按群独立记忆."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # 按群统计信号日志
    rows = (
        session.query(
            SignalLog.tg_group_title,
            func.count(SignalLog.id).label("total"),
            func.sum(SignalLog.is_trading_signal.cast(Integer)).label("trading"),
            func.max(SignalLog.created_at).label("last_ts"),
        )
        .filter(SignalLog.created_at >= cutoff, SignalLog.tg_group_title.isnot(None))
        .group_by(SignalLog.tg_group_title)
        .all()
    )

    stats_map: dict[str, GroupStats] = {}
    for row in rows:
        name = row.tg_group_title.strip() if row.tg_group_title else "unknown"
        gs = GroupStats(
            group_name=name,
            total_signals=row.total or 0,
            trading_signals=row.trading or 0,
            last_signal_at=row.last_ts,
        )
        stats_map[name] = gs

    # 按群统计成交的 Trade
    trades = (
        session.query(Trade)
        .filter(Trade.open_date >= cutoff, Trade.signal_meta.isnot(None))
        .all()
    )
    for t in trades:
        src = (t.signal_meta or {}).get("source_group", "")
        if not src:
            continue
        gs = stats_map.setdefault(src, GroupStats(group_name=src))
        gs.executed_trades += 1
        if t.close_profit_abs is not None and not t.is_open:
            gs.total_pnl += t.close_profit_abs
            if t.close_profit_abs > 0:
                gs.winning_trades += 1

    # 每个群最近10条方向
    for name in stats_map:
        recent = (
            session.query(SignalLog.direction, SignalLog.pair, SignalLog.created_at)
            .filter(
                SignalLog.tg_group_title == name,
                SignalLog.is_trading_signal.is_(True),
                SignalLog.created_at >= cutoff,
            )
            .order_by(SignalLog.created_at.desc())
            .limit(10)
            .all()
        )
        stats_map[name].recent_directions = [
            {"dir": r.direction, "pair": r.pair, "ts": r.created_at.isoformat()} for r in recent
        ]

    return stats_map


def print_group_report(session: Session, days: int = 7):
    """打印各群信号报告."""
    stats = get_all_group_stats(session, days)
    if not stats:
        logger.info("暂无群组信号数据")
        return

    logger.info(f"\n{'='*70}")
    logger.info(f"各带单群信号报告 ({days}天)")
    logger.info(f"{'='*70}")

    for name, gs in sorted(stats.items()):
        win_rate = (
            f"{gs.winning_trades / gs.executed_trades * 100:.0f}%"
            if gs.executed_trades > 0
            else "N/A"
        )
        logger.info(
            f"  [{name}]\n"
            f"    消息: {gs.total_signals}条 | 交易信号: {gs.trading_signals}条 | "
            f"已执行: {gs.executed_trades}笔 | 胜率: {win_rate} | "
            f"盈亏: {gs.total_pnl:+.2f}USDT"
        )
        if gs.recent_directions:
            dirs = ", ".join(
                f"{d['dir']}({d['pair']})" for d in gs.recent_directions[:5]
            )
            logger.info(f"    最近信号: {dirs}")


# ———— 25 个带单群注册表 (从 groups.py 同步生成) ————
KNOWN_GROUPS: dict[str, str] = {g["short"]: g["full"] for g in SIGNAL_GROUPS}


def match_group(raw_title: str) -> str | None:
    """模糊匹配群名到已知带单群."""
    if not raw_title:
        return None
    raw = raw_title.strip()
    for key, full_name in KNOWN_GROUPS.items():
        if key.lower() in raw.lower() or raw.lower() in full_name.lower():
            return key
    return raw
