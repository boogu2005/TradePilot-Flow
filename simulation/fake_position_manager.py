"""
FakePositionManager - 模拟 PositionManager
提供与 position_manager.py 相同的接口，用于快照管理
"""
from __future__ import annotations
import asyncio
import time
from typing import Optional
from loguru import logger

from .fake_exchange import FakeExchange


class FakePositionManager:
    """
    模拟 PositionManager

    提供与 position_manager.py 相同的接口：
    - start(): 启动管理器
    - stop(): 停止管理器
    - get_position(): 获取单个仓位
    - get_snapshot(): 获取快照
    - get_snapshot_raw(): 获取原始快照
    - is_healthy(): 检查健康状态
    - get_age(): 获取缓存年龄
    - get_metadata(): 获取元数据
    - rest_refresh(): REST 刷新
    """

    def __init__(self, exchange: FakeExchange):
        """
        初始化模拟 PositionManager

        Args:
            exchange: FakeExchange 实例
        """
        self._exchange = exchange
        self._snapshot: list[dict] = []
        self._snapshot_time: float = 0
        self._healthy: bool = True
        self._started: bool = True  # 模拟模式默认为已启动
        self._refresh_count: int = 0
        self._failure_count: int = 0
        self._ws_connected: bool = True  # 模拟 WS 连接

        # 刷新锁
        self._refresh_lock = asyncio.Lock()

        logger.info("[FakePositionManager] 初始化完成")

    async def start(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        exchange_name: str = "okx",
    ) -> None:
        """
        启动 PositionManager

        Args:
            api_key: API Key（模拟环境忽略）
            api_secret: API Secret（模拟环境忽略）
            passphrase: Passphrase（模拟环境忽略）
            exchange_name: 交易所名称（模拟环境忽略）
        """
        if self._started:
            logger.warning("[FakePositionManager] 已经启动")
            return

        self._started = True
        self._ws_connected = True

        # 初始刷新
        await self.rest_refresh()

        logger.success("[FakePositionManager] 启动完成")

    async def stop(self) -> None:
        """停止 PositionManager"""
        if not self._started:
            return

        self._started = False
        self._ws_connected = False
        logger.info("[FakePositionManager] 已停止")

    def get_position(self, symbol: str, side: Optional[str] = None) -> Optional[dict]:
        """
        获取单个仓位

        Args:
            symbol: 交易对
            side: 方向（"long" or "short"）

        Returns:
            仓位数据
        """
        for pos in self._snapshot:
            if pos["symbol"] == symbol:
                if side is None or pos["side"] == side:
                    return pos
        return None

    def get_snapshot(self) -> dict[tuple[str, str], dict]:
        """
        获取快照（字典格式）

        Returns:
            {(symbol, side): position_data}
        """
        result = {}
        for pos in self._snapshot:
            key = (pos["symbol"], pos["side"])
            result[key] = pos
        return result

    def get_snapshot_raw(self) -> list[dict]:
        """
        获取原始快照（列表格式）

        Returns:
            [position_data, ...]
        """
        return self._snapshot.copy()

    def is_healthy(self) -> bool:
        """
        检查健康状态

        Returns:
            是否健康
        """
        if not self._started:
            return False

        if not self._ws_connected:
            return False

        # 如果有仓位，检查缓存是否过期
        if self._snapshot:
            age = time.time() - self._snapshot_time
            return age < 20.0  # 20秒内认为健康

        # 空仓时只要 WS 连接就健康
        return True

    def get_age(self) -> float:
        """
        获取缓存年龄（秒）

        Returns:
            缓存年龄
        """
        if self._snapshot_time == 0:
            return float('inf')
        return time.time() - self._snapshot_time

    def get_metadata(self) -> dict:
        """
        获取元数据

        Returns:
            元数据字典
        """
        return {
            "refresh_count": self._refresh_count,
            "failure_count": self._failure_count,
            "ws_connected": self._ws_connected,
            "idle": len(self._snapshot) == 0,
            "version": int(time.time()),
            "last_refresh": self._snapshot_time,
            "healthy": self.is_healthy(),
            "last_error": None,
        }

    async def rest_refresh(self) -> bool:
        """
        REST 刷新（从 FakeExchange 获取最新仓位）

        Returns:
            是否成功
        """
        async with self._refresh_lock:
            try:
                positions = await self._exchange.fetch_positions()
                self._snapshot = positions
                self._snapshot_time = time.time()
                self._refresh_count += 1

                # REST 刷新成功后恢复连接状态
                if not self._ws_connected:
                    self._ws_connected = True
                    logger.info("[FakePositionManager] REST 刷新成功，恢复连接状态")

                logger.debug(f"[FakePositionManager] 刷新成功: {len(positions)} 个仓位")
                return True
            except Exception as e:
                self._failure_count += 1
                logger.warning(f"[FakePositionManager] 刷新失败: {e}")
                return False

    def exit_idle(self) -> None:
        """退出 Idle 模式（模拟）"""
        # 模拟环境不需要 Idle 模式
        pass

    def set_healthy(self, healthy: bool) -> None:
        """
        设置健康状态（用于测试）

        Args:
            healthy: 是否健康
        """
        self._healthy = healthy
        if not healthy:
            self._ws_connected = False
        else:
            self._ws_connected = True

    def simulate_ws_disconnect(self) -> None:
        """模拟 WS 断线（用于测试）"""
        self._ws_connected = False
        logger.warning("[FakePositionManager] 模拟 WS 断线")

    def simulate_ws_reconnect(self) -> None:
        """模拟 WS 重连（用于测试）"""
        self._ws_connected = True
        logger.info("[FakePositionManager] 模拟 WS 重连")


# 全局单例（模拟 position_manager）
_position_manager: Optional[FakePositionManager] = None


def get_position_manager() -> Optional[FakePositionManager]:
    """获取全局 PositionManager 实例"""
    return _position_manager


def set_position_manager(pm: FakePositionManager) -> None:
    """设置全局 PositionManager 实例"""
    global _position_manager
    _position_manager = pm
