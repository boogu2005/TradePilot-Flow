"""
数据库清理服务 — 自动删除过期数据。

清理规则（与交易线程完全隔离）：
  | 表                  | 保留期 | 清理条件                                                |
  |---------------------|--------|---------------------------------------------------------|
  | trades              | 10 年  | is_open=False, close_date<10y, position_state='closed'  |
  | orders              | 10 年  | ft_is_open=False, order_date<10y, 不属于活跃 trade      |
  | telegram_messages   | 3 个月 | receive_time < 90d                                      |
  | reply_mapping       | 3 个月 | created_at < 90d                                        |
  | signal_logs         | 3 个月 | created_at < 90d                                        |
  | reconciler_logs     | 10 年  | created_at < 10y                                        |
  | recovery_logs       | 10 年  | created_at < 10y                                        |

严禁影响：
  - 活跃持仓（PENDING_ENTRY / OPEN / PARTIAL_TP / CLOSE_PENDING）
  - OKX 对账系统（Reconciler / PositionSync）
  - 状态机（ExitManager / OrderManager / StopLossManager）
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from loguru import logger
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from database.models import (
    Trade,
    Order,
    SignalLog,
    ReconcilerLog,
    RecoveryLog,
    TelegramMessage,
    ReplyMapping,
)


async def cleanup_database(session: Session) -> dict:
    """
    一次完整的数据库清理。按正确顺序执行以防止外键冲突。

    删除顺序：Order → Trade → SignalLog → VACUUM → ANALYZE

    参数:
        session: 独立的数据库 session（非交易线程共享 session）

    返回:
        {"orders_deleted":int, "trades_deleted":int,
         "signal_logs_deleted":int, "vacuum":bool}

    安全说明：
        - 使用 SQL DELETE WHERE 批量操作，绝不遍历 Python list
        - 仅删除已安全关闭超过 30 天的 Trades
        - 保留所有活跃 trades（由 get_active_trades 语义定义）
        - 保留所有活跃 orders（ft_is_open=True 或属于活跃 trade）
        - 删除前先删 orders（外键约束优先）
        - VACUUM 仅在确实删除了数据后执行
    """
    # SQLite 存储 naive datetime，去除 tzinfo 确保字符串比较一致
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # trades 是收益账本的唯一数据源（Dashboard 总收益 = Σ realized_profit），
    # 删除历史交易会直接破坏收益统计，保留期设为 10 年（≈ 永久保留）。
    # 2026-09-05 曾因 30 天保留期误删对账补建的历史交易，教训：账本数据不清理。
    cutoff_10y = now - timedelta(days=3650)
    cutoff_90d = now - timedelta(days=90)

    stats = {
        "orders_deleted": 0,
        "trades_deleted": 0,
        "telegram_messages_deleted": 0,
        "reply_mapping_deleted": 0,
        "signal_logs_deleted": 0,
        "reconciler_logs_deleted": 0,
        "recovery_logs_deleted": 0,
        "vacuum": False,
    }

    # ========================================================
    # Step 1: 确定可删除的 Trade ID
    #
    # 安全条件（只有所有条件同时满足才删）：
    #   1. is_open = False           — trade 已标记关闭
    #   2. close_date IS NOT NULL    — 有明确的平仓时间
    #   3. close_date < 10 年        — 超过保留期限（账本几乎永久保留）
    #   4. position_state = 'closed' — 状态机已到达最终终止态
    #   5. amount = 0                — 无残留仓位
    #
    # 绝不删除的状态（即使 is_open=False）：
    #   - position_state IN ('pending_entry', 'open', 'partial_tp')
    #   - close_date IS NULL（挂单未成交、僵尸恢复中）
    #   - amount > 0（OKX 仍有仓位未同步）
    # ========================================================
    deletable_trade_ids = [
        row[0] for row in
        session.execute(
            select(Trade.id).where(
                Trade.is_open == False,
                Trade.close_date.isnot(None),
                Trade.close_date < cutoff_10y,
                Trade.position_state == "closed",
                Trade.amount == 0,
            )
        ).all()
    ]

    # ========================================================
    # Step 2: 确定活跃 Trade ID — 以此保护 orders 不被误删
    #
    # 活跃 = get_active_trades() 语义（与 models.py 保持一致）：
    #   - is_open=True（持币中）
    #   或
    #   - is_open=False AND amount=0 AND close_date IS NULL（挂单中）
    # ========================================================
    active_trade_ids = [
        row[0] for row in
        session.execute(
            select(Trade.id).where(
                (Trade.is_open == True)
                | ((Trade.is_open == False)
                   & (Trade.amount == 0)
                   & (Trade.close_date.is_(None)))
            )
        ).all()
    ]

    # ========================================================
    # Step 3: 删除过期 Orders（先删，避免外键冲突）
    #
    # 第 3a 批：属于待删除 trades 的所有 orders
    #   → 这些 trades 已确定删除，orders 随主记录清理
    #
    # 第 3b 批：过期孤儿 orders
    #   → ft_is_open=False, order_date<30d, 不属于任何活跃 trade
    #   → 兜底清理：trade 可能已被外部删除，或 order 失去关联
    #
    # 永不删除：
    #   - ft_is_open=True（任何活跃订单）
    #   - 属于活跃 trade 的 orders（由 active_trade_ids 保护）
    # ========================================================
    if deletable_trade_ids:
        result = session.execute(
            delete(Order).where(Order.ft_trade_id.in_(deletable_trade_ids))
        )
        stats["orders_deleted"] += result.rowcount
        logger.debug(f"[清理] 待删 trade 关联 orders: {result.rowcount} 条")

    # 过期孤儿 orders（ft_trade_id 不在活跃列表且已过期，保留 10 年）
    orphan_cond = (
        (Order.ft_is_open == False)
        & (Order.order_date.isnot(None))
        & (Order.order_date < cutoff_10y)
    )
    if active_trade_ids:
        orphan_cond = orphan_cond & Order.ft_trade_id.notin_(active_trade_ids)

    orphan_result = session.execute(delete(Order).where(orphan_cond))
    if orphan_result.rowcount:
        stats["orders_deleted"] += orphan_result.rowcount
        logger.debug(f"[清理] 过期孤儿 orders: {orphan_result.rowcount} 条")

    # ========================================================
    # Step 4: 删除过期 Trades
    #
    # 此时这些 trades 的 orders 已在 Step 3a 中删除完毕，
    # 外键约束不会阻止删除。
    # ========================================================
    if deletable_trade_ids:
        result = session.execute(
            delete(Trade).where(Trade.id.in_(deletable_trade_ids))
        )
        stats["trades_deleted"] += result.rowcount

    # ========================================================
    # Step 5: 删除过期 Telegram 消息（3 个月保留期）
    # ========================================================
    tg_result = session.execute(
        delete(TelegramMessage).where(
            TelegramMessage.receive_time.isnot(None),
            TelegramMessage.receive_time < cutoff_90d,
        )
    )
    if tg_result.rowcount:
        stats["telegram_messages_deleted"] = tg_result.rowcount

    # ========================================================
    # Step 6: 删除过期 ReplyMapping（3 个月保留期）
    #   3 个月前持仓早已平仓（最大持仓 7 天），映射已无操作价值
    # ========================================================
    rm_result = session.execute(
        delete(ReplyMapping).where(
            ReplyMapping.created_at.isnot(None),
            ReplyMapping.created_at < cutoff_90d,
        )
    )
    if rm_result.rowcount:
        stats["reply_mapping_deleted"] = rm_result.rowcount

    # ========================================================
    # Step 7: 删除过期 SignalLog（3 个月保留期）
    # ========================================================
    sig_result = session.execute(
        delete(SignalLog).where(
            SignalLog.created_at.isnot(None),
            SignalLog.created_at < cutoff_90d,
        )
    )
    if sig_result.rowcount:
        stats["signal_logs_deleted"] += sig_result.rowcount

    # ========================================================
    # Step 8: 删除过期 ReconcilerLog（10 年保留期）
    # ========================================================
    rec_result = session.execute(
        delete(ReconcilerLog).where(
            ReconcilerLog.created_at.isnot(None),
            ReconcilerLog.created_at < cutoff_10y,
        )
    )
    if rec_result.rowcount:
        stats["reconciler_logs_deleted"] = rec_result.rowcount

    # ========================================================
    # Step 9: 删除过期 RecoveryLog（10 年保留期）
    # ========================================================
    rec2_result = session.execute(
        delete(RecoveryLog).where(
            RecoveryLog.created_at.isnot(None),
            RecoveryLog.created_at < cutoff_10y,
        )
    )
    if rec2_result.rowcount:
        stats["recovery_logs_deleted"] = rec2_result.rowcount

    # ========================================================
    # Step 10: 提交事务
    # ========================================================
    session.commit()

    total_deleted = sum(v for k, v in stats.items() if k != "vacuum")
    if total_deleted > 0:
        # ========================================================
        # Step 8: VACUUM + ANALYZE
        #
        # VACUUM 回收 SQLite 空闲页空间。仅在删除数据后执行。
        # 在 SQLite WAL 模式下 VACUUM 会创建新的 WAL 文件，
        # 但不会阻塞并发读。
        #
        # VACUUM 非事务性语句：SQLAlchemy 会在执行前自动提交
        # 隐式事务。我们已在 Step 7 commit，此处安全。
        # ========================================================
        try:
            session.execute(text("VACUUM"))
            session.execute(text("ANALYZE"))
            stats["vacuum"] = True
            logger.debug("[清理] VACUUM + ANALYZE 完成")
        except Exception as e:
            logger.warning(f"[清理] VACUUM/ANALYZE 失败（非致命）: {e}")

        detail = " | ".join(f"{k}={v}" for k, v in stats.items() if v)
        logger.info(f"[清理] 数据库清理完成: {detail}")
    else:
        logger.debug("[清理] 无过期数据需要清理")

    return stats
