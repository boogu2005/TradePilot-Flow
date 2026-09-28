"""
基础退出检查器 — 所有 checker 继承此类。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional

from .models import ExitResult, ExitMode


class BaseExitChecker(ABC):
    """退出检查器基类。"""

    name: str = ""

    def __init__(self, config: dict):
        self.config = config

    def should_run(self, exit_mode: ExitMode, position_state: str) -> bool:
        """
        是否应该运行此检查器。
        子类重写以控制在不同 ExitMode 下的行为。
        """
        return True

    @abstractmethod
    async def check(self, trade, exchange: str, session, current_price: float | None = None) -> Optional[ExitResult]:
        """
        执行退出检查。返回 ExitResult 或 None。
        current_price 由 ExitManager 统一传入（复用 fetch_ticker 结果）。
        """
        ...
