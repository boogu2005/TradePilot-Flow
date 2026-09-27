"""
统一时间工具 — 解决 SQLite 存储 naive datetime 与 aware datetime 的兼容问题。

SQLAlchemy + SQLite 存储的 datetime 字段没有 tzinfo（naive）。
但代码中大量使用 datetime.now(timezone.utc) 生成 aware datetime。
直接相减会抛出：
  TypeError: can't subtract offset-naive and offset-aware datetimes

所有从数据库读取的时间字段都必须经过 ensure_utc() 处理后再参与计算。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional


def ensure_utc(dt: datetime | None) -> datetime | None:
    """
    将任意 datetime 统一为 UTC aware datetime。

    规则：
    - None → None
    - naive (tzinfo is None) → 视为 UTC，添加 timezone.utc
    - aware → 转换到 UTC（已在 UTC 则不变）

    使用示例：
        open_time = ensure_utc(trade.open_date)
        if open_time:
            elapsed = (datetime.now(timezone.utc) - open_time).total_seconds()
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # 数据库存储的 naive datetime 视为 UTC
        return dt.replace(tzinfo=timezone.utc)
    # 已是 aware datetime → 统一转到 UTC
    return dt.astimezone(timezone.utc)


def utc_now() -> datetime:
    """返回当前 UTC aware datetime。"""
    return datetime.now(timezone.utc)


def dt_seconds_ago(dt: datetime | None) -> float | None:
    """
    计算给定 datetime 距离现在的秒数。
    dt 可以是 naive 或 aware，自动统一处理。
    返回 None 如果 dt 为 None。
    """
    dt = ensure_utc(dt)
    if dt is None:
        return None
    return (utc_now() - dt).total_seconds()
