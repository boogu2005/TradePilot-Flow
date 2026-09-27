"""
模块状态管理 — 统一追踪所有子系统运行状态。

状态枚举：
  STOPPED        未启动
  INITIALIZING   初始化中
  CONNECTED      已连接（Exchange / Telegram）
  CONNECTING     连接中 / 重连中（Exchange）
  DISCONNECTED   已断开（Exchange / Telegram）
  RUNNING        运行中（服务类模块）
  PAUSED         已暂停（服务类模块，等待依赖恢复）
  FAILED         初始化失败

状态流转：
  Exchange:  STOPPED → INITIALIZING → CONNECTED ⇄ DISCONNECTED → CONNECTING → CONNECTED
  Telegram:  STOPPED → INITIALIZING → CONNECTED → DISCONNECTED
  服务:      STOPPED → RUNNING ⇄ PAUSED → STOPPED

回调机制：
  当模块状态变化时，触发所有注册的回调，用于联动暂停/恢复。
"""
from __future__ import annotations

from enum import Enum
from typing import Callable, Optional
from loguru import logger


class ModuleState(Enum):
    STOPPED = "stopped"
    INITIALIZING = "initializing"
    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"


# 模块名称常量（避免拼写错误）
MOD_EXCHANGE = "exchange"
MOD_TELEGRAM = "telegram"
MOD_POSITION_SYNC = "position_sync"
MOD_EXIT_MANAGER = "exit_manager"
MOD_ORDER_MONITOR = "order_monitor"
MOD_SIGNAL_CONSUMER = "signal_consumer"


# 回调类型：(module_name, old_state, new_state) -> None
StateCallback = Callable[[str, ModuleState, ModuleState], None]


class ServiceRegistry:
    """
    全局模块状态注册表（类变量共享，无需实例化）。

    线程安全说明：
    - Python asyncio 单线程，状态读写无竞态
    - 回调在设置状态的协程中同步执行（不跨线程）
    """

    _states: dict[str, ModuleState] = {}
    _callbacks: dict[str, list[StateCallback]] = {}

    @classmethod
    def set_state(cls, module: str, state: ModuleState) -> None:
        """设置模块状态，触发回调。"""
        old = cls._states.get(module, ModuleState.STOPPED)
        cls._states[module] = state
        if old != state:
            level = "SUCCESS" if state in (ModuleState.CONNECTED, ModuleState.RUNNING) else "WARNING"
            color = {"SUCCESS": "<green>", "WARNING": "<yellow>"}.get(level, "<blue>")
            logger.log(level, f"{color}[状态] {module}: {old.value} → {state.value}")
            cls._notify(module, old, state)

    @classmethod
    def get_state(cls, module: str) -> ModuleState:
        """获取模块当前状态。"""
        return cls._states.get(module, ModuleState.STOPPED)

    @classmethod
    def is_connected(cls, module: str) -> bool:
        """检查模块是否已连接（Exchange / Telegram）。"""
        return cls.get_state(module) == ModuleState.CONNECTED

    @classmethod
    def is_running(cls, module: str) -> bool:
        """检查服务模块是否正在运行。"""
        return cls.get_state(module) == ModuleState.RUNNING

    @classmethod
    def can_trade(cls) -> bool:
        """检查是否可以执行交易（Exchange 已连接）。"""
        return cls.is_connected(MOD_EXCHANGE)

    @classmethod
    def on_state_change(cls, module: str, callback: StateCallback) -> None:
        """注册状态变化回调。"""
        cls._callbacks.setdefault(module, []).append(callback)

    @classmethod
    def _notify(cls, module: str, old: ModuleState, new: ModuleState) -> None:
        """触发模块的状态变化回调。"""
        for cb in cls._callbacks.get(module, []):
            try:
                cb(module, old, new)
            except Exception as e:
                logger.warning(f"[状态回调] {module} 回调异常: {e}")

    @classmethod
    def snapshot(cls) -> dict[str, str]:
        """返回所有模块状态快照（用于日志/看门狗）。"""
        return {m: s.value for m, s in cls._states.items()}

    @classmethod
    def reset(cls) -> None:
        """重置所有状态（测试用）。"""
        cls._states.clear()
        cls._callbacks.clear()
