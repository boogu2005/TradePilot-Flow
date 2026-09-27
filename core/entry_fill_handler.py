"""
Entry Fill Handler — Event-driven SL/TP setup via EventBus.

Subscribes to position_update on EventBus.
When WS pushes a new position, immediately checks if the corresponding
Trade needs SL/TP protection and triggers ensure_protection.

This provides TRUE event-driven SL/TP creation with millisecond latency,
replacing the old approach of relying solely on:
  - Consumer loop post_entry_setup (one-shot, signal time)
  - order_monitor 5s poll (now a safety net)

v6 — Batch Fill Coordination:
  When a Trade uses limit_range strategy (batch limit orders), each partial
  fill triggers a WS position_update. This handler now detects position size
  CHANGES and triggers reconcile_on_batch_fill() to recalculate TP1 and SL
  using the latest OKX real position data.

Architecture:
  WS position_update ──→ EventBus.publish("position_update")
                             │
                             ├─→ PositionTracker（缓存更新）
                             └─→ EntryFillHandler（SL/TP 事件驱动）

Dedup: per-trade debounce (5s) + position-size-change detection.
The 5s order_monitor and 600s Reconciler remain as safety nets.
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from .runtime_events import event_bus

L = logger.bind(module="entry_fill")


class EntryFillHandler:
    """
    Event-driven SL/TP setup — reacts to WS position updates.

    When a position appears or changes on OKX, this handler immediately
    checks if the corresponding Trade in DB needs SL/TP protection and
    creates them without waiting for any polling cycle.

    v6: Detects position size CHANGES (not just first appearance) and
    triggers batch fill coordination for limit_range strategy trades.

    Safety features:
      - Per-trade debounce (5s) prevents duplicate triggers
      - Position-size-change detection avoids redundant calls
      - Double-checks _setup_done before and after acquiring trade_lock
      - Opens independent DB session per event (doesn't block other modules)
      - Fire-and-forget: uses asyncio.create_task to not block EventBus dispatch
    """

    def __init__(self):
        self._last_trigger: dict[int, float] = {}  # trade_id → timestamp
        self._last_contracts: dict[int, float] = {}  # trade_id → last known contracts
        self._debounce_s: float = 5.0

        event_bus.subscribe("position_update", self._on_position_update)
        L.info("[EntryFillHandler] 已注册 EventBus position_update 订阅 (v6 batch-fill aware)")

    def _norm(self, s: str) -> str:
        """Normalize any symbol format to short form: BTCUSDT"""
        return (s or "").upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")

    async def _on_position_update(self, data: dict) -> None:
        """
        WS position update callback.

        Uses asyncio.create_task to process in background — the EventBus
        dispatches synchronously and we must not block other subscribers
        (PositionTracker, etc.) with DB queries.
        """
        symbol = data.get("symbol", "")
        side = data.get("side", "long")
        contracts = data.get("contracts", 0)

        if contracts <= 0:
            # Position closed / empty — clear tracking
            # We can't clear from here because we don't know the trade_id yet
            return

        # Fire-and-forget: don't block the EventBus dispatch loop
        asyncio.create_task(self._handle_new_position(symbol, side, contracts))

    async def _handle_new_position(self, symbol: str, side: str, contracts: float) -> None:
        """
        Open a DB session, find matching trade, trigger protection.

        v6: Detects position size changes for batch fill coordination.
        - First appearance → ensure_protection() (initial setup)
        - Position increased → reconcile_on_batch_fill() (recalculate TP1/SL)
        - Position unchanged → skip (no-op)

        This runs as a background task spawned by the EventBus callback.
        Uses its own DB session — no dependency on any calling context.
        """
        from database.db import get_session
        from database.models import Trade
        from core.reconciler import ensure_protection, reconcile_on_batch_fill
        from core.trade_lock import trade_lock_manager

        session = get_session()
        try:
            # ———— Find matching open trade ————
            trade = self._find_trade(session, symbol, side)
            if trade is None:
                return  # No DB trade for this position (manual trade, recovery will handle)
            if not trade.is_open or trade.amount <= 0:
                return

            # ———— v6: Track position size changes ————
            last_contracts = self._last_contracts.get(trade.id, 0)
            is_first_seen = (last_contracts == 0)
            position_increased = (contracts > last_contracts * 1.001)  # 0.1% tolerance for rounding

            # ———— Debounce check ————
            # 首次出现(初始保护)需要防抖，避免重复设置
            # 但 position_increased (分批成交) 是独立事件，不应被防抖阻塞
            now = time.time()
            last = self._last_trigger.get(trade.id, 0)
            if is_first_seen and now - last < self._debounce_s:
                return  # Too soon since last trigger
            self._last_trigger[trade.id] = now

            # ———— Acquire trade lock + re-check ————
            lock = await trade_lock_manager.acquire(trade.id)
            async with lock:
                # Re-read from DB to get latest signal_meta
                session.refresh(trade, attribute_names=["signal_meta"])
                meta = trade.signal_meta or {}

                if is_first_seen:
                    # ———— First position appearance → initial protection setup ————
                    if meta.get("_setup_done"):
                        # Already done by consumer loop or previous event,
                        # but record the contracts for future batch fill detection
                        self._last_contracts[trade.id] = contracts
                        return

                    # SOL fix: 确保 position_state 从 pending_entry → open
                    # 让 TP1Checker 能正常运行（仅在 pending_entry 时转换）
                    if trade.position_state == "pending_entry":
                        trade.position_state = "open"
                        L.info(f"[EntryFill] {trade.pair} pending_entry → open (EntryFillHandler)")

                    L.info(
                        f"[EntryFill] 首次仓位出现 → 初始化保护: {trade.pair} "
                        f"{'SHORT' if trade.is_short else 'LONG'} "
                        f"contracts={contracts}"
                    )
                    await ensure_protection(trade, session)
                    self._last_contracts[trade.id] = contracts

                elif position_increased:
                    # ———— v6: Position size increased → batch fill coordination ————
                    entry_strategy = meta.get("entry_strategy", "")
                    L.info(
                        f"[EntryFill] 仓位增长检测: {trade.pair} "
                        f"{'SHORT' if trade.is_short else 'LONG'} "
                        f"contracts: {last_contracts} → {contracts} "
                        f"strategy={entry_strategy}"
                    )
                    await reconcile_on_batch_fill(trade, session, contracts)
                    self._last_contracts[trade.id] = contracts

                elif contracts < last_contracts * 0.999:
                    # Position decreased — could be TP1 fill or partial close
                    # Update tracking but don't interfere (order_monitor handles this)
                    self._last_contracts[trade.id] = contracts
                    L.debug(
                        f"[EntryFill] 仓位减少: {trade.pair} "
                        f"contracts: {last_contracts} → {contracts} (由 order_monitor 处理)"
                    )
                else:
                    # Position unchanged — just update tracking
                    self._last_contracts[trade.id] = contracts

        except Exception as e:
            L.error(f"[EntryFill] {symbol} {side} 处理失败: {type(e).__name__}: {e}")
        finally:
            session.close()

    def _find_trade(self, session, symbol: str, side: str):
        """
        Match a WS position (symbol + side) to an open DB Trade.

        Uses normalized symbol comparison to handle all format variants:
          BTC-USDT-SWAP / BTC/USDT:USDT / BTCUSDT → BTCUSDT
        """
        from database.models import Trade

        norm = self._norm(symbol)
        is_short = (side == "short")

        for t in Trade.get_active_trades(session):
            t_norm = self._norm(t.pair)
            if t_norm == norm and t.is_short == is_short:
                return t
        return None


# Global singleton — instantiated on first import
entry_fill_handler = EntryFillHandler()