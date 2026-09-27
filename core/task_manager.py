"""
任务管理器 — 统一注册 / 取消 / 等待所有后台任务。

解决问题：
- asyncio.create_task() 创建后丢失引用 → 资源泄漏
- 任务异常无人知晓 → 静默失败
- 退出时任务未取消 → event loop 关闭报错

特性：
1. 注册任务时指定名称（唯一）
2. 自动监督：异常退出后指数退避重启
3. 退出时 cancel_all() + wait_all() 确保干净退出
4. stats() 返回各任务状态（用于看门狗）
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional
from loguru import logger

from .states import ServiceRegistry, ModuleState


class TaskManager:
    """统一后台任务管理。"""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._factories: dict[str, Callable[[], Awaitable]] = {}  # 用于重启

    # —————— 创建 / 注册 ——————

    async def create(
        self,
        name: str,
        coro_factory: Callable[[], Awaitable],
        *,
        autorestart: bool = True,
        max_retries: int = 10,
        base_delay: float = 5.0,
        max_delay: float = 60.0,
    ) -> asyncio.Task:
        """
        创建并注册一个任务。

        参数:
            name: 任务名称（唯一，重复创建会先取消旧的）
            coro_factory: 无参函数，返回协程对象（用于重启）
            autorestart: 异常退出后是否自动重启
            max_retries: 最大重试次数
            base_delay: 首次重试延迟（秒）
            max_delay: 重试延迟上限（秒）
        """
        # 如果同名任务还在运行，先取消
        if name in self._tasks and not self._tasks[name].done():
            logger.warning(f"[TaskManager] 任务 {name} 已存在，先取消旧的")
            self._tasks[name].cancel()
            try:
                await asyncio.wait_for(self._tasks[name], timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        self._factories[name] = coro_factory

        if autorestart:
            task = asyncio.create_task(
                self._supervise(name, coro_factory, max_retries, base_delay, max_delay),
                name=name,
            )
        else:
            task = asyncio.create_task(coro_factory(), name=name)

        self._tasks[name] = task
        logger.info(f"[TaskManager] 注册任务: {name} (autorestart={autorestart})")
        return task

    async def _supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable],
        max_retries: int,
        base_delay: float,
        max_delay: float,
    ) -> None:
        """监督循环：异常退出后指数退避重启。"""
        for attempt in range(1, max_retries + 1):
            try:
                await factory()
                # 正常退出
                logger.info(f"[TaskManager] 任务 {name} 正常退出")
                return
            except asyncio.CancelledError:
                logger.info(f"[TaskManager] 任务 {name} 被取消")
                return
            except Exception as e:
                logger.exception(f"[TaskManager] 任务 {name} 异常退出 (第{attempt}/{max_retries}次): {e}")

            if attempt < max_retries:
                delay = min(base_delay * (1.5 ** (attempt - 1)), max_delay)
                logger.info(f"[TaskManager] 任务 {name} 将在 {delay:.0f}s 后重启...")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
            else:
                logger.error(f"[TaskManager] 任务 {name} 已达最大重试次数 ({max_retries})，停止重启")
                # 标记对应模块为 FAILED
                _try_mark_failed(name)

    # —————— 取消 / 等待 ——————

    def cancel(self, name: str) -> None:
        """取消指定任务。"""
        task = self._tasks.get(name)
        if task and not task.done():
            task.cancel()

    async def wait(self, name: str, timeout: float = 10.0) -> None:
        """等待指定任务结束。"""
        task = self._tasks.get(name)
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def cancel_all(self, timeout: float = 10.0) -> None:
        """取消所有任务并等待结束（退出时调用）。"""
        # 第一阶段：全部 cancel
        for name, task in self._tasks.items():
            if not task.done():
                logger.info(f"[TaskManager] 取消任务: {name}")
                task.cancel()

        # 第二阶段：等待
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[TaskManager] 部分任务 {timeout}s 内未结束")

        self._tasks.clear()
        self._factories.clear()
        logger.info("[TaskManager] 所有任务已清理")

    # —————— 查询 ——————

    def stats(self) -> dict[str, str]:
        """返回各任务状态。"""
        result = {}
        for name, task in self._tasks.items():
            if task.done():
                if task.cancelled():
                    result[name] = "cancelled"
                elif task.exception():
                    result[name] = f"error: {type(task.exception()).__name__}"
                else:
                    result[name] = "done"
            else:
                result[name] = "running"
        return result

    def is_running(self, name: str) -> bool:
        """检查任务是否正在运行。"""
        task = self._tasks.get(name)
        return task is not None and not task.done()


# —————— 辅助函数 ——————

_TASK_TO_MODULE_MAP = {
    "订单监控器": "order_monitor",
    "信号消费者": "signal_consumer",
    "看门狗": None,
    "仓位同步器": "position_sync",
    "退出管理器": "exit_manager",
    "ExchangeSupervisor": "exchange",
}


def _try_mark_failed(task_name: str) -> None:
    """任务达到最大重试后，标记对应模块为 FAILED。"""
    module = _TASK_TO_MODULE_MAP.get(task_name)
    if module:
        ServiceRegistry.set_state(module, ModuleState.FAILED)
