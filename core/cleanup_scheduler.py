"""
数据库清理调度器 — 每日 03:00（本地时间）执行一次自动清理。

使用方式（在 main.py 中注册为 TaskManager 后台任务）：

    from core.cleanup_scheduler import run_cleanup_scheduler
    await tasks.create("数据库清理", lambda: run_cleanup_scheduler(shutdown_event))

设计原则：
  - 使用独立 session，绝不与交易线程共享 session
  - 仅通过 TaskManager 管理生命周期，随机器人关闭自动停止
  - 不依赖任何交易/风控/状态机模块
  - 异常仅记录日志，不影响主流程
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from loguru import logger

from database.db import get_session
from database.cleanup_service import cleanup_database
from database.daily_stats import fill_daily_statistics


async def run_cleanup_scheduler(shutdown_event: asyncio.Event) -> None:
    """
    清理调度器协程 — 每日 03:00（本地时间）执行一次。

    工作流程：
      1. 计算到下次 03:00 的等待秒数
      2. asyncio.wait_for 等待（可被 shutdown_event 中断）
      3. 时间到 → 打开独立 session → 执行 cleanup_database() → fill_daily_statistics()
      4. 循环继续，等待下一个 03:00

    由于使用 wait_for(timeout)，shutdown 时能及时响应，
    不会阻塞机器人关闭流程。
    """
    logger.info("[清理调度器] 已启动（每日 03:00 本地时间：数据库清理 + 日统计）")

    while not shutdown_event.is_set():
        # ———— 计算到下次 03:00（本地时间）的等待时间 ————
        # NOTE: 使用系统本地时间（naive datetime），仅计算等待秒数
        # 不与数据库时间比较，不会引发 naive/aware 类型错误
        now = datetime.now()
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()

        logger.debug(
            f"[清理调度器] 下次执行: {next_run.strftime('%m-%d %H:%M')} "
            f"(等待 {wait_seconds/3600:.1f}h)"
        )

        # ———— 等待（可被 shutdown 中断）————
        # wait_for 超时后继续执行清理，shutdown_event.set() 时立即退出
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            pass

        if shutdown_event.is_set():
            break

        # ———— 执行清理 + 日统计（独立 session，与交易线程隔离）————
        session = get_session()
        try:
            # Phase 1: 清理过期数据
            stats = await cleanup_database(session)
            total = sum(v for k, v in stats.items() if k != "vacuum")
            if total > 0:
                logger.info(f"[清理调度器] 本次清理完成: 删除了 {total} 条过期数据")

            # Phase 2: 填充昨日日统计（P3-1）
            try:
                await fill_daily_statistics(session)
            except Exception as e:
                logger.warning(f"[清理调度器] 日统计填充异常（已隔离）: {e}")
        except Exception as e:
            logger.warning(f"[清理调度器] 清理过程中出现异常（已隔离，不影响交易）: {e}")
        finally:
            try:
                session.close()
            except Exception:
                pass
