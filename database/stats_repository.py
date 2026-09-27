"""
统计查询 Repository。

提供 AI 管理后台所需的统计查询接口。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select, func, and_, or_, desc

from .models import (
    Trade, Order, TeacherStat, AccountSnapshot, SystemEvent,
    Execution, TeacherMessage, SignalLog
)
from .db import get_session


class StatsRepository:
    """统计查询 Repository"""

    @staticmethod
    def get_today_summary() -> dict:
        """
        获取今日统计摘要。

        返回:
        {
            "date": "2026-07-12",
            "profit": 123.45,
            "loss": -67.89,
            "net_profit": 55.56,
            "fee": 12.34,
            "trade_count": 10,
            "win_count": 6,
            "loss_count": 4,
            "win_rate": 0.6,
            "open_trades": 3,
        }
        """
        session = get_session()
        try:
            # 今日时间范围
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)

            # 今日已平仓交易
            closed_trades = session.execute(
                select(Trade).where(
                    and_(
                        Trade.close_date >= today_start,
                        Trade.close_date < today_end,
                        Trade.is_open.is_(False)
                    )
                )
            ).scalars().all()

            # 计算盈亏 — 优先 realized_profit，回退 close_profit_abs（兼容历史数据）
            def _trade_pnl(t: Trade) -> float:
                return t.realized_profit or t.close_profit_abs or 0.0

            profit = sum(_trade_pnl(t) for t in closed_trades if _trade_pnl(t) > 0)
            loss = sum(_trade_pnl(t) for t in closed_trades if _trade_pnl(t) < 0)
            net_profit = sum(_trade_pnl(t) for t in closed_trades)

            # 计算手续费
            fee = 0.0
            for t in closed_trades:
                if t.fee_open_cost:
                    fee += t.fee_open_cost
                if t.fee_close_cost:
                    fee += t.fee_close_cost

            # 统计胜负
            win_count = len([t for t in closed_trades if _trade_pnl(t) > 0])
            loss_count = len([t for t in closed_trades if _trade_pnl(t) < 0])
            trade_count = len(closed_trades)
            win_rate = win_count / trade_count if trade_count > 0 else 0.0

            # 当前持仓数
            open_trades = session.scalar(
                select(func.count(Trade.id)).where(Trade.is_open.is_(True))
            ) or 0

            return {
                "date": today_start.strftime("%Y-%m-%d"),
                "profit": round(profit, 2),
                "loss": round(loss, 2),
                "net_profit": round(net_profit, 2),
                "fee": round(fee, 2),
                "trade_count": trade_count,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": round(win_rate, 4),
                "open_trades": open_trades,
            }

        finally:
            session.close()

    @staticmethod
    def get_teacher_rank(
        days: int = 30,
        sort_by: str = "total_profit",
        limit: int = 20
    ) -> list[dict]:
        """
        获取老师排行榜。

        Args:
            days: 统计天数
            sort_by: 排序字段（total_profit/profit_factor/win_rate）
            limit: 返回数量

        返回:
        [
            {
                "teacher": "老师A",
                "total_trades": 50,
                "wins": 30,
                "losses": 20,
                "win_rate": 0.6,
                "total_profit": 1234.56,
                "profit_factor": 2.5,
                "last_trade_time": "2026-07-12T10:00:00Z",
            },
            ...
        ]
        """
        session = get_session()
        try:
            # 查询 teacher_stats 表
            query = select(TeacherStat).where(
                TeacherStat.total_trades > 0
            )

            # 排序
            if sort_by == "profit_factor":
                query = query.order_by(desc(TeacherStat.profit_factor))
            elif sort_by == "win_rate":
                query = query.order_by(desc(TeacherStat.win_rate))
            else:  # total_profit
                query = query.order_by(desc(TeacherStat.total_profit))

            query = query.limit(limit)

            stats = session.execute(query).scalars().all()

            result = []
            for s in stats:
                result.append({
                    "teacher": s.teacher,
                    "group_name": s.group_name,
                    "total_trades": s.total_trades,
                    "wins": s.wins,
                    "losses": s.losses,
                    "win_rate": round(s.win_rate, 4),
                    "total_profit": round(s.total_profit, 2),
                    "average_profit": round(s.average_profit, 2),
                    "average_loss": round(s.average_loss, 2),
                    "profit_factor": round(s.profit_factor, 2) if s.profit_factor != float("inf") else 999.99,
                    "total_volume": round(s.total_volume, 2),
                    "last_trade_time": s.last_trade_time.isoformat() if s.last_trade_time else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                })

            return result

        finally:
            session.close()

    @staticmethod
    def get_teacher_detail(teacher: str) -> dict:
        """
        获取老师详细信息。

        返回:
        {
            "teacher": "老师A",
            "total_trades": 50,
            "wins": 30,
            "losses": 20,
            "win_rate": 0.6,
            "total_profit": 1234.56,
            "average_profit": 45.67,
            "average_loss": -23.45,
            "profit_factor": 2.5,
            "total_volume": 50000.0,
            "last_trade_time": "2026-07-12T10:00:00Z",
            "recent_trades": [...],
            "profit_curve": [...],
        }
        """
        session = get_session()
        try:
            # 查询老师统计
            stat = session.execute(
                select(TeacherStat).where(TeacherStat.teacher == teacher)
            ).scalar_one_or_none()

            if stat is None:
                return {"error": f"老师 {teacher} 不存在"}

            # 查询最近交易（最近 10 条）
            recent_trades = session.execute(
                select(Trade).where(
                    Trade.teacher == teacher
                ).order_by(desc(Trade.close_date)).limit(10)
            ).scalars().all()

            recent_trades_data = []
            for t in recent_trades:
                recent_trades_data.append({
                    "id": t.id,
                    "pair": t.pair,
                    "is_short": t.is_short,
                    "open_rate": t.open_rate,
                    "close_rate": t.close_rate,
                    "amount": t.amount,
                    "leverage": t.leverage,
                    "realized_profit": round(t.realized_profit or t.close_profit_abs or 0.0, 2),
                    "open_date": t.open_date.isoformat() if t.open_date else None,
                    "close_date": t.close_date.isoformat() if t.close_date else None,
                    "exit_reason": t.exit_reason,
                    "exit_type": t.exit_type,
                    "is_open": t.is_open,
                })

            # 查询收益曲线（最近 30 天）
            profit_curve = StatsRepository._calculate_teacher_profit_curve(teacher, days=30)

            return {
                "teacher": stat.teacher,
                "group_name": stat.group_name,
                "total_trades": stat.total_trades,
                "wins": stat.wins,
                "losses": stat.losses,
                "win_rate": round(stat.win_rate, 4),
                "total_profit": round(stat.total_profit, 2),
                "average_profit": round(stat.average_profit, 2),
                "average_loss": round(stat.average_loss, 2),
                "profit_factor": round(stat.profit_factor, 2) if stat.profit_factor != float("inf") else 999.99,
                "total_volume": round(stat.total_volume, 2),
                "last_trade_time": stat.last_trade_time.isoformat() if stat.last_trade_time else None,
                "updated_at": stat.updated_at.isoformat() if stat.updated_at else None,
                "recent_trades": recent_trades_data,
                "profit_curve": profit_curve,
            }

        finally:
            session.close()

    @staticmethod
    def _calculate_teacher_profit_curve(teacher: str, days: int = 30) -> list[dict]:
        """计算老师收益曲线"""
        session = get_session()
        try:
            # 查询最近 N 天的交易
            start_date = datetime.now(timezone.utc) - timedelta(days=days)

            trades = session.execute(
                select(Trade).where(
                    and_(
                        Trade.teacher == teacher,
                        Trade.close_date >= start_date,
                        Trade.is_open.is_(False)
                    )
                ).order_by(Trade.close_date)
            ).scalars().all()

            # 按天聚合
            daily_profit = {}
            for t in trades:
                if t.close_date:
                    day = t.close_date.strftime("%Y-%m-%d")
                    if day not in daily_profit:
                        daily_profit[day] = 0.0
                    daily_profit[day] += (t.realized_profit or t.close_profit_abs or 0.0)

            # 转换为列表
            curve = []
            cumulative = 0.0
            for day in sorted(daily_profit.keys()):
                cumulative += daily_profit[day]
                curve.append({
                    "date": day,
                    "daily_profit": round(daily_profit[day], 2),
                    "cumulative_profit": round(cumulative, 2),
                })

            return curve

        finally:
            session.close()

    @staticmethod
    def get_profit_curve(days: int = 30) -> list[dict]:
        """
        获取整体收益曲线。

        返回:
        [
            {"date": "2026-07-01", "equity": 10000.0, "daily_profit": 100.0},
            ...
        ]
        """
        session = get_session()
        try:
            # 从 account_snapshots 查询
            start_date = datetime.now(timezone.utc) - timedelta(days=days)

            snapshots = session.execute(
                select(AccountSnapshot).where(
                    AccountSnapshot.time >= start_date
                ).order_by(AccountSnapshot.time)
            ).scalars().all()

            if not snapshots:
                # 如果没有快照，从 trades 计算
                return StatsRepository._calculate_profit_curve_from_trades(days)

            # 按天聚合（取每天最后一个快照）
            daily_snapshots = {}
            for s in snapshots:
                day = s.time.strftime("%Y-%m-%d")
                daily_snapshots[day] = s

            curve = []
            for day in sorted(daily_snapshots.keys()):
                s = daily_snapshots[day]
                curve.append({
                    "date": day,
                    "equity": round(s.equity, 2),
                    "balance": round(s.balance, 2),
                    "unrealized_pnl": round(s.unrealized_pnl, 2),
                    "open_positions": s.open_positions,
                })

            return curve

        finally:
            session.close()

    @staticmethod
    def _calculate_profit_curve_from_trades(days: int = 30) -> list[dict]:
        """从 trades 计算收益曲线（当没有 account_snapshots 时）"""
        session = get_session()
        try:
            start_date = datetime.now(timezone.utc) - timedelta(days=days)

            trades = session.execute(
                select(Trade).where(
                    and_(
                        Trade.close_date >= start_date,
                        Trade.is_open.is_(False)
                    )
                ).order_by(Trade.close_date)
            ).scalars().all()

            # 按天聚合
            daily_profit = {}
            for t in trades:
                if t.close_date:
                    day = t.close_date.strftime("%Y-%m-%d")
                    if day not in daily_profit:
                        daily_profit[day] = 0.0
                    daily_profit[day] += (t.realized_profit or t.close_profit_abs or 0.0)

            # 转换为列表
            curve = []
            cumulative = 0.0
            for day in sorted(daily_profit.keys()):
                cumulative += daily_profit[day]
                curve.append({
                    "date": day,
                    "daily_profit": round(daily_profit[day], 2),
                    "cumulative_profit": round(cumulative, 2),
                })

            return curve

        finally:
            session.close()

    @staticmethod
    def get_trade_history(
        days: int = 30,
        teacher: Optional[str] = None,
        pair: Optional[str] = None,
        is_open: Optional[bool] = None,
        limit: int = 100
    ) -> list[dict]:
        """
        获取交易历史。

        支持过滤：
        - teacher: 老师名称
        - pair: 交易对
        - is_open: 是否持仓
        """
        session = get_session()
        try:
            start_date = datetime.now(timezone.utc) - timedelta(days=days)

            query = select(Trade).where(Trade.open_date >= start_date)

            if teacher:
                query = query.where(Trade.teacher == teacher)
            if pair:
                query = query.where(Trade.pair == pair)
            if is_open is not None:
                query = query.where(Trade.is_open == is_open)

            query = query.order_by(desc(Trade.open_date)).limit(limit)

            trades = session.execute(query).scalars().all()

            result = []
            for t in trades:
                result.append({
                    "id": t.id,
                    "pair": t.pair,
                    "is_short": t.is_short,
                    "leverage": t.leverage,
                    "open_rate": t.open_rate,
                    "close_rate": t.close_rate,
                    "amount": t.amount,
                    "stake_amount": t.stake_amount,
                    "realized_profit": round(t.realized_profit or t.close_profit_abs or 0.0, 2),
                    "open_date": t.open_date.isoformat() if t.open_date else None,
                    "close_date": t.close_date.isoformat() if t.close_date else None,
                    "exit_reason": t.exit_reason,
                    "exit_type": t.exit_type,
                    "is_open": t.is_open,
                    "teacher": t.teacher,
                    "stop_loss": t.stop_loss,
                    "tp1_price": t.tp1_price,
                })

            return result

        finally:
            session.close()

    @staticmethod
    def get_system_health() -> dict:
        """
        获取系统健康状态。

        返回:
        {
            "status": "healthy",
            "uptime_hours": 24.5,
            "recent_errors": 2,
            "recent_warnings": 5,
            "last_event": {...},
            "error_summary": [...],
        }
        """
        session = get_session()
        try:
            # 查询最近 24 小时的事件
            since = datetime.now(timezone.utc) - timedelta(hours=24)

            # 统计错误和警告
            error_count = session.scalar(
                select(func.count(SystemEvent.id)).where(
                    and_(
                        SystemEvent.time >= since,
                        SystemEvent.level.in_(["error", "critical"])
                    )
                )
            ) or 0

            warning_count = session.scalar(
                select(func.count(SystemEvent.id)).where(
                    and_(
                        SystemEvent.time >= since,
                        SystemEvent.level == "warning"
                    )
                )
            ) or 0

            # 查询最近的事件
            last_event = session.execute(
                select(SystemEvent).order_by(desc(SystemEvent.time)).limit(1)
            ).scalar_one_or_none()

            last_event_data = None
            if last_event:
                last_event_data = {
                    "time": last_event.time.isoformat(),
                    "level": last_event.level,
                    "module": last_event.module,
                    "event_type": last_event.event_type,
                    "message": last_event.message,
                }

            # 查询最近的错误（最近 10 条）
            recent_errors = session.execute(
                select(SystemEvent).where(
                    SystemEvent.level.in_(["error", "critical"])
                ).order_by(desc(SystemEvent.time)).limit(10)
            ).scalars().all()

            error_summary = []
            for e in recent_errors:
                error_summary.append({
                    "time": e.time.isoformat(),
                    "module": e.module,
                    "event_type": e.event_type,
                    "message": e.message,
                })

            # 判断状态
            status = "healthy" if error_count == 0 else ("warning" if error_count < 5 else "unhealthy")

            return {
                "status": status,
                "recent_errors": error_count,
                "recent_warnings": warning_count,
                "last_event": last_event_data,
                "error_summary": error_summary,
            }

        finally:
            session.close()

    @staticmethod
    def get_recent_errors(limit: int = 50) -> list[dict]:
        """
        获取最近的系统错误。

        返回:
        [
            {
                "time": "2026-07-12T10:00:00Z",
                "level": "error",
                "module": "position_manager",
                "event_type": "ws_disconnect",
                "message": "WebSocket 连接断开",
                "trade_id": 123,
                "symbol": "BTC/USDT:USDT",
            },
            ...
        ]
        """
        session = get_session()
        try:
            events = session.execute(
                select(SystemEvent).where(
                    SystemEvent.level.in_(["error", "critical"])
                ).order_by(desc(SystemEvent.time)).limit(limit)
            ).scalars().all()

            result = []
            for e in events:
                result.append({
                    "time": e.time.isoformat(),
                    "level": e.level,
                    "module": e.module,
                    "event_type": e.event_type,
                    "message": e.message,
                    "trade_id": e.trade_id,
                    "symbol": e.symbol,
                    "details": e.details,
                })

            return result

        finally:
            session.close()

    @staticmethod
    def get_executions(trade_id: Optional[int] = None, limit: int = 100) -> list[dict]:
        """
        获取成交记录。

        用于统计：真实成交价、滑点、手续费、部分成交。
        """
        session = get_session()
        try:
            query = select(Execution)

            if trade_id:
                query = query.where(Execution.trade_id == trade_id)

            query = query.order_by(desc(Execution.time)).limit(limit)

            executions = session.execute(query).scalars().all()

            result = []
            for e in executions:
                result.append({
                    "id": e.id,
                    "trade_id": e.trade_id,
                    "order_id": e.order_id,
                    "exchange_execution_id": e.exchange_execution_id,
                    "price": e.price,
                    "amount": e.amount,
                    "fee": e.fee,
                    "side": e.side,
                    "time": e.time.isoformat(),
                    "details": e.details,
                })

            return result

        finally:
            session.close()
