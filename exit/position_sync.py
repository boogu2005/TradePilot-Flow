"""
仓位同步器 — 以交易所为真相来源（Source of Truth），同步本地 DB。
每次 ExitManager 检查前运行，确保数据库反映真实持仓状态。

P0-2 自愈快照：如果 Position Snapshot 为空或过期，主动调用 refresh_position_snapshot()
          刷新缓存后再同步，而不是跳过等待。消除启动初期空缓存导致的"跳过同步"日志。

P1: 集成 trade_lock + atomic_transaction 确保并发安全
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade
from exchange_engine import exchange as ex
from core.trade_lock import trade_lock_manager
from core.transaction import atomic_transaction

_last_fail: dict[str, float] = {}
SYNC_RETRY_DELAY = 30.0
_last_snapshot: dict[str, str] = {}


async def _ensure_snapshot(exchange_name: str) -> list[dict] | None:
    """
    Get current positions from OKX REST (source of truth).

    ⚠️  REST-first: Uses fetch_positions (REST API), NOT WS snapshot.
    WS may be empty at startup or after reconnect, causing false "OKX=0"
    and incorrectly closing trades. REST is the ONLY source of truth.

    Falls back to WS snapshot only if REST fails.
    """
    from exchange_engine.exchange import fetch_positions, get_full_snapshot, refresh_position_snapshot

    # Primary: REST (source of truth)
    try:
        rest_positions = await fetch_positions(exchange=exchange_name)
        if rest_positions:
            logger.info(f"[{exchange_name}] REST仓位获取成功: {len(rest_positions)} 笔")
            return rest_positions
    except Exception as e:
        logger.warning(f"[{exchange_name}] REST fetch_positions 失败: {e}, 回退WS")

    # Fallback: WS snapshot (may be empty at startup)
    if ex.is_snapshot_healthy(exchange_name):
        raw = get_full_snapshot(exchange_name)
        if raw:
            return raw

    logger.info(f"[{exchange_name}] Snapshot 为空/过期，主动刷新一次")
    try:
        await refresh_position_snapshot(exchange_name)
    except Exception as e:
        logger.warning(f"[{exchange_name}] 主动刷新 Snapshot 失败: {e}")
        return None

    raw = get_full_snapshot(exchange_name)
    if raw:
        logger.info(f"[{exchange_name}] 主动刷新成功: {len(raw)} 笔持仓")
    return raw


def _norm(pair: str) -> str:
    return (pair or "").upper().replace("/", "").replace(":USDT", "").strip()


def _extract_positions_map(positions: list[dict], exchange_name: str) -> dict:
    result = {}
    for p in positions:
        try:
            pair = p.get("symbol", "")
            side = (p.get("side") or "").lower()
            contracts = float(p.get("contracts") or 0)
            if contracts <= 0:
                continue
            norm = _norm(pair)
            direction = "short" if side == "short" else "long"
            result[(norm, direction)] = {
                "pair": pair,
                "contracts": contracts,
                "entry_price": float(p.get("entry_price") or 0),
                "leverage": int(p.get("leverage") or 10),
                "unrealized_pnl": float(p.get("unrealized_pnl") or 0),
            }
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"[{exchange_name}] 解析持仓失败 {p}: {e}")
            continue
    return result


async def sync_positions(session: Session, exchange_name: str) -> list[Trade]:
    """
    同步单个交易对的仓位。

    与普通函数不同，sync_positions 不会在 Snapshot 为空时跳过。
    它会主动调用 refresh_position_snapshot() 确保缓存有数据。
    """
    global _last_fail

    # 检查重试冷却
    last_fail = _last_fail.get(exchange_name, 0.0)
    elapsed = datetime.now(timezone.utc).timestamp() - last_fail
    if last_fail > 0 and elapsed < SYNC_RETRY_DELAY:
        return []

    # 确保 Snapshot 可用（缓存为空时主动刷新 OKX）
    raw_positions = await _ensure_snapshot(exchange_name)
    if raw_positions is None:
        _last_fail[exchange_name] = datetime.now(timezone.utc).timestamp()
        logger.warning(f"[{exchange_name}] Snapshot 不可用 ({SYNC_RETRY_DELAY:.0f}s后重试)")
        return []

    _last_fail.pop(exchange_name, None)

    # 从原始数据中提取仓位映射（无论空还是有数据）
    exchange_positions = _extract_positions_map(raw_positions or [], exchange_name)

    # 记录快照摘要（仅在数据变化时打印）
    global _last_snapshot
    snap_parts = sorted(
        f"{p.get('symbol')}|{p.get('side')}|{p.get('contracts')}|{p.get('entryPrice')}"
        for p in (raw_positions or [])
    )
    snap_key = ";".join(snap_parts)
    if _last_snapshot.get(exchange_name) != snap_key:
        logger.info(f"[{exchange_name}] 原始持仓响应: {len(raw_positions or [])} 笔")
        for rp in (raw_positions or [])[:10]:
            logger.info(f"  {rp.get('symbol')} side={rp.get('side')} contracts={rp.get('contracts')} entry={rp.get('entryPrice')}")
        _last_snapshot[exchange_name] = snap_key

    synced = []
    db_trades = Trade.get_active_trades(session)
    db_trades_exchange = [t for t in db_trades if (t.exchange or "okx") == exchange_name]

    # 情况 1 & 3: 遍历 DB trades，与交易所对账
    for trade in db_trades_exchange:
        trade_lock = await trade_lock_manager.acquire(trade.id)
        async with trade_lock:
            try:
                norm_pair = _norm(trade.pair)
                direction = "short" if trade.is_short else "long"
                key = (norm_pair, direction)

                if key in exchange_positions:
                    ex_info = exchange_positions[key]
                    ex_amount = ex_info["contracts"]

                    async with atomic_transaction(session, f"position_sync: {trade.pair}"):
                        if abs(trade.amount - ex_amount) > 1e-8:
                            old_amt = trade.amount
                            trade.amount = ex_amount
                            logger.info(f"[Reconciliation] {exchange_name}:{norm_pair} {direction} DB={old_amt} OKX={ex_amount} -> corrected.")
                        if trade.signal_meta is None:
                            trade.signal_meta = {}
                        trade.signal_meta["sync_unrealized_pnl"] = ex_info.get("unrealized_pnl", 0)
                    synced.append(trade)
                else:
                    # OKX 无仓位，但需要检查是否有未过期的限价入场单
                    # 如果有，说明是限价开仓的正常等待状态，不应关闭 trade
                    from core.reconciler import _is_entry_order, _is_entry_order_stale

                    has_active_entry = any(
                        o.ft_is_open and _is_entry_order(o) and not _is_entry_order_stale(o)
                        for o in (trade.orders or [])
                    )

                    if has_active_entry:
                        # 有未过期的限价入场单，这是正常的等待成交状态
                        logger.info(f"[Reconciliation] {exchange_name}:{norm_pair} {direction} OKX无仓位但有未过期入场单，保持等待")
                        continue

                    # 没有未过期的入场单，可以关闭 trade
                    logger.warning(f"[Reconciliation] {exchange_name}:{norm_pair} {direction} DB={trade.amount} OKX=0 -> closed.")
                    async with atomic_transaction(session, f"position_sync_close: {trade.pair}"):
                        # 尝试用最新价格计算盈亏
                        try:
                            ticker = await ex.fetch_ticker(trade.pair, exchange=exchange_name)
                            trade.close(ticker["last"])  # okx_pnl=None → 公式回退
                        except Exception:
                            # 连行情价也拿不到，标记关闭（盈亏使用公式回退）
                            trade.close(trade.close_rate or trade.open_rate or 0)
                        trade.exit_reason = "manual_close"
                        trade.close_reason = "manual_close"
                        # 只取消退出单（SL/TP/Trailing），不取消未过期的入场单
                        if trade.orders:
                            for o in trade.orders:
                                if o.ft_is_open:
                                    # 跳过未过期的入场单
                                    if _is_entry_order(o) and not _is_entry_order_stale(o):
                                        continue
                                    try:
                                        await ex.cancel_order(o.order_id, trade.pair, exchange=exchange_name)
                                        o.ft_is_open = False
                                    except Exception:
                                        pass
                    synced.append(trade)
            except Exception as e:
                logger.error(f"[{exchange_name}] 同步异常 {trade.pair}: {e}")

    # 情况 2: DB 没有、交易所有 -> 恢复仓位
    # v5: DISABLED auto-recovery to prevent duplicate trades.
    # Recovery is now handled by reconcile_trade_existence in the Reconciler
    # which uses normalized pair comparison to avoid duplicates.
    _disabled_auto_recovery = True
    if _disabled_auto_recovery:
        skipped = len(exchange_positions) - len([t for t in db_trades_exchange if t.is_open])
        if skipped > 0:
            logger.info(f"[{exchange_name}] 跳过 {skipped} 个仓位恢复（由 Reconciler 统一处理）")
        session.commit()
        return synced

    for (norm_pair, direction), ex_info in exchange_positions.items():
        # Check both existing DB trades AND newly created trades in this batch
        already_exists = any(
            _norm(t.pair) == norm_pair and t.is_short == (direction == "short")
            for t in db_trades_exchange + synced if t.is_open
        )
        if already_exists:
            continue

        is_short = (direction == "short")
        trade = Trade(
            pair=norm_pair,
            base_currency=norm_pair.replace("USDT", ""),
            stake_currency="USDT",
            exchange=exchange_name,
            is_open=True,
            is_short=is_short,
            open_rate=ex_info["entry_price"],
            amount=ex_info["contracts"],
            amount_requested=ex_info["contracts"],
            open_date=datetime.now(timezone.utc),
            opened_at=datetime.now(timezone.utc),
            strategy="recovery",
            leverage=ex_info.get("leverage", 10),
            trading_mode="futures",
            signal_id="recovery",
            exit_mode="auto",
            position_state="open",
            fee_open=0.0004,
            fee_close=0.0004,
        )

        from core.config_loader import load_config
        config = load_config()
        sl_pct = abs(config.get("risk", {}).get("default_stoploss_pct", 0.02))
        trade.adjust_stop_loss(ex_info["entry_price"], sl_pct, initial=True)

        session.add(trade)
        # Push to Repair Queue instead of directly creating SL
        # This ensures state machine guards are respected
        try:
            from core.repair_queue import repair_queue, RepairTask
            from datetime import datetime as dt, timezone as tz
            repair_queue.push(RepairTask(
                priority=1,
                created_at=dt.now(tz.utc).timestamp(),
                trade_id=trade.id,
                task_type="create_sl",
                description=f"Position sync: new position {norm_pair} needs SL",
            ))
            logger.info(f"[{exchange_name}] 同步: {norm_pair} 已推入修复队列创建 SL")
        except Exception as e:
            logger.critical(f"[{exchange_name}] 同步: {norm_pair} 推入修复队列异常: {e}")

        logger.info(f"[{exchange_name}] 同步: 发现新仓位 {norm_pair} {direction} {ex_info['contracts']}张 @ {ex_info['entry_price']}")
        synced.append(trade)

    session.commit()
    return synced


async def sync_all_positions(session: Session) -> list[Trade]:
    all_synced = []
    for exchange_name in ex.get_active_exchanges():
        synced = await sync_positions(session, exchange_name)
        all_synced.extend(synced)
    return all_synced
