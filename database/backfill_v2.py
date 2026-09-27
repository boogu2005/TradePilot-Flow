"""
数据库回填迁移脚本 — 将现有数据填入 v2 新表。

运行方式：
  python database/backfill_v2.py

回填内容：
1. telegram_messages ← 从 Trade.signal_meta.tg_msg_id + SignalLog
2. reply_mapping     ← 从 Trade.signal_meta.tg_msg_id + signal_id
3. Trade.telegram_message_id ← 从 Trade.signal_meta.tg_msg_id
4. Trade.teacher             ← 从 Trade.signal_meta.source_sender
5. SignalLog.tg_msg_id       ← 从 Trade.signal_meta.tg_msg_id 反查
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from loguru import logger
from database.db import init_db, get_session, close_db
from database.models import Trade, SignalLog, TelegramMessage, ReplyMapping
from sqlalchemy import text


def backfill_telegram_messages(session) -> int:
    """
    从 Trade.signal_meta.tg_msg_id + SignalLog 回填 telegram_messages。
    """
    count = 0
    trades = session.query(Trade).all()
    for t in trades:
        meta = t.signal_meta or {}
        tg_msg_id = meta.get("tg_msg_id")
        if not tg_msg_id:
            continue

        # 检查是否已有记录
        existing = session.query(TelegramMessage).filter(
            TelegramMessage.telegram_message_id == tg_msg_id
        ).first()
        if existing:
            continue

        # 查找对应的 SignalLog
        signal_log = None
        if t.signal_id:
            signal_log = session.query(SignalLog).filter(
                SignalLog.signal_id == t.signal_id
            ).first()

        tm = TelegramMessage(
            telegram_message_id=tg_msg_id,
            reply_to_message_id=None,
            chat_id=t.source_chat_id or "",
            chat_name=t.source_group_name or "",
            sender_name=meta.get("source_sender", ""),
            text=signal_log.raw_text if signal_log else None,
            message_type="signal",
            receive_time=t.open_date or datetime.now(timezone.utc),
        )
        session.add(tm)
        count += 1
        if count % 50 == 0:
            session.flush()

    session.commit()
    logger.info(f"回填 telegram_messages: {count} 条")
    return count


def backfill_reply_mapping(session) -> int:
    """
    从 Trade.signal_meta.tg_msg_id + signal_id 回填 reply_mapping。
    """
    count = 0
    trades = session.query(Trade).all()
    for t in trades:
        meta = t.signal_meta or {}
        tg_msg_id = meta.get("tg_msg_id")
        if not tg_msg_id:
            continue

        # 检查是否已有记录
        existing = session.query(ReplyMapping).filter(
            ReplyMapping.telegram_message_id == tg_msg_id
        ).first()
        if existing:
            continue

        sender = meta.get("source_sender", "")
        pair = t.pair or ""

        mapping = ReplyMapping(
            telegram_message_id=tg_msg_id,
            signal_id=t.signal_id,
            trade_id=t.id,
            teacher=sender,
            symbol=pair.replace("/", "").replace(":USDT", ""),
            chat_id=t.source_chat_id or "",
            signal_type="new",
        )
        session.add(mapping)
        count += 1
        if count % 50 == 0:
            session.flush()

    session.commit()
    logger.info(f"回填 reply_mapping: {count} 条")
    return count


def backfill_trade_v2_fields(session) -> int:
    """
    回填 Trade.v2 新增字段（telegram_message_id, teacher）。
    """
    count = 0
    trades = session.query(Trade).all()
    for t in trades:
        changed = False
        meta = t.signal_meta or {}

        # telegram_message_id
        if t.telegram_message_id is None and meta.get("tg_msg_id"):
            t.telegram_message_id = meta["tg_msg_id"]
            changed = True

        # teacher
        if t.teacher is None and meta.get("source_sender"):
            t.teacher = meta["source_sender"]
            changed = True

        if changed:
            count += 1

    session.commit()
    logger.info(f"回填 Trade v2 字段: {count} 条")
    return count


def backfill_signal_log_v2_fields(session) -> int:
    """
    回填 SignalLog.v2 新增字段（tg_msg_id）。
    通过 Trade.signal_meta.tg_msg_id 反查 SignalLog。
    """
    count = 0
    signal_logs = session.query(SignalLog).all()
    for sl in signal_logs:
        if sl.tg_msg_id is not None:
            continue

        # 通过 Trade.signal_id 反查
        trade = session.query(Trade).filter(
            Trade.signal_id == sl.signal_id
        ).first()
        if trade:
            meta = trade.signal_meta or {}
            if meta.get("tg_msg_id"):
                sl.tg_msg_id = meta["tg_msg_id"]
                sl.chat_id = sl.tg_group_id
                sl.teacher = sl.tg_sender_name
                count += 1

    session.commit()
    logger.info(f"回填 SignalLog v2 字段: {count} 条")
    return count


def main():
    logger.info("=" * 60)
    logger.info("v2 数据库回填迁移开始")
    logger.info("=" * 60)

    init_db()
    session = get_session()

    try:
        c1 = backfill_trade_v2_fields(session)
        c2 = backfill_signal_log_v2_fields(session)
        c3 = backfill_telegram_messages(session)
        c4 = backfill_reply_mapping(session)

        logger.info("=" * 60)
        logger.info(f"回填完成: Trade={c1} SignalLog={c2} TelegramMsg={c3} ReplyMapping={c4}")
        logger.info("=" * 60)
    finally:
        session.close()
        close_db()


if __name__ == "__main__":
    main()
