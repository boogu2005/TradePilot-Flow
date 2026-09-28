"""
退出管理器 — 状态驱动，按仓位生命周期分派退出检查器。
⚠️ 只管理自动策略（TP1/TP2/Trailing/ROI/MaxHold），不处理老师手动操作。

======================= 状态机 =======================
PENDING_ENTRY → 不检查（等待 OKX 成交）
OPEN          → MaxLoss + StopLoss + TP1 + ROI(时间到) + MaxHold(时间到)
TP1_FILLED    → MaxLoss + StopLoss + TP2 + ROI(时间到) + MaxHold(时间到)
PARTIAL_TP    → MaxLoss + StopLoss + Trailing + ROI(时间到) + MaxHold(时间到)
CLOSED        → 不检查（已平仓）

老师 Close/Update/Cancel 绕过 ExitManager，直接在 signal_updater 中执行。

======================= 检查器依赖关系 =======================
MaxLoss:    OPEN + TP1_FILLED + PARTIAL_TP（最高优先级，浮亏安全网）
StopLoss:   OPEN + TP1_FILLED + PARTIAL_TP（始终运行，保护全部仓位）
TP1:        仅 OPEN（成交后状态变为 TP1_FILLED）
TP2:        仅 TP1_FILLED（成交后状态变为 PARTIAL_TP）
Trailing:   仅 PARTIAL_TP（依赖 TP2 成交）
ROI:        OPEN + TP1_FILLED + PARTIAL_TP（基于开仓时间，不依赖 TP1/TP2）
MaxHold:    OPEN + TP1_FILLED + PARTIAL_TP（基于开仓时间，不依赖 TP1/TP2）
=============================================================

======================= 速率限制优化 =======================
ExitManager 每次 check() 只 fetch_ticker 一次，传入所有检查器共享。
============================================================
"""
from __future__ import annotations

from loguru import logger

from .models import ExitResult, ExitMode, PositionState
from .stop_loss import StopLossChecker
from .tp1 import TP1Checker
from .tp2 import TP2Checker
from .trailing import TrailingChecker
from .roi import ROIChecker
from .max_hold import MaxHoldChecker
from .max_loss import MaxLossChecker


class ExitManager:
    """
    退出管理器 — 状态驱动调度。

    根据 position_state 和时间条件动态决定运行哪些检查器。
    一个仓位只有一个退出管理者，确保不重复平仓。
    """

    def __init__(self, config: dict):
        self.config = config
        # 检查器实例（复用）
        self._stoploss = StopLossChecker(config)
        self._tp1 = TP1Checker(config)
        self._tp2 = TP2Checker(config)
        self._trailing = TrailingChecker(config)
        self._roi = ROIChecker(config)
        self._max_hold = MaxHoldChecker(config)
        self._max_loss = MaxLossChecker(config)

    def _get_state(self, trade) -> str:
        """安全获取 position_state — 默认 PENDING_ENTRY 避免误触发 TP1"""
        if hasattr(trade, 'position_state') and trade.position_state:
            return trade.position_state
        return "pending_entry"

    def _get_exit_mode(self, trade) -> ExitMode:
        """安全获取 exit_mode"""
        if hasattr(trade, 'exit_mode') and trade.exit_mode:
            try:
                return ExitMode(trade.exit_mode)
            except ValueError:
                pass
        meta = trade.signal_meta or {}
        try:
            return ExitMode(meta.get("exit_mode", "auto"))
        except ValueError:
            return ExitMode.AUTO

    def _get_checkers(self, trade, pos_state: str) -> list:
        """
        根据仓位状态动态决定运行哪些检查器。

        依赖关系：
        - MaxLoss:   OPEN + TP1_FILLED + PARTIAL_TP（始终运行，最高优先级 — 浮亏安全网）
        - StopLoss:  OPEN + TP1_FILLED + PARTIAL_TP（始终运行）
        - TP1:       仅 OPEN
        - TP2:       仅 TP1_FILLED
        - Trailing:  仅 PARTIAL_TP（依赖 TP2 成交）
        - ROI:       OPEN + TP1_FILLED + PARTIAL_TP（基于时间，不依赖 TP1/TP2）
        - MaxHold:   OPEN + TP1_FILLED + PARTIAL_TP（基于时间，不依赖 TP1/TP2）

        动态排序: 当 Trailing 激活时，TrailingChecker 排在 StopLoss 前面，
        使其能独立检测自身触发条件并返回 exit_type="trailing"。
        """
        checkers = []

        # 0. MaxLoss: 最高优先级 — 浮亏超过阈值立即强制平仓
        # 作为最终安全网，在所有其他退出检查器之前运行
        checkers.append(self._max_loss)

        # partial_tp: TrailingChecker 始终优先于 StopLossChecker
        # TP2 成交后先让 Trailing 检查/激活，再运行 StopLoss。
        # 如果 Trailing 未激活时先跑 StopLoss，价格稍回保本价就触发全平，
        # Trailing 永远没机会激活。
        if pos_state == PositionState.PARTIAL_TP_DONE.value:
            checkers.append(self._trailing)

        # 1. StopLoss: 始终运行（OPEN / TP1_FILLED / PARTIAL_TP）
        checkers.append(self._stoploss)

        # 2. TP1: 仅 OPEN 状态
        if pos_state == PositionState.OPEN.value:
            checkers.append(self._tp1)

        # 3. TP2: 仅 TP1_FILLED 状态
        if pos_state == PositionState.TP1_FILLED.value:
            checkers.append(self._tp2)

        # 4. ROI: OPEN + TP1_FILLED + PARTIAL_TP（基于开仓时间，不依赖 TP1/TP2）
        checkers.append(self._roi)

        # 5. MaxHold: OPEN + TP1_FILLED + PARTIAL_TP（基于开仓时间，不依赖 TP1/TP2）
        checkers.append(self._max_hold)

        return checkers

    async def check(self, trade, exchange: str, session) -> ExitResult | None:
        """
        状态驱动的退出检查。

        PENDING_ENTRY → 不检查
        OPEN          → StopLoss + TP1 + ROI(时间到) + MaxHold(时间到)
        TP1_FILLED    → StopLoss + TP2 + ROI(时间到) + MaxHold(时间到)
        PARTIAL_TP    → StopLoss + Trailing + ROI(时间到) + MaxHold(时间到)

        ⚠️ 老师 Close/Update/Cancel 不经过 ExitManager，直接执行。
        """
        pos_state = self._get_state(trade)
        exit_mode = self._get_exit_mode(trade)

        # ———— PENDING_ENTRY: 不检查任何退出策略 ————
        if pos_state == PositionState.PENDING_ENTRY.value:
            return None

        # ———— CLOSED: 不检查 ————
        if pos_state == PositionState.CLOSED.value:
            return None

        # ———— 动态获取检查器列表 ————
        checkers = self._get_checkers(trade, pos_state)

        # ———— 统一 fetch_ticker via Runtime，所有 checker 共享 ————
        current_price = None
        try:
            from core.exchange_runtime import runtime
            ticker = await runtime.fetch_ticker(trade.pair)
            current_price = ticker.get("last")
        except Exception:
            pass

        # ———— 链式检查（只有一个 checker 能返回退出） ————
        for checker in checkers:
            try:
                result = await checker.check(trade, exchange, session, current_price=current_price)
            except Exception as e:
                logger.warning(f"[ExitManager] {checker.name} 检查异常: {e}")
                continue
            if result and result.should_exit:
                return result

        return None
