"""
日统计模块 — 每日自动汇总交易数据写入 daily_statistics 表。

P3-1: 补全日统计初始化与数据填充逻辑。

调用方式：
  - 自动：cleanup_scheduler 在每日 03:00 清理后追加日统计计算
  - 手动：await fill_daily_statistics(session, target_date=date(2026, 7, 19))

数据来源：Trade 表（已平仓记录），OKX = 唯一真相源（不依赖其他缓存）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade, DailyStatistics

L = logger.bind(module="daily_stats")


def _utc_day_range(target_date: date) -> tuple[datetime, datetime]:
    """返回指定日期 UTC 的 [day_start, day_end)。"""
    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


def _safe_pnl(trade: Trade) -> float:
    """提取 Trade 的已实现盈亏，优先 OKX 真实值，回退 close_profit_abs。"""
    return trade.realized_profit or trade.close_profit_abs or 0.0


async def fill_daily_statistics(
    session: Session,
    target_date: Optional[date] = None,
) -> DailyStatistics | None:
    """
    计算并填充指定日期的 DailyStatistics。

    Args:
        session: SQLAlchemy Session
        target_date: 目标日期。None 默认昨天（每日凌晨运行时统计前一天）。

    Returns:
        新创建的 DailyStatistics 对象，或 None（无数据或已存在）。
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    # —— 幂等检查：该日期已有记录则跳过 ——
    existing = session.query(DailyStatistics).filter(
        DailyStatistics.date == target_date
    ).first()
    if existing is not None:
        L.debug(f"[DailyStats] {target_date} 已有记录，跳过")
        return None

    day_start, day_end = _utc_day_range(target_date)

    # —— 查询该日期内已平仓的 Trade ————
    closed = session.query(Trade).filter(
        Trade.is_open == False,
        Trade.close_date.isnot(None),
        Trade.close_date >= day_start,
        Trade.close_date < day_end,
    ).all()

    if not closed:
        L.info(f"[DailyStats] {target_date} 无已平仓交易，跳过")
        return None

    # —— 计算统计指标 ————
    pnls = [_safe_pnl(t) for t in closed]
    profits = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    trade_count = len(closed)
    win_count = len(profits)
    loss_count = len(losses)

    # 手续费汇总
    total_fees = 0.0
    for t in closed:
        if t.fee_open_cost:
            total_fees += t.fee_open_cost
        if t.fee_close_cost:
            total_fees += t.fee_close_cost

    # 交易量汇总（名义价值）
    total_volume = 0.0
    for t in closed:
        total_volume += t.stake_amount * (t.leverage or 1.0)

    # 最大回撤（基于日内 closed PnL 累计）
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    stats = DailyStatistics(
        date=target_date,
        profit=round(sum(profits), 4),
        loss=round(sum(losses), 4),
        net_profit=round(sum(pnls), 4),
        fees=round(total_fees, 4),
        trade_count=trade_count,
        win_count=win_count,
        loss_count=loss_count,
        win_rate=round(win_count / trade_count, 4) if trade_count > 0 else 0.0,
        volume=round(total_volume, 2),
        max_drawdown=round(max_drawdown, 4),
    )

    session.add(stats)
    session.commit()

    L.success(
        f"[DailyStats] {target_date} 统计完成: "
        f"trades={trade_count} wins={win_count} losses={loss_count} "
        f"net={sum(pnls):.2f}U fees={total_fees:.4f}U "
        f"win_rate={stats.win_rate*100:.1f}%"
    )

    return stats


async def fill_missing_statistics(session: Session, days_back: int = 30) -> int:
    """
    回填最近 N 天缺失的日统计（用于首次部署后的数据补齐）。

    Args:
        session: SQLAlchemy Session
        days_back: 向前回填天数（默认 30 天）

    Returns:
        填充的记录数
    """
    today = date.today()
    filled = 0

    for i in range(days_back):
        target = today - timedelta(days=i + 1)
        result = await fill_daily_statistics(session, target_date=target)
        if result is not None:
            filled += 1

    if filled > 0:
        L.info(f"[DailyStats] 回填完成: {filled} 天数据已补齐")

    return filled
