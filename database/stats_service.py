"""
统计服务模块。

提供自动化统计功能：
- teacher_stats 自动更新
- account_snapshots 定时写入
- system_events 自动记录
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select, func

from .models import TeacherStat, AccountSnapshot, SystemEvent, Trade
from .db import get_session


class StatsService:
    """统计服务"""

    def __init__(self):
        self._snapshot_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """启动统计服务"""
        if self._running:
            return

        self._running = True
        logger.info("启动统计服务...")

        # 启动账户快照定时任务（60秒一次）
        self._snapshot_task = asyncio.create_task(self._snapshot_loop())

        logger.success("统计服务启动完成")

    async def stop(self):
        """停止统计服务"""
        if not self._running:
            return

        self._running = False

        if self._snapshot_task:
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass
            self._snapshot_task = None

        logger.info("统计服务已停止")

    async def _snapshot_loop(self):
        """账户快照定时写入循环"""
        logger.info("账户快照定时任务启动（60秒间隔）")

        while self._running:
            try:
                await self.take_account_snapshot()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"账户快照写入失败: {e}")
                await asyncio.sleep(60)

    async def take_account_snapshot(self):
        """写入账户快照"""
        try:
            # 这里需要从外部传入余额数据获取函数
            # 暂时使用空数据，实际使用时需要注入
            balance_data = {
                "balance": 0.0,
                "equity": 0.0,
                "available": 0.0,
                "margin": 0.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
            }

            # 使用单个 Session 完成查询和写入
            session = get_session()
            try:
                # 获取当前持仓数
                positions_count = session.scalar(
                    select(func.count(Trade.id)).where(Trade.is_open.is_(True))
                ) or 0

                snapshot = AccountSnapshot.create_from_balance(balance_data, positions_count)
                session.add(snapshot)
                session.commit()
                logger.debug(f"账户快照写入成功: equity={balance_data['equity']}, positions={positions_count}")
            except Exception as e:
                session.rollback()
                logger.error(f"账户快照写入失败: {e}")
                raise
            finally:
                session.close()

        except Exception as e:
            logger.error(f"写入账户快照失败: {e}")

    @staticmethod
    async def update_teacher_stats(teacher: str):
        """更新指定老师的统计数据"""
        session = get_session()
        try:
            # 查询该老师的所有交易
            trades = session.execute(
                select(Trade).where(
                    Trade.source_group_name == teacher
                )
            ).scalars().all()

            if not trades:
                logger.debug(f"老师 {teacher} 没有交易记录")
                return

            # 计算统计数据
            stats_data = TeacherStat.calculate_from_trades(trades)

            # 查询或创建 TeacherStat 记录
            teacher_stat = session.execute(
                select(TeacherStat).where(TeacherStat.teacher == teacher)
            ).scalar_one_or_none()

            if teacher_stat is None:
                teacher_stat = TeacherStat(teacher=teacher)
                session.add(teacher_stat)

            # 更新统计数据
            teacher_stat.total_trades = stats_data["total_trades"]
            teacher_stat.wins = stats_data["wins"]
            teacher_stat.losses = stats_data["losses"]
            teacher_stat.win_rate = stats_data["win_rate"]
            teacher_stat.total_profit = stats_data["total_profit"]
            teacher_stat.average_profit = stats_data["average_profit"]
            teacher_stat.average_loss = stats_data["average_loss"]
            teacher_stat.profit_factor = stats_data["profit_factor"]
            teacher_stat.total_volume = stats_data["total_volume"]
            teacher_stat.last_trade_time = stats_data["last_trade_time"]
            teacher_stat.updated_at = datetime.now(timezone.utc)

            # 设置 group_name（取第一个交易的 source_group_name）
            if trades and not teacher_stat.group_name:
                teacher_stat.group_name = trades[0].source_group_name

            session.commit()
            logger.info(f"老师 {teacher} 统计更新完成: trades={stats_data['total_trades']}, profit={stats_data['total_profit']:.2f}")

        except Exception as e:
            session.rollback()
            logger.error(f"更新老师统计失败: {e}")
            raise
        finally:
            session.close()

    @staticmethod
    async def update_all_teacher_stats():
        """更新所有老师的统计数据"""
        # 先查询所有老师名称，避免嵌套 Session
        session = get_session()
        try:
            # 查询所有有 source_group_name 的交易
            teachers = session.execute(
                select(Trade.source_group_name).where(
                    Trade.source_group_name.isnot(None)
                ).distinct()
            ).scalars().all()

            logger.info(f"开始更新 {len(teachers)} 位老师的统计数据...")
        except Exception as e:
            logger.error(f"查询老师列表失败: {e}")
            raise
        finally:
            session.close()

        # 关闭外层 Session 后，逐个更新统计
        for teacher in teachers:
            await StatsService.update_teacher_stats(teacher)

        logger.success(f"所有老师统计更新完成")

    @staticmethod
    async def log_system_event(
        level: str,
        module: str,
        event_type: str,
        message: str,
        trade_id: Optional[int] = None,
        symbol: Optional[str] = None,
        details: Optional[dict] = None,
    ):
        """记录系统事件"""
        try:
            event = SystemEvent.log(
                level=level,
                module=module,
                event_type=event_type,
                message=message,
                trade_id=trade_id,
                symbol=symbol,
                details=details,
            )

            session = get_session()
            try:
                session.add(event)
                session.commit()
                logger.debug(f"系统事件记录: [{level}] {module}.{event_type} - {message}")
            except Exception as e:
                session.rollback()
                logger.error(f"系统事件记录失败: {e}")
                raise
            finally:
                session.close()

        except Exception as e:
            logger.error(f"记录系统事件失败: {e}")


# 全局实例
_stats_service: Optional[StatsService] = None


def get_stats_service() -> StatsService:
    """获取统计服务实例"""
    global _stats_service
    if _stats_service is None:
        _stats_service = StatsService()
    return _stats_service
