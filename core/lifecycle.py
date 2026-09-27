"""
AppLifecycle — 应用生命周期管理器。

功能：
  1. 所有异步资源统一注册（exchange / DB / Telegram / aiohttp session）
  2. 关闭时按注册顺序的逆序执行清理（后开先关）
  3. 每个资源都有超时保护，避免一个资源卡死导致全部阻塞
  4. 支持注册回调（如关闭前通知 Telegram、取消挂单等）
  5. 详尽日志 — 每个资源释放都输出 [SHUTDOWN] 行
  6. 幂等 — 多次调用 shutdown 安全

用法：
  lifecycle = AppLifecycle()
  lifecycle.register("okx", lambda: ex.close_all_exchanges())
  lifecycle.register("db", lambda: close_db())

  # 程序退出时：
  await lifecycle.shutdown()

输出：
  [SHUTDOWN] 开始关闭 ...（N 个资源）
  [SHUTDOWN]  ✔ okx         已释放  (125ms)
  [SHUTDOWN]  ✔ db          已释放  (5ms)
  [SHUTDOWN]  ✅ 所有资源已释放
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import Callable, Optional
from loguru import logger

# 异步清理函数签名：() -> Awaitable[None]
_AsyncCleanup = Callable[[], Awaitable[None]]


class AppLifecycle:
    """应用生命周期管理器（单例模式）。"""

    _instance: Optional[AppLifecycle] = None

    @classmethod
    def get_instance(cls) -> AppLifecycle:
        """获取全局单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅测试用）。"""
        cls._instance = None

    def __init__(self):
        """初始化资源注册表。"""
        self._resources: list[_ResourceEntry] = []
        self._shutdown_started = False
        self._shutdown_complete = False

    # ———————— 注册 ————————

    def register(
        self,
        name: str,
        cleanup: _AsyncCleanup,
        *,
        timeout: float = 10.0,
        depends_on: Optional[list[str]] = None,
    ) -> None:
        """
        注册一个资源。

        参数:
            name:     资源名称（用于日志）
            cleanup:  异步清理函数
            timeout:  清理超时（秒）
            depends_on: 依赖的资源列表（当前仅用于日志提示）
        """
        self._resources.append(_ResourceEntry(
            name=name,
            cleanup=cleanup,
            timeout=timeout,
            depends_on=depends_on or [],
        ))
        logger.debug(f"[生命周期] 已注册资源: {name}")

    # ———————— 关闭 ————————

    async def shutdown(self) -> None:
        """
        关闭所有已注册的资源。

        关闭顺序 = 注册顺序的逆序（后注册的先关闭）。
        每个资源都有超时保护。
        幂等：多次调用安全，第二次开始直接返回。
        """
        # 幂等守卫
        if self._shutdown_complete:
            return
        if self._shutdown_started:
            # 已经在关闭中 — 等待完成
            while not self._shutdown_complete:
                await asyncio.sleep(0.1)
            return
        self._shutdown_started = True

        count = len(self._resources)
        logger.info(f"[SHUTDOWN] ═══ 开始关闭（{count} 个资源）═══")

        # 按注册逆序关闭
        success_count = 0
        fail_count = 0
        for entry in reversed(self._resources):
            ok, elapsed = await self._close_one(entry)
            if ok:
                success_count += 1
            else:
                fail_count += 1

        self._shutdown_complete = True

        if fail_count == 0:
            logger.success(f"[SHUTDOWN] ✅ 所有 {count} 个资源已释放")
        else:
            logger.warning(f"[SHUTDOWN] ⚠️  {success_count} 成功 / {fail_count} 失败（共 {count} 个资源）")

    async def _close_one(self, entry: _ResourceEntry) -> tuple[bool, float]:
        """关闭单个资源，带超时 + 日志。支持 sync 和 async 清理函数。"""
        start = time.perf_counter()
        try:
            result = entry.cleanup()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=entry.timeout)
            elapsed = (time.perf_counter() - start) * 1000
            logger.success(f"[SHUTDOWN]  ✔ {entry.name:<12} 已释放  ({elapsed:.0f}ms)")
            return True, elapsed
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            logger.warning(f"[SHUTDOWN]  ⏱ {entry.name:<12} 超时 ({entry.timeout}s)")
            return False, elapsed
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            logger.warning(f"[SHUTDOWN]  ✗ {entry.name:<12} 释放异常: {e}")
            return False, elapsed

    # ———————— 查询 ————————

    @property
    def resource_count(self) -> int:
        """已注册的资源数量。"""
        return len(self._resources)

    def list_resources(self) -> list[str]:
        """返回所有已注册的资源名称列表。"""
        return [r.name for r in self._resources]

    def is_shutting_down(self) -> bool:
        """是否正在关闭中。"""
        return self._shutdown_started

    def is_shutdown_complete(self) -> bool:
        """是否已完成关闭。"""
        return self._shutdown_complete


class _ResourceEntry:
    """内部资源条目。"""
    __slots__ = ("name", "cleanup", "timeout", "depends_on")

    def __init__(
        self,
        name: str,
        cleanup: _AsyncCleanup,
        timeout: float,
        depends_on: list[str],
    ):
        self.name = name
        self.cleanup = cleanup
        self.timeout = timeout
        self.depends_on = depends_on
