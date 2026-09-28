"""
P1-1: Trade 并发安全锁管理器

为每个 trade.id 提供独立的 asyncio.Lock，确保：
1. PositionSync 和 ExitManager 不会同时修改同一个 trade
2. 锁粒度为 trade.id，不是全局锁
3. 所有修改 trade/order/state 的操作必须持有锁
"""
import asyncio
from typing import Dict
from loguru import logger


class TradeLockManager:
    """
    Trade 锁管理器

    使用方式：
        lock_manager = TradeLockManager()
        async with lock_manager.acquire(trade_id):
            # 安全地修改 trade
            trade.amount = new_amount
            session.commit()
    """

    def __init__(self):
        self._locks: Dict[int, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()  # 保护 _locks 字典本身

    async def acquire(self, trade_id: int) -> asyncio.Lock:
        """获取指定 trade_id 的锁"""
        async with self._global_lock:
            if trade_id not in self._locks:
                self._locks[trade_id] = asyncio.Lock()
            return self._locks[trade_id]

    async def release(self, trade_id: int):
        """释放指定 trade_id 的锁（可选，Lock 会自动释放）"""
        # asyncio.Lock 不需要手动释放，acquire/release 通过 async with 管理
        pass

    async def cleanup(self, trade_id: int):
        """清理已关闭 trade 的锁（可选，防止内存泄漏）"""
        async with self._global_lock:
            if trade_id in self._locks:
                del self._locks[trade_id]
                logger.debug(f"[TradeLock] 清理 trade_id={trade_id} 的锁")


# 全局单例
trade_lock_manager = TradeLockManager()
