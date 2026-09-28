"""
订单管理器 — 复刻 Freqtrade freqtradebot.py 的订单管理逻辑。
包括：订单状态追踪、止损挂载/更新/追踪、入场单状态检查。

多交易所支持：根据 trade.exchange 路由到对应交易所。

======================= OKX = 唯一真相源 =======================
所有 TP/SL 数量：
1. 实时 fetch_position() 获取 OKX 实际持仓
2. normalize_order_amount() 校验精度/最小值
3. 数据库仅作为缓存，下单禁止使用 trade.amount
================================================================
"""
from __future__ import annotations

from datetime import datetime, timezone

from utils.time import ensure_utc

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade, Order
from core.exchange_runtime import runtime

# 日志去重：同一消息 8 小时内最多打印一次
_LOG_DEDUP: dict[str, float] = {}
_LOG_DEDUP_TTL = 8 * 3600


def _norm_amt(symbol: str, amount: float, exchange: str = "okx") -> float | None:
    from exchange_engine.exchange import normalize_order_amount
    return normalize_order_amount(symbol, amount, exchange)


def _log_once_8h(key: str, msg: str, level: str = "info"):
    now = datetime.now(timezone.utc).timestamp()
    last = _LOG_DEDUP.get(key, 0)
    if now - last < _LOG_DEDUP_TTL:
        return
    _LOG_DEDUP[key] = now
    if level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.info(msg)


# ———— update_trade_state ————
async def update_trade_state(
    trade: Trade,
    order_id: str,
    session: Session,
    stoploss_order: bool = False,
) -> bool:
    """根据交易所订单状态更新 Trade。返回 True 表示订单已被取消且未成交。"""
    trade_ex = trade.exchange or "okx"
    try:
        order = runtime.get_order(order_id)
    except Exception as e:
        logger.warning(f"[{trade_ex}] 无法获取订单 {order_id}: {e}")
        return False

    if order is None:
        return False

    trade.update_order(order)

    if order.get("status") in ("canceled", "cancelled") and (order.get("filled", 0) or 0) == 0:
        return True

    order_obj = trade.select_order_by_id(order_id)
    if order_obj is None:
        return False

    await _handle_order_fee(trade, order_obj, order)
    trade.update_trade(order_obj)
    session.commit()
    return False


async def _handle_order_fee(trade: Trade, order_obj: Order, order: dict):
    """处理订单手续费"""
    if order.get("fee") and order.get("status") not in ("open",):
        fee = order["fee"]
        if isinstance(fee, dict) and fee.get("cost"):
            side = order.get("side", "")
            is_entry = (side == trade.entry_side)
            if is_entry and trade.fee_open_currency is None:
                trade.fee_open_cost = fee.get("cost", 0)
                trade.fee_open_currency = fee.get("currency", "")
            elif not is_entry and trade.fee_close_currency is None:
                trade.fee_close_cost = fee.get("cost", 0)
                trade.fee_close_currency = fee.get("currency", "")


# ———— 入场单状态追踪 ————
async def check_entry_order(trade: Trade, session: Session) -> bool:
    """检查入场单是否已成交。返回 True 表示入场已完全成交。"""
    if not trade.has_open_orders:
        return trade.has_open_position

    for order_obj in trade.open_orders:
        if order_obj.ft_order_side != trade.entry_side:
            continue
        cancelled = await update_trade_state(trade, order_obj.order_id, session)
        if cancelled:
            trade_ex = trade.exchange or "okx"
            logger.warning(f"[{trade_ex}] 入场单已被取消且未成交: {trade.pair}")

    if not trade.has_open_position and not trade.has_open_orders and trade.amount == 0:
        trade.is_open = False
        trade.exit_reason = "entry_cancelled"
        trade.close_date = datetime.now(timezone.utc)
        trade.position_state = "closed"
        logger.warning(f"[{trade.exchange}] 僵尸清理 {trade.pair} 入场单全部取消")

    return trade.has_open_position and not trade.has_open_orders


# ———— 止盈管理（OKX 真相源）————
async def handle_take_profit_orders(trade: Trade, session: Session, tp_levels=None):
    """
    挂载系统默认 TP1 止盈单（2%盈利，50%仓位）。

    委托给 ProtectionManager.create_tp()，状态机保证幂等性。
    同一 trade 绝不会创建第二个 TP1。

    ===== 只创建 TP1，忽略所有外部 tp_levels =====
    teacher_tp 仅用于日志展示，不参与交易决策。
    ===============================================
    """
    from exit.protection import create_tp
    await create_tp(trade, session)


# ———— TP1 保本损 ————
async def check_tp1_breakeven(trade: Trade, session: Session) -> bool:
    """
    检查 TP1 是否已成交，若是则移动止损到入场价（保本）。

    ===== OKX 真相源 =====
    保本损数量使用 OKX 实时持仓，不用 trade.amount
    ======================
    """
    if trade.stop_loss == trade.open_rate:
        return True  # 已经是保本损
    if trade.open_rate <= 0:
        return False

    trade_ex = trade.exchange or "okx"
    tp1_filled = False
    for o in (trade.orders or []):
        if (o.ft_order_tag or "").startswith("tp_") and not o.ft_is_open and o.safe_filled > 0:
            tp1_filled = True
            break

    if not tp1_filled:
        return False

    # 实时获取 OKX 持仓
    pos = runtime.get_position(trade.pair)
    real_contracts = pos["contracts"] if pos else 0
    if real_contracts <= 0:
        logger.warning(f"[{trade_ex}] 保本损跳过：{trade.pair} 无实时仓位")
        return False

    logger.info(f"[{trade_ex}] TP1 已成交，SL → 保本价 {trade.open_rate} (OKX持仓={real_contracts}张)")
    # 只撤销移动止损单（trailing_stop），保留原始止损单（stoploss）作为最终保障
    trailing_sl_orders = [o for o in trade.open_sl_orders if o.ft_order_role == "trailing_stop"]
    for slo in trailing_sl_orders:
        try:
            await runtime.cancel_order(slo.order_id, trade.pair)
            slo.ft_is_open = False
        except Exception:
            pass
    session.flush()

    # 挂保本损 — 使用实时数量（基于剩余仓位）
    # v4: 统一走 ProtectionCreator
    trade.stop_loss = trade.open_rate
    sl_amt = _norm_amt(trade.pair, real_contracts, trade_ex)
    if sl_amt is None or sl_amt <= 0:
        logger.warning(f"[{trade_ex}] 保本损数量={sl_amt}，跳过（不合法）")
        return False
    try:
        from core.protection_creator import protection_creator
        # Bump clOrdId version — breakeven SL is a deliberate replacement
        protection_creator.bump_clordid_version(trade.id, "sl")
        result = await protection_creator.create_sl(trade, session)
        if not result.success:
            logger.error(f"[{trade_ex}] 保本损设置失败 {trade.pair}: {result.error}")
            return False
        # Mark the new order as trailing_stop for subsequent replacement
        for o in trade.orders:
            if o.order_id == result.algo_id:
                o.ft_order_role = "trailing_stop"
                break
        session.commit()
        logger.success(f"[{trade_ex}] 保本损已设置 {trade.pair} SL={trade.open_rate} algoId={result.algo_id} 数量={sl_amt}")
        return True
    except Exception as e:
        logger.error(f"[{trade_ex}] 保本损设置失败 {trade.pair}: {e}")
        return False


# ———— 开仓后流程 ————
async def post_entry_setup(trade: Trade, session: Session, tp_levels: list[dict]):
    """
    Entry fill setup — v5: Event-driven, ONE-TIME protection setup.

    Called ONCE when entry is confirmed filled.
    Uses REST pre-flight verification before creating SL/TP.
    No state machine. No loops. No repair queue pushes.
    """
    from core.reconciler import ensure_protection

    meta = trade.signal_meta or {}
    if meta.get("_setup_done"):
        return

    trade_ex = trade.exchange or "okx"

    # 限价成交后使用真实开仓均价；老师 SL 优先，缺失时才算固定止损。
    if (trade.stop_loss or 0) <= 0 and trade.open_rate > 0:
        from core.reconciler import _recompute_initial_stop_loss
        _recompute_initial_stop_loss(trade)

    # Apply any pending updates (teacher signals received before entry filled)
    pending = meta.pop("_pending_updates", [])
    if pending:
        from exchange_engine.signal_updater import apply_update_signal
        logger.info(f"[{trade_ex}] Applying {len(pending)} pending updates for {trade.pair}")
        for upd in pending:
            fake_signal = {
                "signal_type": "update",
                "symbol": trade.pair,
                "direction": "short" if trade.is_short else "long",
                "update_type": upd.get("update_type", "sl_and_tp"),
                "new_stop_loss": upd.get("new_stop_loss"),
                "new_take_profit": upd.get("new_take_profit"),
            }
            try:
                ok, info = await apply_update_signal(session, fake_signal, exchange=trade_ex)
                logger.info(f"[{trade_ex}] pending update applied: {'OK' if ok else 'FAIL'}: {info}")
            except Exception as e:
                logger.warning(f"[{trade_ex}] pending update failed: {e}")

    # 更新信号可能写入了新的 JSON 目标价；使用最新值，避免下面覆盖回旧 meta。
    meta = dict(trade.signal_meta or {})
    meta.pop("_pending_updates", None)

    # ONE-TIME protection setup via REST pre-flight verification
    try:
        result = await ensure_protection(trade, session)
        logger.info(f"[{trade_ex}] Protection setup {trade.pair}: SL={result.get('sl')} TP={result.get('tp')}")
    except Exception as e:
        logger.error(f"[{trade_ex}] ensure_protection failed {trade.pair}: {e}")
        # Will be retried by the 10-min Reconciler
        session.commit()
        return

    # Mark setup done
    meta["_setup_done"] = True
    trade.signal_meta = meta

    # 记录 TP1 初始创建时的仓位（用于分批挂单 TP1 重算）
    if meta.get("entry_strategy") == "limit_range":
        try:
            okx_pos = runtime.get_position(trade.pair)
            okx_contracts = okx_pos["contracts"] if okx_pos else 0
            if okx_contracts > 0:
                meta["_tp1_setup_amount"] = okx_contracts
                trade.signal_meta = meta
        except Exception:
            pass  # Non-critical: TP1 will be recalculated on next batch fill

    if trade.position_state != "open":
        from core.transaction import atomic_transaction
        async with atomic_transaction(session, "post_entry_setup: → open"):
            logger.info(f"[{trade_ex}] {trade.pair} → open")
            trade.position_state = "open"
            if not trade.opened_at:
                trade.opened_at = datetime.now(timezone.utc)
    else:
        session.commit()
