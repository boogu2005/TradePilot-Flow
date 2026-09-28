"""
数据库事务管理模块。

提供原子化状态转换的事务包装，确保：
- BEGIN transaction
- 修改所有字段
- COMMIT
- 异常时 ROLLBACK

P0-3: Trade 状态转换事务
P0-4: TP1 状态转换事务
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from loguru import logger
from sqlalchemy.orm import Session


@asynccontextmanager
async def atomic_transaction(session: Session, operation: str = "operation") -> AsyncGenerator[None, None]:
    """
    原子化事务包装器。

    用法：
        async with atomic_transaction(session, "TP1状态转换"):
            trade.position_state = "partial_tp"
            trade.amount = new_amount
            session.commit()

    异常时自动 rollback，成功时自动 commit。
    """
    try:
        # 开始事务（SQLAlchemy 默认自动开始，这里显式标记）
        logger.debug(f"[事务] 开始: {operation}")
        yield
        # 如果没有显式 commit，在这里提交
        if session.is_active:
            session.commit()
            logger.debug(f"[事务] 提交: {operation}")
    except Exception as e:
        # 异常时回滚
        if session.is_active:
            session.rollback()
            logger.warning(f"[事务] 回滚: {operation} - {e}")
        raise


async def execute_atomic(session: Session, operation: str, func):
    """
    执行原子化操作。

    用法：
        async def update_state():
            trade.position_state = "partial_tp"
            trade.amount = new_amount

        await execute_atomic(session, "TP1状态转换", update_state)
    """
    try:
        logger.debug(f"[事务] 开始: {operation}")
        result = await func()
        session.commit()
        logger.debug(f"[事务] 提交: {operation}")
        return result
    except Exception as e:
        if session.is_active:
            session.rollback()
            logger.warning(f"[事务] 回滚: {operation} - {e}")
        raise
