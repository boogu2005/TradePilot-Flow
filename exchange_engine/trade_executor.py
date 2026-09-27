"""
交易执行器 — 复刻 Freqtrade FreqtradeBot.execute_entry() / execute_trade_exit()。
支持 4 种入场策略（由信号里的 entry_strategy 决定）：
  - market         : 立即市价
  - limit_single   : 单点限价
  - limit_range    : 区间拆 3-5 个限价
  - limit_trigger  : 突破触发（stop_limit 挂在 trigger_price）

⚠️ 限价/突破单下完时 trade.is_open=False, trade.amount=0，amount 由监控循环在成交后更新。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Trade, Order
from exchange_engine import exchange as ex


# DeepSeek 必须严格输出这 4 个值之一，兜底不再走市价
VALID_STRATEGIES = ("market", "limit_single", "limit_range", "limit_trigger")


def _norm_strategy(s: str | None) -> str:
    """归一化: 大写转小写 + 去下划线空格的容错"""
    if not s:
        return "market"
    s = str(s).strip().lower().replace("-", "_").replace(" ", "_")
    if s in VALID_STRATEGIES:
        return s
    # 模糊匹配: limitbreakout→limit_trigger, breakoutlimit→limit_trigger
    if "trigger" in s or "breakout" in s or "stop" in s:
        return "limit_trigger"
    if "range" in s:
        return "limit_range"
    if "limit" in s:
        return "limit_single"
    return ""  # 不在白名单 → 返回空字符串，调用方会拒绝执行


async def _open_one_market(pair: str, side: str, contracts: float, exchange: str = "okx") -> dict | None:
    """市价入场，超限自动拆单（返回第一笔订单用于记录）"""
    max_sz = ex.get_max_market_size(pair, exchange=exchange)
    if max_sz and contracts > max_sz:
        logger.info(f"[{exchange}] 市价拆单 {pair}: {contracts}张 → 每笔≤{max_sz}张")
        first_order = None
        remaining = contracts
        while remaining > 0:
            chunk = min(remaining, max_sz)
            try:
                o = await ex.create_order(pair, "market", side, chunk, exchange=exchange)
                if first_order is None:
                    first_order = o
                filled = float(o.get("filled", chunk) or 0)
                remaining -= filled if filled > 0 else chunk  # use actual fill if available
                logger.info(f"[{exchange}] 拆单完成 chunk={chunk} 剩余={remaining}")
            except Exception as e:
                logger.warning(f"[{exchange}] 市价拆单失败 {pair} chunk={chunk}: {e}")
                return first_order  # 返回已成交的部分
        return first_order
    try:
        return await ex.create_order(pair, "market", side, contracts, exchange=exchange)
    except Exception as e:
        logger.warning(f"[{exchange}] 市价入场失败 symbol={pair} side={side} amount={contracts}: {e}")
        return None


async def _open_one_limit(pair: str, side: str, contracts: float, price: float,
                          reduce_only: bool = False, exchange: str = "okx") -> dict | None:
    try:
        return await ex.create_order(pair, "limit", side, contracts, price,
                                      reduce_only=reduce_only, exchange=exchange)
    except Exception as e:
        logger.warning(f"[{exchange}] 限价入场失败 symbol={pair} side={side} amount={contracts} price={price}: {e}")
        return None


async def _open_one_stop_limit(pair: str, side: str, contracts: float,
                                trigger_price: float, limit_price: float,
                                exchange: str = "okx") -> dict | None:
    """突破触发：stop_limit 单，触发后转限价单"""
    try:
        params = {"stopPrice": trigger_price, "price": limit_price}
        return await ex.create_order(pair, "stop_limit", side, contracts, limit_price,
                                      exchange=exchange, **params)
    except Exception as e:
        logger.warning(f"[{exchange}] 突破入场失败 symbol={pair} side={side} amount={contracts} trigger={trigger_price}: {e}")
        return None


async def _place_pending_entry(
    session: Session, trade: Trade, sub_orders: list[dict], pair: str, side: str
) -> None:
    """把子单挂到 trade.orders"""
    for s in sub_orders:
        order_obj = Order.parse_from_ccxt(s["order"], pair, side)
        order_obj.ft_order_tag = "entry"
        order_obj.ft_order_role = "entry"  # 明确标记为入场委托
        # 入场单 3 天 TTL，超时自动撤销
        from datetime import datetime, timezone, timedelta
        order_obj.expire_at = datetime.now(timezone.utc) + timedelta(hours=72)
        trade.orders.append(order_obj)


async def cancel_existing_entry_orders(
    session: Session, pair: str, exchange: str = "okx"
) -> int:
    """
    取消同币种所有 Limit Entry 订单（无论是否过期）。

    市价入场前调用，确保老师放弃等待，立即成交。
    串行执行：取消 → 等待确认 → 返回。

    返回：取消的订单数量
    """
    # 查询同币种所有未完成的入场单
    entry_orders = session.query(Order).filter(
        Order.ft_pair == pair,
        Order.ft_order_role == "entry",
        Order.ft_is_open == True,
    ).all()

    if not entry_orders:
        logger.info(f"[{exchange}] {pair} 无待取消的 Limit Entry")
        return 0

    cancelled = 0
    for order in entry_orders:
        try:
            await ex.cancel_order(order.order_id, pair, exchange=exchange)
            order.ft_is_open = False
            order.status = "canceled"
            cancelled += 1
            logger.info(
                f"[{exchange}] 取消 Limit Entry {order.order_id} "
                f"(市价入场前清理, pair={pair})"
            )
        except Exception as e:
            err_str = str(e)
            if "51400" in err_str:
                # 订单已不存在
                order.ft_is_open = False
                order.status = "canceled"
                cancelled += 1
                logger.warning(
                    f"[{exchange}] Limit Entry {order.order_id} 已不存在，同步 DB"
                )
            else:
                logger.error(
                    f"[{exchange}] 取消 Limit Entry {order.order_id} 失败: {e}"
                )

    if cancelled:
        session.commit()
        logger.info(f"[{exchange}] {pair} 取消 {cancelled} 个 Limit Entry")

    return cancelled


async def execute_entry(
    session: Session,
    pair: str,
    direction: Literal["long", "short"],
    stake_amount: float,
    leverage: int,
    signal_id: str,
    signal_meta: dict | None = None,
    source_chat_id: str = "", source_group_name: str = "",
    source_message_id: int | None = None,
    ordertype: str = "market",
    entry_low: float | None = None,
    entry_high: float | None = None,
    entry_strategy: str = "market",
    trigger_price: float | None = None,
    exchange: str = "okx",
) -> Trade | None:
    """
    执行入场 — 入场方式由 entry_strategy 决定：
      market         → 1 个市价单
      limit_single   → 1 个限价单 @ entry_low
      limit_range    → 区间拆 3 个限价单 (按金额均分张数)
      limit_trigger  → 1 个 stop_limit 单 (触发价=trigger_price, 限价=entry_low)
    """
    # 0. 策略白名单校验
    strategy = _norm_strategy(entry_strategy)
    if not strategy:
        logger.error(f"[{exchange}] 入场拒绝 {pair} 未识别的 entry_strategy={entry_strategy!r}")
        return None

    # 0.5 币种校验 → pair 直接使用 ccxt 标准 symbol (如 BTC/USDT:USDT)
    ok, ccxt_symbol = ex.validate_symbol(pair, exchange=exchange)
    if not ok:
        available = ex.list_available_symbols(exchange=exchange, limit=20)
        logger.error(
            f"[{exchange}] 入场拒绝 {pair} 不是有效的 USDT 永续合约。"
            f"前20个: {available}..."
        )
        return None
    pair = ccxt_symbol
    logger.debug(f"[{exchange}] 币种校验通过: {pair}")

    is_short = (direction == "short")
    side = "sell" if is_short else "buy"
    pos_side = "short" if is_short else "long"

    # 1. 使用系统杠杆规则覆盖信号中的杠杆
    leverage = ex.get_default_leverage(pair)

    # 1.5 开仓前检查：确保该 symbol 无残留条件单（避免 OKX 59668）
    from exit.protection import ensure_no_algo_orders
    if not await ensure_no_algo_orders(pair, exchange=exchange):
        logger.error(f"[{exchange}] 入场拒绝 {pair} 无法清理残留条件单")
        return None

    # 2. 设置杠杆（OKX 的 set-leverage 同时设置杠杆和逐仓模式）
    if not await ex.set_leverage(pair, leverage, exchange=exchange, side=pos_side):
        logger.error(f"[{exchange}] 入场拒绝 {pair} 杠杆/逐仓设置失败")
        return None

    # 日志：杠杆选择
    _sym_norm = pair.upper().replace("/", "").replace(":USDT", "")
    _major = _sym_norm in ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "SOLUSDT")
    logger.info(
        f"[Leverage] symbol={pair} "
        f"selected={leverage}x "
        f"reason={'major_coin' if _major else 'altcoin'}"
    )

    # 2. 行情
    ticker = await ex.fetch_ticker(pair, exchange=exchange)
    last_price = ticker["last"]

    # 3. 计算张数
    precision = ex.get_precision_amount(pair, exchange=exchange)
    ct_val = ex.get_ct_val(pair, exchange=exchange)
    max_mkt = ex.get_max_market_size(pair, exchange=exchange) or 0

    # ———— 仓位计算调试日志 ————
    logger.info(
        f"[{exchange}] 仓位计算 | pair={pair} price={last_price} "
        f"stake={stake_amount}U leverage={leverage}x "
        f"ctVal={ct_val} maxMktSz={max_mkt}"
    )

    # 名义价值 = margin × leverage，提交给 OKX API。
    # 系统不做预拦截 — OKX API 最终决定是否接受订单。
    # 系统保证：normalize_order_amount() 已校验 limits.amount.min，
    # amount_to_precision() 处理精度，minSz/lotSz 由 CCXT 自动适配。
    notional = stake_amount * leverage

    sub_orders: list[dict] = []
    entry_price_for_trade = last_price

    # ============ market ============
    if strategy == "market":
        # 市价入场前：取消同币种所有未过期的 Limit Entry
        # 串行执行：取消 → 等待确认 → 市价下单
        cancelled = await cancel_existing_entry_orders(session, pair, exchange=exchange)
        if cancelled > 0:
            logger.info(f"[{exchange}] {pair} 市价入场前已取消 {cancelled} 个 Limit Entry")

        contracts = stake_amount * leverage / last_price / ct_val
        contracts = ex.amount_to_precision(pair, contracts, exchange=exchange)
        logger.info(
            f"[{exchange}] 市价计算 | target_usdt={stake_amount}U "
            f"notional={stake_amount*leverage}U "
            f"contracts_before_round={stake_amount * leverage / last_price / ct_val:.4f} "
            f"contracts={contracts}"
        )
        if contracts <= 0:
            logger.error(f"[{exchange}] 入场 {pair} 合约张数=0 (ctVal={ct_val})")
            return None
        # OKX 市价单有最大张数限制，超限则按上限下单
        if max_mkt > 0 and contracts > max_mkt:
            logger.warning(f"[{exchange}] 市价单 {contracts}张 超限(maxMktSz={max_mkt}), 降至上限")
            contracts = max_mkt
        o = await _open_one_market(pair, side, contracts, exchange=exchange)
        if not o:
            logger.error(f"[{exchange}] 入场 {pair} 市价单未成交")
            return None
        sub_orders.append({"order": o, "qty": contracts})
        entry_price_for_trade = o.get("average") or last_price

    # ============ limit_single ============
    elif strategy == "limit_single":
        if not entry_low or entry_low <= 0:
            logger.error(f"[{exchange}] 入场 limit_single 需要 entry_low，但收到 {entry_low}")
            return None
        entry_low = ex.price_to_precision(pair, entry_low, exchange=exchange)
        contracts = stake_amount * leverage / entry_low / ct_val
        contracts = ex.amount_to_precision(pair, contracts, exchange=exchange)
        if contracts <= 0:
            return None
        o = await _open_one_limit(pair, side, contracts, entry_low, exchange=exchange)
        if not o:
            logger.error(f"[{exchange}] 入场 {pair} 限价单下失败")
            return None
        sub_orders.append({"order": o, "qty": contracts})

    # ============ limit_range ============
    elif strategy == "limit_range":
        if not entry_low or not entry_high:
            logger.error(f"[{exchange}] 入场 limit_range 参数非法: low={entry_low} high={entry_high}")
            return None
        # 自动交换：确保 entry_low <= entry_high
        if entry_low > entry_high:
            logger.warning(f"[{exchange}] 入场 limit_range 自动交换: {entry_low} > {entry_high} → {entry_high} < {entry_low}")
            entry_low, entry_high = entry_high, entry_low
        entry_low = ex.price_to_precision(pair, entry_low, exchange=exchange)
        entry_high = ex.price_to_precision(pair, entry_high, exchange=exchange)
        prices = [entry_low, (entry_low + entry_high) / 2, entry_high]
        prices = [ex.price_to_precision(pair, p, exchange=exchange) for p in prices]
        notional_each = stake_amount * leverage / 3
        for p in prices:
            q = notional_each / p / ct_val
            q = ex.amount_to_precision(pair, q, exchange=exchange)
            if q <= 0:
                continue
            o = await _open_one_limit(pair, side, q, p, exchange=exchange)
            if o:
                sub_orders.append({"order": o, "qty": q})
        if not sub_orders:
            logger.error(f"[{exchange}] 入场 {pair} 区间3单全部失败")
            return None
        entry_price_for_trade = (entry_low + entry_high) / 2

    # ============ limit_trigger ============
    elif strategy == "limit_trigger":
        if not entry_low or not trigger_price:
            logger.error(f"[{exchange}] 入场 limit_trigger 缺参数: low={entry_low} trigger={trigger_price}")
            return None
        entry_low = ex.price_to_precision(pair, entry_low, exchange=exchange)
        trigger_price = ex.price_to_precision(pair, trigger_price, exchange=exchange)
        contracts = stake_amount * leverage / entry_low / ct_val
        contracts = ex.amount_to_precision(pair, contracts, exchange=exchange)
        if contracts <= 0:
            return None
        o = await _open_one_stop_limit(pair, side, contracts, trigger_price, entry_low, exchange=exchange)
        if not o:
            logger.error(f"[{exchange}] 入场 {pair} 突破单下失败")
            return None
        sub_orders.append({"order": o, "qty": contracts})
        entry_price_for_trade = entry_low

    if not sub_orders:
        logger.error(f"[{exchange}] 入场 {pair} 没有产生任何子单")
        return None

    is_filled = (strategy == "market")
    total_qty = sum(s["qty"] for s in sub_orders)

    # 4. 建 Trade
    _teacher = (signal_meta or {}).get("source_sender", "")
    trade = Trade(
        pair=pair,
        base_currency=pair.replace("/", "").replace(":USDT", "").replace("USDT", ""),
        stake_currency="USDT",
        stake_amount=stake_amount,
        amount=total_qty if is_filled else 0,
        amount_requested=total_qty,
        is_open=is_filled,
        is_short=is_short,
        open_rate=entry_price_for_trade if is_filled else 0,
        open_rate_requested=entry_price_for_trade,
        open_date=datetime.now(timezone.utc),
        exchange=exchange,
        strategy="telegram_signal",
        leverage=leverage,
        trading_mode="futures",
        signal_id=signal_id,
        signal_meta=signal_meta or {},
        source_chat_id=source_chat_id or None,
        source_group_name=source_group_name or None,
        source_message_id=source_message_id,
        # v2 新增字段
        telegram_message_id=source_message_id,
        teacher=_teacher or None,
        fee_open=0.0004,
        fee_close=0.0004,
    )
    await _place_pending_entry(session, trade, sub_orders, pair, side)

    session.add(trade)
    session.flush()  # 需要 trade.id

    # ———— 更新 ReplyMapping.trade_id ————
    if source_message_id:
        try:
            from database.models import ReplyMapping
            mapping = session.query(ReplyMapping).filter(
                ReplyMapping.telegram_message_id == source_message_id
            ).first()
            if mapping:
                mapping.trade_id = trade.id
                logger.info(f"[映射] ReplyMapping 绑定 Trade: MsgID={source_message_id} TradeID={trade.id}")
        except Exception as e:
            logger.warning(f"[映射] ReplyMapping 绑定失败: {e}")

    session.commit()

    logger.info(
        f"[{exchange}] 入场 {pair} {direction} | 策略={strategy} | "
        f"子单={len(sub_orders)}张 | 期望价={entry_price_for_trade} | "
        f"{'已成交' if is_filled else '挂单中'}"
    )
    return trade


async def execute_trade_exit(
    trade: Trade,
    session: Session,
    exit_reason: str = "signal",
    sub_trade_amt: float | None = None,
    ordertype: str = "market",
) -> bool:
    """
    执行平仓 — 复刻 Freqtrade FreqtradeBot.execute_trade_exit()。

    ===== OKX 真相源 =====
    平仓数量 = OKX 实时持仓（从 Position Snapshot 读取，零 HTTP）
    禁止使用 trade.amount 作为平仓数量。
    ═══ 所有模块共享 Position Snapshot，禁止单独查询 OKX ═══
    ======================
    """
    trade_ex = trade.exchange or "okx"

    # 0. 从 Position Snapshot 读取持仓（零 HTTP，监控循环已刷新）
    pos = ex.get_position_from_snapshot(trade.pair, trade_ex)
    if pos is None:
        logger.warning(f"[{trade_ex}] 平仓 {trade.pair}：OKX 无此仓位，标记 manual_close")
        # 尝试用最新 ticker 价格估算盈亏（无 OKX 订单数据，使用公式回退）
        try:
            ticker = await ex.fetch_ticker(trade.pair, exchange=trade_ex)
            trade.close(ticker["last"])  # okx_realized_pnl=None → 公式回退
        except Exception:
            # 连行情价也拿不到，只能标记关闭，盈亏使用公式回退
            trade.close(trade.close_rate or trade.open_rate or 0)
        trade.exit_reason = "manual_close"
        session.commit()
        return True

    real_contracts = pos["contracts"]
    if real_contracts <= 0:
        logger.warning(f"[{trade_ex}] 平仓跳过 {trade.pair}：OKX 仓位数量=0")
        return False

    # 确定平仓数量：sub_trade_amt 或 OKX 全部持仓
    if sub_trade_amt is not None and sub_trade_amt > 0:
        amount = ex.normalize_order_amount(trade.pair, sub_trade_amt, trade_ex)
    else:
        amount = ex.normalize_order_amount(trade.pair, real_contracts, trade_ex)

    if amount is None or amount <= 0:
        logger.warning(f"[{trade_ex}] 平仓 {trade.pair} 数量不规范={amount} (OKX={real_contracts})")
        return False

    ticker = await ex.fetch_ticker(trade.pair, exchange=trade_ex)
    exit_price = ticker["last"]

    # Step 1: 下平仓单
    try:
        order = await ex.create_order(
            trade.pair, ordertype, trade.exit_side, amount,
            reduce_only=True, exchange=trade_ex,
        )
    except Exception as e:
        logger.error(f"[{trade_ex}] 平仓 下单失败 {trade.pair}: {e}")
        return False

    order_obj = Order.parse_from_ccxt(order, trade.pair, trade.exit_side)
    order_obj.ft_order_tag = exit_reason
    trade.orders.append(order_obj)

    is_full_close = (amount >= real_contracts or abs(amount - real_contracts) < 1e-8)
    if is_full_close:
        trade.close_rate_requested = exit_price
        trade.exit_reason = exit_reason

    # 验证平仓单已成交 — 未成交时不撤保护单防裸仓
    # P2: 不能只看 status。市价单可能返回 status="open"（ccxt 尚未归一化为 closed）
    # 但实际已成交（filled>0）。以"实际成交量>0 或 status 为成交终态"为准，
    # 减少"订单未成交"误报；filled==0 且非成交终态才是真正未成交。
    _status = (order.get("status") or "") if order else ""
    _filled = float(order.get("filled", 0) or 0) if order else 0.0
    is_filled = bool(order) and (_status in ("closed", "filled") or _filled > 0)
    if not is_filled:
        # ccxt 确认延迟: 市价单可能已实际成交但响应未带 filled/status 终态
        # （TRIA 12:17 平仓 / 老师平仓 #597 均报"未成交"但实际已成交）
        # → REST 验证真实仓位（OKX 唯一真相源），区分真未成交与假未成交:
        #   仓位仍在 → 未成交，返回 False（调用方保留仓位，下轮重试）
        #   仓位归零 → 实际已成交，继续按成交流程记账 + 撤保护单
        try:
            _positions_now = await ex.fetch_positions(exchange=trade_ex)
            _still_now = 0.0
            _sym_norm = _normalize_pair(trade.pair)
            for _p in _positions_now:
                if _normalize_pair(str(_p.get("symbol", ""))) == _sym_norm:
                    _still_now = float(_p.get("contracts", 0) or 0)
                    break
        except Exception as _ve:
            logger.warning(f"[{trade_ex}] 平仓 {trade.pair} REST验证仓位失败: {_ve}，保留仓位由下轮重试")
            return False
        if _still_now > 0:
            logger.warning(f"[{trade_ex}] 平仓 {trade.pair} 订单未成交(REST确认仓位仍在 {_still_now}张)，不撤保护单")
            return False
        logger.info(f"[{trade_ex}] 平仓 {trade.pair} 订单响应未确认但REST仓位已归零 → 按成交处理")

    # ⚠️ 不能用 .get(key, default): ccxt 异步响应中 average 键存在但为 None
    # 时不会用默认值 → trade.close(None) 会 TypeError。
    # (2026-09-04 现场: DASH SL平仓触发后此处炸掉, 由 Step0.5 5秒后兜底记账)
    avg = order.get("average") or exit_price

    # 从 OKX 平仓订单响应中提取交易所计算的真实已实现盈亏
    # OKX API v5 在市价全平订单成交后返回 info.pnl 字段
    # 这是交易所的真实结算结果（包含手续费和滑点），应作为唯一真相源
    okx_pnl = None
    if is_full_close:
        try:
            order_info = order.get("info", {})
            if isinstance(order_info, dict):
                okx_pnl_raw = order_info.get("pnl")
                if okx_pnl_raw is not None:
                    okx_pnl = float(okx_pnl_raw)
        except (ValueError, TypeError):
            pass

    if is_full_close:
        trade.close(avg, okx_realized_pnl=okx_pnl)
    else:
        trade.update_trade(order_obj)

    # Step 2: 确认成交后才取消止盈止损单
    for o in trade.open_orders + trade.open_sl_orders:
        try:
            await ex.cancel_order(o.order_id, trade.pair, exchange=trade_ex)
            o.ft_is_open = False
        except Exception:
            pass
    session.flush()

    # Step 3: 验证仓位已关闭 — 从 Position Snapshot 读取（零 HTTP）
    # ═══ 禁止单独查询 OKX：由下一轮监控循环自动对账 ═══
    remaining_pos = ex.get_position_from_snapshot(trade.pair, trade_ex)
    remaining = remaining_pos["contracts"] if remaining_pos else 0
    if remaining > 0:
        logger.warning(f"[{trade_ex}] 平仓 {trade.pair} 后仍有余仓 {remaining}（将在下轮监控循环中对账）")

    session.commit()

    logger.info(
        f"[{trade_ex}] 平仓 {trade.pair} | "
        f"数量={amount}张 (OKX={real_contracts}) | "
        f"{'全平' if is_full_close else '部分'} | 原因={exit_reason}"
    )

    # 全平后立即清理残留条件单（事件驱动，不等巡检）
    if is_full_close:
        from exit.protection import cleanup_trade_orders
        await cleanup_trade_orders(trade, session=session, exchange=trade_ex)

    return True


async def emergency_exit(trade: Trade, session: Session):
    """紧急平仓（止损单创建失败时的兜底）"""
    logger.warning(f"[{trade.exchange}] 紧急平仓 {trade.pair} — 市价全平")
    await execute_trade_exit(trade, session, exit_reason="emergency_exit", ordertype="market")


# ———————— 风控 ————————

async def calculate_position_size(balance_free: float, ratio: float = 0.05) -> float:
    """单笔开仓保证金 = 可用余额 × 仓位比例。

    ratio 由 main.py 开仓 Step C 按「老师月度分层快照」取定（core/position_tier），
    默认参数 0.05 仅作兼容兜底，生产调用方总是显式传入。
    """
    margin = balance_free * ratio
    logger.info(
        f"[PositionSizing] "
        f"balance_free={balance_free:.2f} "
        f"risk_percent={ratio} "
        f"margin={margin:.4f}"
    )
    return margin


def _normalize_pair(pair: str) -> str:
    """统一 pair 格式: AAVE/USDT:USDT → AAVEUSDT"""
    return pair.upper().replace("/", "").replace(":USDT", "").strip()


async def risk_check(
    session: Session,
    symbol: str,
    direction: str,
    max_positions: int = 5,
    daily_max_loss: float = 500.0,
    exchange_name: str | None = None,
    exchange_balances: dict[str, dict] | None = None,
) -> tuple[bool, str]:
    """
    综合风控检查。返回 (通过?, 原因)。

    exchange_name=None 时检查所有活跃交易所，否则只检查指定交易所。
    exchange_balances 可传入调用方已获取的余额字典，避免重复 fetch_balance。
    """
    sym_norm = _normalize_pair(symbol)

    # 1. 同币种同方向检测 — REST 直接查询 OKX 真实持仓
    # ⚠️ 不使用 WS Position Snapshot：WS 断线时 snapshot 为空或过期，
    #    has_same_coin_same_dir 检测不到已有仓位 → 同方向重复开仓（历史故障）。
    #    REST fetch_positions 有 2s 缓存，开仓路径调用频率低，不增加额外开销。
    active_exchanges = ex.get_active_exchanges()
    if exchange_name:
        active_exchanges = [e for e in active_exchanges if e == exchange_name]
    total_positions = 0
    has_same_coin_same_dir = False
    has_same_coin_opposite_dir = False
    for trade_ex in active_exchanges:
        positions = await ex.fetch_positions(exchange=trade_ex)
        if not positions:
            continue
        for p in positions:
            pos_sym = _normalize_pair(p["symbol"])
            pos_side = p.get("side", "").lower()
            pos_dir = "short" if pos_side == "short" else "long"
            if pos_sym == sym_norm:
                if pos_dir == direction:
                    has_same_coin_same_dir = True
                else:
                    has_same_coin_opposite_dir = True
            total_positions += 1

    logger.info(
        f"[风控] 交易所持仓={total_positions}笔 "
        f"(同币同向={'Y' if has_same_coin_same_dir else 'N'} "
        f"同币反向={'Y' if has_same_coin_opposite_dir else 'N'}) "
        f"上限={max_positions}"
    )

    if total_positions >= max_positions:
        return False, f"已达最大持仓数 ({total_positions}/{max_positions})"

    # 2. 同币种同方向：禁止加仓
    if has_same_coin_same_dir:
        return False, f"{sym_norm} 已有同方向持仓，禁止加仓"

    # 3. 同币种挂单上限 — 查本地 DB（挂单未成交只有本地知道）
    active_trades = Trade.get_active_trades(session)
    pending_count = sum(
        1 for t in active_trades
        if _normalize_pair(t.pair) == sym_norm
        and not t.is_open and t.amount == 0
    )
    if pending_count >= 3:
        return False, f"{sym_norm} 挂单已达上限 ({pending_count}/3)"

    # 4. 单日熔断检查（含浮亏，双通道后台监控: WS每5s + REST每60s，此处只读 flag）
    #    daily_loss_breaker 在 order_monitor 中持续评估，不依赖数据库
    from core.daily_loss_breaker import daily_loss_breaker
    if daily_loss_breaker.is_tripped:
        return False, daily_loss_breaker._trip_reason

    # 5. 账户余额检查（尝试所有活跃交易所，有一个有余额即可）
    #    优先使用调用方传入的 exchange_balances，避免重复 API 调用
    any_free = False
    if exchange_balances:
        for e in ex.get_active_exchanges():
            bal = exchange_balances.get(e) or {}
            if bal.get("free", 0) > 0:
                any_free = True
                break
    if not any_free:
        for e in ex.get_active_exchanges():
            try:
                balance = await ex.fetch_balance(exchange=e)
                if balance.get("free", 0) > 0:
                    any_free = True
                    break
            except Exception:
                continue
    if not any_free:
        return False, "所有交易所可用余额为0"

    return True, "风控通过"
