"""
信号更新器 — 处理 update / close / cancel 信号。
update: 撤旧 SL/TP 单 → 挂新 SL/TP 单
close:  全平或部分平
cancel: 取消未成交的 Entry 挂单

多交易所支持：根据 trade.exchange 路由操作。

P1: 集成 trade_lock 确保并发安全

Trade 匹配四级优先级：
  1. Reply 消息匹配（SignalLog → signal_id → Trade）
  2. 数据库匹配（symbol + direction + source_chat_id）
  3. OKX 持仓恢复（OKX 有仓位但 DB 无 Trade → 自动恢复）
  4. 放弃（OKX 也无仓位 → 记录日志）
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade, SignalLog, ReplyMapping
from exchange_engine import exchange as ex
from exchange_engine.trade_executor import execute_trade_exit
from core.trade_lock import trade_lock_manager

# 日志去重：同一消息 1 小时内最多打印一次
_LOG_DEDUP: dict[str, float] = {}
_LOG_DEDUP_TTL = 3600


def _log_once(key: str, msg: str, level: str = "warning"):
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


def _norm(pair: str) -> str:
    """统一 pair 格式: AAVE/USDT:USDT → AAVEUSDT"""
    return (pair or "").upper().replace("/", "").replace(":USDT", "").strip()


async def _trade_pnl_pct(trade: Trade) -> float | None:
    """计算持仓当前浮动盈亏比例（相对开仓价，正=盈利，负=亏损）。

    优先读 Position Snapshot 的 mark_price（零 HTTP），缺失时回退 ticker。
    无法获取价格时返回 None（调用方按原逻辑处理）。
    """
    if trade.open_rate <= 0:
        return None
    trade_ex = trade.exchange or "okx"
    price = 0.0
    try:
        pos = ex.get_position_from_snapshot(trade.pair, trade_ex)
        if pos:
            price = float(pos.get("mark_price") or pos.get("markPrice") or 0)
    except Exception:
        price = 0.0
    if price <= 0:
        try:
            ticker = await ex.fetch_ticker(trade.pair, exchange=trade_ex)
            price = float(ticker.get("last") or 0)
        except Exception as e:
            logger.warning(f"[CLOSE] {trade.pair} 获取当前价失败，无法判断盈亏: {e}")
            return None
    if price <= 0:
        return None
    if trade.is_short:
        return (trade.open_rate - price) / trade.open_rate
    return (price - trade.open_rate) / trade.open_rate


# ════════════════════════════════════════════════════════════════════════
# Trade 匹配四级优先级
# ════════════════════════════════════════════════════════════════════════


async def _find_trade_by_reply(
    session: Session,
    signal: dict,
    exchange: str = "okx",
) -> Trade | None:
    """
    第一优先级：Reply 消息匹配（v2 重构 — 6 级匹配）。

    通过 reply_to_msg_id 定位 Trade。
    不再限制为 active trades — 所有 Trade 均可匹配。

    v2 改进:
    - Level 1: ReplyMapping O(1) 索引查询
    - Level 2: Trade.telegram_message_id 索引查询
    - Level 3: Trade.signal_meta.tg_msg_id 反序列化匹配
    - Level 4: SignalLog.tg_msg_id → Trade.signal_id 关联查询
    """
    reply_id = signal.get("reply_to_msg_id")
    if not reply_id:
        return None

    symbol = signal.get("symbol", "")
    sym_norm = _norm(symbol)

    # ================================================================
    # Level 1: ReplyMapping 直接查询（O(1)，100% 命中如果 mapping 存在）
    # ================================================================
    try:
        mapping = session.query(ReplyMapping).filter(
            ReplyMapping.telegram_message_id == reply_id
        ).first()
        if mapping and mapping.trade_id:
            trade = session.get(Trade, mapping.trade_id)
            if trade:
                logger.info(
                    f"[匹配] ReplyMapping 命中: ReplyMsgID={reply_id} "
                    f"→ TradeID={trade.id} {trade.pair} "
                    f"{'SHORT' if trade.is_short else 'LONG'}"
                )
                return trade
    except Exception:
        pass

    # ================================================================
    # Level 2: Trade.telegram_message_id 直接查询（索引）
    # ================================================================
    try:
        trade = Trade.find_by_telegram_message_id(session, reply_id)
        if trade and (trade.exchange or "okx") == exchange:
            if not sym_norm or _norm(trade.pair) == sym_norm:
                logger.info(
                    f"[匹配] TelegramMsgID 命中: ReplyMsgID={reply_id} "
                    f"→ TradeID={trade.id} {trade.pair} "
                    f"{'SHORT' if trade.is_short else 'LONG'}"
                )
                return trade
    except Exception:
        pass

    # ================================================================
    # Level 3: Trade.signal_meta.tg_msg_id 反序列化匹配
    # ================================================================
    try:
        all_trades = session.query(Trade).all()
        for t in all_trades:
            if (t.exchange or "okx") != exchange:
                continue
            if sym_norm and _norm(t.pair) != sym_norm:
                continue
            meta = t.signal_meta or {}
            if meta.get("tg_msg_id") == reply_id:
                logger.info(
                    f"[匹配] signal_meta 命中: ReplyMsgID={reply_id} "
                    f"→ TradeID={t.id} {t.pair} "
                    f"{'SHORT' if t.is_short else 'LONG'}"
                )
                return t
    except Exception:
        pass

    # ================================================================
    # Level 4: SignalLog.tg_msg_id → Trade.signal_id 关联查询
    # ================================================================
    try:
        log = session.query(SignalLog).filter(
            SignalLog.tg_msg_id == reply_id
        ).first()
        if log and log.signal_id:
            trade = Trade.find_by_signal_id(session, log.signal_id)
            if trade and (trade.exchange or "okx") == exchange:
                if not sym_norm or _norm(trade.pair) == sym_norm:
                    logger.info(
                        f"[匹配] SignalLog 命中: ReplyMsgID={reply_id} "
                        f"→ SignalID={log.signal_id} "
                        f"→ TradeID={trade.id} {trade.pair} "
                        f"{'SHORT' if trade.is_short else 'LONG'}"
                    )
                    return trade
    except Exception:
        pass

    logger.info(f"[匹配] Reply 未命中: ReplyMsgID={reply_id} {symbol}，进入下一级匹配")
    return None


async def _find_trade_by_db(
    session: Session,
    signal: dict,
    exchange: str = "okx",
) -> Trade | None:
    """
    第五优先级：数据库匹配（v2 — 搜索所有 Trade，不限制 active）。

    使用 symbol + direction + source_chat_id 查找唯一 Trade。
    如果 source_chat_id 匹配失败，放宽为 symbol + direction（跨群匹配）。
    """
    symbol = signal.get("symbol", "")
    direction = signal.get("direction", "") or ""
    src_chat = signal.get("source_chat_id", "") or ""

    # 查询所有 Trade（v2 修复：不限制 active trades）
    all_trades = session.query(Trade).all()

    # 5a: 精确匹配 symbol + direction + source_chat_id
    if src_chat:
        sym_norm = _norm(symbol)
        matches = [t for t in all_trades
                   if _norm(t.pair) == sym_norm and t.exchange == exchange
                   and str(t.source_chat_id or "") == str(src_chat)]
        if matches:
            matches.sort(key=lambda t: t.open_date or datetime.min, reverse=True)
            trade = matches[0]
            # 方向校验
            if direction:
                trade_dir = "short" if trade.is_short else "long"
                if trade_dir != direction:
                    logger.info(
                        f"[匹配] DB精确匹配方向冲突: {symbol} "
                        f"source_chat={src_chat} 期望={direction} 实际={trade_dir}，"
                        f"尝试放宽匹配"
                    )
                else:
                    logger.info(
                        f"[匹配] DB精确命中: TradeID={trade.id} {trade.pair} "
                        f"{'SHORT' if trade.is_short else 'LONG'} "
                        f"source_chat={src_chat}"
                    )
                    return trade
            else:
                logger.info(
                    f"[匹配] DB精确命中: TradeID={trade.id} {trade.pair} "
                    f"{'SHORT' if trade.is_short else 'LONG'} "
                    f"source_chat={src_chat}"
                )
                return trade

    # 5b: 放宽匹配 symbol + direction（不限 source_chat_id）
    sym_norm = _norm(symbol)
    if sym_norm:
        matches = [t for t in all_trades
                   if _norm(t.pair) == sym_norm and t.exchange == exchange]
        if direction:
            matches = [t for t in matches if t.is_short == (direction == "short")]
        if matches:
            matches.sort(key=lambda t: t.open_date or datetime.min, reverse=True)
            trade = matches[0]
            logger.info(
                f"[匹配] DB放宽命中: TradeID={trade.id} {trade.pair} "
                f"{'SHORT' if trade.is_short else 'LONG'} "
                f"(source_chat={src_chat} 未匹配，跨群回退)"
            )
            return trade

    logger.info(
        f"[匹配] DB未命中: {symbol} direction={direction or 'any'} "
        f"source_chat={src_chat}"
    )
    return None


async def _try_recover_from_exchange(
    session: Session,
    signal: dict,
    exchange: str = "okx",
) -> Trade | None:
    """
    第六优先级：OKX 实时查询恢复（v2 修复 — 最后关卡使用真实 API）。

    如果数据库没有找到 Trade，且 Position Snapshot 也没有仓位：
    最后调用一次 OKX 实时 API 查询持仓。
    只有 OKX 真实确认无仓位，才放弃恢复。

    OKX 是 Source of Truth。
    """
    symbol = signal.get("symbol", "")
    direction = signal.get("direction", "") or ""
    sym_norm = _norm(symbol)

    if not sym_norm:
        return None

    # 6a: 先查 Position Snapshot（零 HTTP）
    try:
        positions = ex.get_full_snapshot(exchange=exchange)
    except Exception as e:
        logger.warning(f"[恢复] 查询 Position Snapshot 失败: {e}")
        positions = None

    matched_pos = _match_position(positions, sym_norm, direction) if positions else None

    # 6b: Snapshot 未命中 → 最后关头调用一次实时 API
    if matched_pos is None:
        logger.info(f"[恢复] Snapshot 未命中 {sym_norm}，尝试实时 OKX API 查询...")
        try:
            live_positions = await ex.fetch_positions(exchange=exchange)
            matched_pos = _match_position(live_positions, sym_norm, direction)
        except Exception as e:
            logger.warning(f"[恢复] 实时查询 OKX 失败: {e}")

    if matched_pos is None:
        logger.info(
            f"[恢复] OKX无匹配仓位: {symbol} direction={direction or 'any'}，"
            f"放弃恢复"
        )
        # 记录 RecoveryLog（用于审计）
        try:
            from database.models import RecoveryLog
            rl = RecoveryLog(
                symbol=sym_norm,
                direction=direction or "unknown",
                reason="okx_no_position",
                rebuilt=False,
                detail="Snapshot + Live API 均未找到仓位",
            )
            session.add(rl)
            session.flush()
        except Exception:
            pass
        return None

    # OKX 有仓位 → 自动恢复 Trade
    pos_side = (matched_pos.get("side") or "").lower()
    is_short = (pos_side == "short")
    entry_price = float(matched_pos.get("entry_price") or matched_pos.get("avgPx") or 0)
    contracts = float(matched_pos.get("contracts") or 0)
    leverage = float(matched_pos.get("leverage") or 10)

    logger.warning(
        f"[恢复] 数据库Trade缺失 → OKX发现持仓 "
        f"{symbol} {'SHORT' if is_short else 'LONG'} "
        f"{contracts}张 @ {entry_price} → 自动恢复Trade"
    )

    # 创建恢复 Trade
    trade = Trade(
        pair=sym_norm,
        base_currency=sym_norm.replace("USDT", ""),
        stake_currency="USDT",
        exchange=exchange,
        is_open=True,
        is_short=is_short,
        open_rate=entry_price,
        amount=contracts,
        amount_requested=contracts,
        open_date=datetime.now(timezone.utc),
        opened_at=datetime.now(timezone.utc),
        strategy="recovery",
        leverage=leverage,
        trading_mode="futures",
        signal_id="recovery",
        exit_mode="auto",
        position_state="open",
        fee_open=0.0004,
        fee_close=0.0004,
        # 来源信息从信号中继承
        source_chat_id=signal.get("source_chat_id", ""),
        source_group_name=signal.get("source_group_name", ""),
    )

    # 设置默认 SL
    from core.config_loader import load_config
    config = load_config()
    sl_pct = abs(config.get("risk", {}).get("default_stoploss_pct", 0.02))
    trade.adjust_stop_loss(entry_price, sl_pct, initial=True)

    session.add(trade)
    try:
        session.flush()  # 获取 trade.id
    except Exception as e:
        logger.error(f"[恢复] Trade写入DB失败: {e}")
        session.rollback()
        return None

    # 记录 RecoveryLog
    try:
        from database.models import RecoveryLog
        rl = RecoveryLog(
            symbol=sym_norm,
            direction="short" if is_short else "long",
            reason="db_missing",
            db_trade_id=trade.id,
            okx_contracts=contracts,
            rebuilt=True,
        )
        session.add(rl)
        session.commit()
    except Exception:
        session.commit()

    # 补挂 TP1（通过 ProtectionCreator）
    try:
        from core.protection_creator import protection_creator
        tp_result = await protection_creator.create_tp(trade, session)
        if not tp_result.success:
            logger.warning(f"[恢复] {sym_norm} TP1 创建失败: {tp_result.error}")
    except Exception as e:
        logger.warning(f"[恢复] {sym_norm} TP1补挂失败: {e}")

    # 补挂交易所 SL — 推入 Repair Queue（v4 统一路径）
    try:
        from core.repair_queue import repair_queue, RepairTask
        repair_queue.push(RepairTask(
            priority=1,
            created_at=datetime.now(timezone.utc).timestamp(),
            trade_id=trade.id,
            task_type="create_sl",
            description=f"Recovery: new position {sym_norm} needs SL",
        ))
        logger.info(f"[恢复] {sym_norm} SL 创建已推入 Repair Queue")
    except Exception as e:
        logger.warning(f"[恢复] {sym_norm} SL 推入 Repair Queue 失败: {e}")

    logger.success(
        f"[恢复] Trade恢复成功: TradeID={trade.id} {sym_norm} "
        f"{'SHORT' if is_short else 'LONG'} {contracts}张 @ {entry_price}"
    )
    return trade


def _match_position(
    positions: list[dict] | None,
    sym_norm: str,
    direction: str,
) -> dict | None:
    """辅助函数：匹配仓位列表中的对应仓位。"""
    if not positions:
        return None
    for p in positions:
        p_sym = _norm(p.get("symbol", ""))
        if p_sym != sym_norm:
            continue
        side = (p.get("side") or "").lower()
        contracts = float(p.get("contracts") or 0)
        if contracts <= 0:
            continue
        pos_dir = "short" if side == "short" else "long"
        if direction and pos_dir != direction:
            continue
        return p
    return None


async def _resolve_trade(
    session: Session,
    signal: dict,
    exchange: str = "okx",
) -> tuple[Trade | None, str]:
    """
    统一 Trade 解析入口 — 六级优先级匹配（v2 重构）。

    v2 改进:
    - Level 1-4: Reply 消息匹配（O(1) → 索引 → 遍历 → 关联）
    - Level 5:   数据库匹配（搜索所有 trade，不限于 active）
    - Level 6:   OKX 实时查询（Snapshot + Live API 双重保障）

    返回 (trade, match_source):
      trade: Trade 对象或 None
      match_source:
        "reply_mapping" / "reply_msg_id" / "reply_signal_meta" / "reply_signal_log"
        "db_exact" / "db_fallback" / "recovery" / "not_found"
    """
    symbol = signal.get("symbol", "")
    reply_id = signal.get("reply_to_msg_id")

    # 如果存在 reply_id，日志显示
    if reply_id:
        logger.info(f"[匹配] === 开始匹配 {symbol} ReplyMsgID={reply_id} ===")

    # ================================================================
    # Level 1-4: Reply 消息匹配（不限制 active trades）
    # ================================================================
    if reply_id:
        trade = await _find_trade_by_reply(session, signal, exchange=exchange)
        if trade:
            # 判断具体是哪个子匹配
            mapping = None
            try:
                mapping = session.query(ReplyMapping).filter(
                    ReplyMapping.telegram_message_id == reply_id,
                    ReplyMapping.trade_id == trade.id,
                ).first()
            except Exception:
                pass
            if mapping:
                return trade, "reply_mapping"
            if trade.telegram_message_id == reply_id:
                return trade, "reply_msg_id"
            meta = trade.signal_meta or {}
            if meta.get("tg_msg_id") == reply_id:
                return trade, "reply_signal_meta"
            return trade, "reply_signal_log"

    # ================================================================
    # Level 5: 数据库匹配（搜索所有 trade）
    # ================================================================
    trade = await _find_trade_by_db(session, signal, exchange=exchange)
    if trade:
        src_chat = signal.get("source_chat_id", "") or ""
        if src_chat and str(trade.source_chat_id or "") == str(src_chat):
            return trade, "db_exact"
        else:
            return trade, "db_fallback"

    # ================================================================
    # Level 6: OKX 实时查询恢复（Snapshot + Live API）
    # ================================================================
    trade = await _try_recover_from_exchange(session, signal, exchange=exchange)
    if trade:
        return trade, "recovery"

    # 所有级别均未命中 → 放弃
    logger.warning(
        f"[匹配] 全部未命中: {symbol} "
        f"Reply=无 DB=无 OKX=无仓位 → 放弃"
    )
    return None, "not_found"


# ════════════════════════════════════════════════════════════════════════
# 方向解析（Update/Close 信号专用）
# ════════════════════════════════════════════════════════════════════════


async def resolve_update_direction(signal: dict, trade_ex: str = "okx", session=None) -> tuple[str, str]:
    """
    三级方向解析：
    1. AI 方向 → 使用（DeepSeek 已解析）
    2. Reply 定位历史 Trade → 使用 Trade.direction
    3. 查询交易所持仓 → 单方向 → 使用；双向 → 拒绝

    返回 (direction, source):
      direction: "long" / "short" / ""
      source: "ai" / "reply" / "exchange" / "rejected" / "no_position"
    """
    # ① 如果 signal 已有 direction 且非空，直接使用
    ai_dir = signal.get("direction", "") or ""
    if ai_dir in ("long", "short"):
        return ai_dir, "ai"

    # ② Reply ID 定位 Trade（增加 symbol 过滤）
    reply_id = signal.get("reply_to_msg_id")
    if reply_id:
        own_session = False
        if session is None:
            from database.db import get_session
            session = get_session()
            own_session = True
        try:
            sym_norm = _norm(signal.get("symbol", ""))
            candidates = Trade.get_active_trades(session)
            for trade in candidates:
                if (trade.exchange or "okx") != trade_ex:
                    continue
                if sym_norm and _norm(trade.pair) != sym_norm:
                    continue
                meta = trade.signal_meta or {}
                if meta.get("tg_msg_id") == reply_id:
                    dir_val = "short" if trade.is_short else "long"
                    return dir_val, "reply"
        except Exception:
            pass
        finally:
            if own_session and session is not None:
                session.close()

    # ③ 查询交易所真实持仓 — 从 Position Snapshot 读取（零 HTTP）
    try:
        positions = ex.get_full_snapshot(exchange=trade_ex)
        if not positions:
            _log_once(f"snapshot_not_ready:{trade_ex}",
                      f"[{trade_ex}] Position Snapshot 未就绪，无法解析方向")
            return "", "no_position"
        sym_norm = (signal.get("symbol") or "").upper().replace("/", "").replace(":USDT", "")
        sym_positions = {"long": False, "short": False}
        for p in positions:
            p_sym = (p.get("symbol") or "").upper().replace("/", "").replace(":USDT", "")
            if p_sym == sym_norm:
                side = (p.get("side") or "").lower()
                contracts = float(p.get("contracts") or 0)
                if contracts > 0:
                    if side == "short":
                        sym_positions["short"] = True
                    else:
                        sym_positions["long"] = True

        has_long = sym_positions["long"]
        has_short = sym_positions["short"]

        if has_long and has_short:
            logger.warning(f"[方向解析] {sym_norm} 存在双向持仓，Update 缺少 direction，已拒绝")
            return "", "rejected"
        if has_long:
            return "long", "exchange"
        if has_short:
            return "short", "exchange"
    except Exception as e:
        logger.warning(f"[方向解析] 查询交易所持仓失败: {e}")

    return "", "no_position"


# ════════════════════════════════════════════════════════════════════════
# Cancel 信号处理
# ════════════════════════════════════════════════════════════════════════


async def apply_cancel_signal(session: Session, signal: dict,
                               exchange: str = "okx") -> tuple[bool, str]:
    """处理 cancel 信号 — 取消未成交的 Entry 挂单。"""
    trade_ex = exchange
    symbol = signal.get("symbol", "")
    if not symbol:
        return False, "缺少 symbol"

    # 来源绑定
    src_chat = signal.get("source_chat_id", "") or ""
    if src_chat:
        trade = await get_latest_open_trade(session, symbol, src_chat, exchange=exchange)
        if trade is None:
            return False, f"[{exchange}] {symbol} 未找到来源群 {src_chat} 的活跃 trade，已忽略"
    else:
        trade = None

    if trade:
        trades = [trade]
    else:
        trades = Trade.get_active_trades(session)

    cancelled = 0
    for trade in trades:
        if (trade.exchange or "okx") != trade_ex:
            continue
        if _norm(trade.pair) != _norm(symbol):
            continue
        # P1: 使用 trade_lock 保护取消操作
        trade_lock = await trade_lock_manager.acquire(trade.id)
        async with trade_lock:
            for o in (trade.orders or []):
                if o.ft_is_open and o.ft_order_side == trade.entry_side:
                    try:
                        await ex.cancel_order(o.order_id, trade.pair, exchange=trade_ex)
                        o.ft_is_open = False
                        cancelled += 1
                    except Exception as e:
                        logger.warning(f"[{trade_ex}] 取消挂单失败 {trade.pair} {o.order_id}: {e}")
            if cancelled and trade.amount == 0:
                trade.is_open = False
                trade.exit_reason = "cancelled"
                trade.close_date = datetime.now(timezone.utc)
                trade.position_state = "closed"
    session.commit()
    if cancelled:
        logger.info(f"[{trade_ex}] 取消挂单 {symbol}: {cancelled} 笔")
        return True, f"取消 {cancelled} 笔挂单"
    return False, f"未找到 {symbol} 的挂单"


# ════════════════════════════════════════════════════════════════════════
# SL/TP 内部操作
# ════════════════════════════════════════════════════════════════════════


async def _cancel_sl_orders(trade: Trade) -> None:
    """撤掉所有未成交的移动止损单（trailing_stop），保留原始止损单（stoploss）作为最终保障"""
    trade_ex = trade.exchange or "okx"
    # 只撤销移动止损单，保留原始止损单
    trailing_sl_orders = [o for o in trade.open_sl_orders if o.ft_order_role == "trailing_stop"]
    for slo in trailing_sl_orders:
        try:
            await ex.cancel_order(slo.order_id, trade.pair, exchange=trade_ex)
            slo.ft_is_open = False
        except Exception as e:
            logger.warning(f"[{trade_ex}] 撤SL {trade.pair} {slo.order_id}: {e}")


async def _cancel_tp_orders(trade: Trade) -> None:
    """撤掉所有未成交的止盈单"""
    trade_ex = trade.exchange or "okx"
    for o in trade.orders:
        if (o.ft_order_tag or "").startswith("tp_") and o.ft_is_open:
            try:
                await ex.cancel_order(o.order_id, trade.pair, exchange=trade_ex)
                o.ft_is_open = False
            except Exception as e:
                logger.warning(f"[{trade_ex}] 撤TP {trade.pair} {o.order_id}: {e}")


async def _place_sl(trade: Trade, sl_price: float, session=None) -> bool:
    """
    挂新的 SL 单 — 通过 ProtectionCreator（v4 统一入口）。

    ===== OKX 真相源 =====
    SL 数量使用实时 OKX 持仓，经 normalize_order_amount 校验。
    ProtectionCreator 负责状态机守卫、去重、冷却时间。
    ======================
    """
    if sl_price <= 0:
        return False
    trade_ex = trade.exchange or "okx"

    # 老师指定的是绝对价；保留旧单，直到新单已在 OKX 建立。
    old_algo_id = trade.sl_algo_id
    if trade.open_rate > 0:
        pct = abs(sl_price - trade.open_rate) / trade.open_rate
        trade.stop_loss_pct = -pct
        trade.initial_stop_loss_pct = -pct
    else:
        # 回退：入场价不可用（极端情况，如恢复的 trade）
        pos = ex.get_position_from_snapshot(trade.pair, trade_ex)
        if pos:
            current = float(pos.get("markPrice", 0) or 0)
            if current <= 0:
                ticker = await ex.fetch_ticker(trade.pair, exchange=trade_ex)
                current = ticker["last"]
        else:
            ticker = await ex.fetch_ticker(trade.pair, exchange=trade_ex)
            current = ticker["last"]
        pct = abs(sl_price - current) / current if current > 0 else 0.02
        trade.stop_loss_pct = -pct
        trade.initial_stop_loss_pct = -pct
    trade.stop_loss = sl_price
    trade.initial_stop_loss = sl_price

    # v5: 统一走 ProtectionCreator（REST pre-flight + clientOrderId）
    from core.protection_creator import protection_creator
    # Bump clOrdId version — teacher update is a deliberate replacement
    protection_creator.bump_clordid_version(trade.id, "sl")
    result = await protection_creator.create_sl(trade, session) if session else await _create_sl_fallback(trade)
    if not result.success:
        logger.warning(f"[{trade_ex}] ProtectionCreator.create_sl 失败 {trade.pair}: {result.error}")
        return False
    if old_algo_id and old_algo_id != result.algo_id:
        from core.exchange_runtime import runtime
        try:
            await runtime.cancel_algo_order(old_algo_id, trade.pair)
            for order in trade.orders or []:
                if order.order_id == old_algo_id:
                    order.ft_is_open = False
        except Exception as e:
            logger.warning(f"[{trade_ex}] 新SL已建立但旧SL {old_algo_id} 撤单失败: {e}")
    logger.info(f"[{trade_ex}] 挂新SL {trade.pair} algoId={result.algo_id}")
    return True


async def _create_sl_fallback(trade: Trade) -> bool:
    """Fallback: create SL without session (used when session is None)."""
    try:
        from database.db import get_session
        session = get_session()
        try:
            from core.protection_creator import protection_creator
            result = await protection_creator.create_sl(trade, session)
            if result.success:
                session.commit()
                return True
            session.rollback()
            return False
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        logger.error(f"[{trade.exchange}] _create_sl_fallback 失败 {trade.pair}: {e}")
        return False


async def _place_tp_levels(trade: Trade, tp_levels, session=None) -> int:
    """
    挂分批止盈 — 通过 ProtectionCreator（v4 统一入口）。

    ===== OKX 真相源 =====
    TP 数量使用实时 OKX 持仓，经 normalize_tp_levels 自动重分配。
    ProtectionCreator 负责状态机守卫、去重、冷却时间。
    ======================
    """
    trade_ex = trade.exchange or "okx"
    if not isinstance(tp_levels, list):
        logger.warning(f"[{trade_ex}] _place_tp_levels 收到非 list 参数 {type(tp_levels).__name__}，跳过")
        return 0

    from core.protection_creator import protection_creator

    success = 0
    for i, tp in enumerate(tp_levels):
        tp_qty = tp.get("contracts", 0)
        if tp_qty <= 0:
            logger.info(f"[{trade_ex}] TP{i+1} {trade.pair} 数量={tp_qty}，跳过（小仓位自动合并）")
            continue
        try:
            price = tp["price"]
            # 更新 TP 价格到 Trade（TP1 专用）
            if i == 0:
                trade.tp1_price = price
            # v4: 统一走 ProtectionCreator
            tp_index = i + 1
            result = await protection_creator.create_tp(trade, session, tp_index=tp_index) if session else None
            if result is None:
                # No session → try fallback
                result = await _create_tp_fallback(trade, tp_index, price)
            if result and result.success:
                success += 1
                logger.info(
                    f"[{trade_ex}] 挂新TP{tp_index} {trade.pair} algoId={result.algo_id} "
                    f"(基于OKX持仓)"
                )
            else:
                err = result.error if result else "no session"
                logger.warning(f"[{trade_ex}] ProtectionCreator.create_tp 失败 {trade.pair}: {err}")
        except Exception as e:
            logger.error(f"[{trade_ex}] 挂新TP{i+1} {trade.pair}: {e}")
    return success


async def _create_tp_fallback(trade: Trade, tp_index: int, price: float) -> bool:
    """Fallback: create TP without session (used when session is None)."""
    try:
        from database.db import get_session
        session = get_session()
        try:
            from core.protection_creator import protection_creator
            # Set the TP price before creating
            if tp_index == 1:
                trade.tp1_price = price
            result = await protection_creator.create_tp(trade, session, tp_index=tp_index)
            if result.success:
                session.commit()
                return result
            session.rollback()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        logger.error(f"[{trade.exchange}] _create_tp_fallback 失败 {trade.pair}: {e}")
        from dataclasses import dataclass
        @dataclass
        class FallbackResult:
            success: bool = False
            algo_id: str = ""
            error: str = str(e)
        return FallbackResult()


# ════════════════════════════════════════════════════════════════════════
# 原有查找函数（保留，供 _find_trade_by_db 使用）
# ════════════════════════════════════════════════════════════════════════


async def find_open_trade(session: Session, symbol: str, direction: str,
                          exchange: str = "okx") -> Trade | None:
    """根据 symbol + direction + exchange 找当前活跃的 Trade。
    direction 为空时匹配任意方向（用于 update/close 信号不传 direction 的场景）。

    v2 修复：搜索所有 Trade，不限于 active_trades。"""
    sym_norm = _norm(symbol)
    all_trades = session.query(Trade).all()
    for t in all_trades:
        if _norm(t.pair) == sym_norm and t.exchange == exchange:
            if direction and (t.is_short == (direction == "short")):
                return t
            if not direction:
                return t
    return None


async def get_latest_open_trade(session: Session, symbol: str, chat_id: str,
                                 exchange: str = "okx") -> Trade | None:
    """
    按 symbol + source_chat_id 找最新一笔 Trade（不限 open/close）。
    用于 Telegram Update/Close/Cancel 信号的来源绑定。

    v2 修复：搜索所有 Trade，不限于 active_trades。"""
    if not chat_id or not symbol:
        return None
    sym_norm = _norm(symbol)
    all_trades = session.query(Trade).all()
    matches = [t for t in all_trades
               if _norm(t.pair) == sym_norm and t.exchange == exchange
               and str(t.source_chat_id or "") == str(chat_id)]
    if not matches:
        return None
    matches.sort(key=lambda t: t.open_date or datetime.min, reverse=True)
    return matches[0]


# ════════════════════════════════════════════════════════════════════════
# Update 信号处理（重构：四级优先级匹配）
# ════════════════════════════════════════════════════════════════════════


async def apply_update_signal(session: Session, signal: dict,
                               exchange: str = "okx") -> tuple[bool, str]:
    """
    处理一条 update 信号。

    Trade 匹配四级优先级：
      1. Reply 消息匹配（SignalLog → signal_id → Trade）
      2. 数据库匹配（symbol + direction + source_chat_id，放宽跨群）
      3. OKX 持仓恢复（OKX 有仓位 → 自动恢复 Trade）
      4. 放弃（OKX 也无仓位）
    """
    symbol = signal.get("symbol", "")
    ai_dir = signal.get("direction", "") or ""

    # 方向解析
    resolved_dir, dir_source = await resolve_update_direction(
        signal, trade_ex=exchange, session=session
    )
    if dir_source == "rejected":
        return False, f"[{exchange}] {symbol} 双向持仓存在，Update 缺少 direction，已拒绝"
    if resolved_dir:
        signal["direction"] = resolved_dir
        if ai_dir != resolved_dir:
            logger.info(
                f"[UPDATE] {symbol} AI direction={ai_dir or 'null'} "
                f"→ Final={resolved_dir} source={dir_source}"
            )

    # 四级优先级匹配 Trade
    trade, match_source = await _resolve_trade(session, signal, exchange=exchange)

    if trade is None:
        return False, f"[{exchange}] {symbol} 未找到活跃 trade（DB+OKX均无），已忽略"

    logger.info(
        f"[UPDATE] {symbol} Trade匹配成功: TradeID={trade.id} "
        f"match_source={match_source}"
    )

    # 入场未成交 → 暂存 update
    if trade.amount == 0 and trade.has_open_orders:
        trade_lock = await trade_lock_manager.acquire(trade.id)
        async with trade_lock:
            meta = dict(trade.signal_meta or {})
            pending = list(meta.get("_pending_updates", []))
            pending.append({
                "update_type": signal.get("update_type", "sl_and_tp"),
                "new_stop_loss": signal.get("new_stop_loss"),
                "new_take_profit": signal.get("new_take_profit"),
            })
            meta["_pending_updates"] = pending
            trade.signal_meta = meta
            session.commit()
        return True, f"入场未成交，update 已暂存 ({len(pending)}个待处理)"

    # 执行 SL/TP 更新
    update_type = signal.get("update_type", "sl_and_tp")
    new_sl = signal.get("new_stop_loss")
    new_tp_raw = signal.get("new_take_profit")
    from core.protection_targets import normalize_tp_prices
    new_tp = normalize_tp_prices(new_tp_raw)

    # 保本损：AI 无法得知开仓价 → 程序用开仓均价填充
    if update_type == "move_sl_to_breakeven" and new_sl is None and trade.open_rate > 0:
        new_sl = float(trade.open_rate)
        logger.info(f"[UPDATE] {symbol} 保本损 → SL 设为开仓价 {new_sl}")

    trade_lock = await trade_lock_manager.acquire(trade.id)
    async with trade_lock:
        msg = []

        # 归一化所有"改止损"语义的 update_type（含 modify_sl / move_sl / 保本损）
        _SL_UPDATE_TYPES = {
            "sl", "sl_and_tp", "modify_sl", "move_sl",
            "move_sl_to_breakeven", "modify_both",
        }
        sl_changed = update_type in _SL_UPDATE_TYPES and new_sl is not None
        if sl_changed:
            old_sl = trade.stop_loss
            meta = dict(trade.signal_meta or {})
            meta["_teacher_sl_price"] = float(new_sl)
            meta["stop_loss"] = float(new_sl)
            trade.signal_meta = meta
            ok = await _place_sl(trade, float(new_sl), session=session)
            msg.append(f"SL→{new_sl}({'OK' if ok else 'FAIL'})")

            # ———— 记录 UpdateHistory ————
            try:
                from database.models import UpdateHistory
                uh = UpdateHistory(
                    trade_id=trade.id,
                    telegram_message_id=signal.get("source_message_id"),
                    reply_message_id=signal.get("reply_to_msg_id"),
                    update_type=update_type,
                    old_sl=old_sl if old_sl else None,
                    new_sl=float(new_sl),
                    operator=signal.get("source_sender", ""),
                )
                session.add(uh)
            except Exception:
                pass

        from core.protection_targets import normalize_tp_prices, teacher_tp_prices
        new_tp = normalize_tp_prices(new_tp)
        tp_update_types = {"tp", "sl_and_tp", "modify_tp", "move_tp", "modify_both", "move_both", "add_tp"}
        if update_type in tp_update_types and new_tp:
            if update_type == "add_tp":
                new_tp = normalize_tp_prices(teacher_tp_prices(trade) + new_tp)
            tp_start = 2 if trade.position_state == "tp1_filled" else 1
            if tp_start == 2:
                new_tp = new_tp[:1]
            meta = dict(trade.signal_meta or {})
            meta["_teacher_tp_prices"] = new_tp
            meta["_teacher_tp_start_index"] = tp_start
            meta["take_profit"] = new_tp
            trade.signal_meta = meta
            trade.tp1_price = new_tp[0] if tp_start == 1 else None
            trade.tp2_price = (new_tp[1] if len(new_tp) > 1 else None) if tp_start == 1 else new_tp[0]
            from exit.protection import cancel_tp
            cancelled = True
            for index in (1, 2):
                if not await cancel_tp(trade, session, tp_index=index):
                    cancelled = False
            if cancelled:
                from core.protection_creator import protection_creator
                indexes = (2,) if tp_start == 2 else (1, 2) if trade.position_state == "open" else ()
                for index in indexes:
                    from core.protection_targets import tp_price_for
                    target_price = tp_price_for(trade, index)
                    if target_price is None:
                        continue
                    protection_creator.bump_clordid_version(trade.id, f"tp{index}")
                    result = await protection_creator.create_tp(trade, session, tp_index=index)
                    msg.append(f"TP{index}→{target_price}({'OK' if result.success else 'FAIL'})")
                if not indexes:
                    msg.append(f"老师TP目标已记录: {new_tp}")
            else:
                msg.append("TP旧单撤销失败，老师目标已记录供协调器重试")

        if update_type == "remove_tp" and new_tp:
            logger.info(f"[{trade.exchange or 'okx'}] 移除TP未改变保护目标，等待明确的新目标价")
            msg.append("移除TP未执行")

        session.commit()
    return True, " | ".join(msg) if msg else "无改动"


# ════════════════════════════════════════════════════════════════════════
# Close 信号处理（重构：四级优先级匹配）
# ════════════════════════════════════════════════════════════════════════


async def apply_close_signal(session: Session, signal: dict,
                              exchange: str = "okx") -> tuple[bool, str]:
    """
    处理 close 信号 — ✅ v2 修复：直接执行平仓，不再设置 CLOSE_PENDING。

    老师要求平仓 → 立即：
      1. resolve_trade()
      2. execute_trade_exit() → 直接调用 OKX create_order(market, reduce_only)
      3. OKX 成交确认
      4. 数据库更新
      5. 记录 CloseHistory

    不等 order_monitor / ExitManager / 任何循环。
    """
    symbol = signal.get("symbol", "")
    ai_dir = signal.get("direction", "") or ""

    # 方向解析
    resolved_dir, dir_source = await resolve_update_direction(
        signal, trade_ex=exchange, session=session
    )
    if dir_source == "rejected":
        return False, f"[{exchange}] {symbol} 双向持仓存在，Close 缺少 direction，已拒绝"
    if resolved_dir:
        signal["direction"] = resolved_dir
        if ai_dir != resolved_dir:
            logger.info(
                f"[CLOSE] {symbol} AI direction={ai_dir or 'null'} "
                f"→ Final={resolved_dir} source={dir_source}"
            )

    # 六级优先级匹配 Trade
    trade, match_source = await _resolve_trade(session, signal, exchange=exchange)

    if trade is None:
        return False, f"[{exchange}] {symbol} 未找到 trade（DB+OKX均无），已忽略"

    logger.info(
        f"[CLOSE] {symbol} Trade匹配成功: TradeID={trade.id} "
        f"match_source={match_source}"
    )

    trade_ex = trade.exchange or "okx"

    trade_lock = await trade_lock_manager.acquire(trade.id)
    async with trade_lock:
        # 入场未成交 → 直接撤单，不等成交
        if trade.amount == 0 and trade.has_open_orders:
            cancelled = 0
            for o in trade.open_orders:
                if o.ft_order_side == trade.entry_side:
                    try:
                        await ex.cancel_order(o.order_id, trade.pair, exchange=trade_ex)
                        o.ft_is_open = False
                        cancelled += 1
                    except Exception as e:
                        logger.warning(f"[{trade_ex}] 撤入场单 {o.order_id}: {e}")
            trade.is_open = False
            trade.exit_reason = "signal_close"
            trade.close_date = datetime.now(timezone.utc)
            trade.position_state = "closed"
            session.commit()

            # 记录 CloseHistory
            try:
                from database.models import CloseHistory
                ch = CloseHistory(
                    trade_id=trade.id,
                    close_type="teacher",
                    exit_reason="signal_close_before_fill",
                )
                session.add(ch)
                session.flush()
            except Exception:
                pass

            return True, f"入场未成交，已撤 {cancelled} 个入场单"

        # ✅ 已持仓 → 直接执行平仓，不等 ExitManager
        # 直接调用 OKX API（市场价全平）
        if not trade.is_open or trade.amount <= 0:
            return False, f"[{exchange}] {trade.pair} 已无持仓"

        # —— 策略：盈利状态不跟随平仓，只在亏损时才跟随老师全平 ——
        pnl_pct = await _trade_pnl_pct(trade)
        if pnl_pct is not None and pnl_pct > 0:
            logger.info(
                f"[CLOSE] {symbol} 当前浮盈 +{pnl_pct * 100:.2f}%，"
                f"按策略不跟随平仓（盈利不平仓）"
            )
            return True, f"{trade.pair} 当前浮盈 +{pnl_pct * 100:.1f}%，未跟随平仓"

        close_pct = float(signal.get("close_pct", 100))

        if close_pct >= 99:
            # 全平
            ok = await execute_trade_exit(trade, session, exit_reason="signal_close", ordertype="market")
            if not ok:
                return False, f"[{exchange}] {trade.pair} 平仓执行失败"

            logger.success(f"[{exchange}] {symbol} 老师平仓已执行 OKX")
            return True, f"{trade.pair} 全平完成"

        else:
            # 部分平仓
            ok = await execute_trade_exit(
                trade, session,
                exit_reason="signal_close",
                sub_trade_amt=trade.amount * close_pct / 100.0,
            )
            if not ok:
                return False, f"[{exchange}] {trade.pair} 部分平仓执行失败"

            logger.success(f"[{exchange}] {symbol} 老师部分平仓 {close_pct}% 已执行 OKX")
            return True, f"{trade.pair} 平{close_pct}% 完成"
