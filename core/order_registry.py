"""
订单注册中心 — 追踪所有订单的生命周期。
支持：TTL 过期、状态查询、缺失检测、价格校验。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Order, Trade
from exchange_engine import exchange as ex



# 默认入场单 TTL
DEFAULT_ENTRY_TTL_HOURS = 72  # 3 天
# TP/SL 永不过期
TP_SL_TTL_HOURS = 0  # 0 = 不限


def set_order_expiry(order: Order, order_type: str):
    """根据订单类型设置过期时间。"""
    if order_type == "entry":
        order.expire_at = datetime.now(timezone.utc) + timedelta(hours=DEFAULT_ENTRY_TTL_HOURS)
    else:
        order.expire_at = None  # TP/SL/trigger 永不过期


def find_expired_entry_orders(session: Session) -> list[Order]:
    """
    查找所有已过期的入场单（status=open，超过 TTL 未成交）。

    严格过滤：只查询 ft_order_role='entry' 的订单，
    防止误取消 TP/SL（它们也可能有 expire_at 字段）。
    """
    now = datetime.now(timezone.utc)
    result = session.query(Order).filter(
        Order.ft_order_role == "entry",  # 关键：只查入场单
        Order.ft_order_side.in_(["buy", "sell"]),
        Order.ft_is_open.is_(True),
        Order.expire_at.isnot(None),
        Order.expire_at < now,
    ).all()
    if result:
        logger.debug(f"[order_registry] 找到 {len(result)} 个过期入场单")
    return result


def find_open_orders_by_trade(session: Session, trade: Trade) -> list[Order]:
    """查某个 Trade 的所有未成交订单。"""
    return session.query(Order).filter(
        Order.ft_trade_id == trade.id,
        Order.ft_is_open.is_(True),
    ).all()


def find_tp_sl_orders_by_trade(session: Session, trade: Trade) -> dict:
    """查某个 Trade 的 TP/SL/Entry 订单。
    返回 {"tp": [...], "sl": [...], "entry": [...]}。
    部署版本只使用 ft_order_role 字段（全新数据库保证该字段始终存在）。"""
    orders = session.query(Order).filter(
        Order.ft_trade_id == trade.id,
    ).all()
    result: dict[str, list] = {"tp": [], "sl": [], "entry": []}
    for o in orders:
        role = (o.ft_order_role or "").lower()
        if role in ("stoploss", "trailing_stop"):
            result["sl"].append(o)
        elif role in ("tp", "roi_exit", "manual_close"):
            result["tp"].append(o)
        elif role == "entry":
            result["entry"].append(o)
    return result


async def cancel_expired_order(session: Session, order: Order) -> bool:
    """取消一笔过期订单。"""
    trade_ex = order.trade.exchange if order.trade else "okx"
    try:
        await ex.cancel_order(order.order_id, order.ft_pair, exchange=trade_ex)
        order.ft_is_open = False
        order.status = "canceled"
        session.commit()
        logger.info(f"[{trade_ex}] 过期取消 {order.ft_order_tag} {order.ft_pair} id={order.order_id}")
        return True
    except Exception as e:
        logger.warning(f"[{trade_ex}] 取消过期订单失败 {order.order_id}: {e}")
        return False
