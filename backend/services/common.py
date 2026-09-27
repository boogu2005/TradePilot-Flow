"""公共工具：时间序列化、数值安全转换等。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# 北京时间偏移（欧易 App/结算按 UTC+8 切「今日」，非 UTC）
BJ_OFFSET = timedelta(hours=8)


def bj_day_start(now: datetime | None = None) -> datetime:
    """北京时间当日 00:00 对应的 UTC 时刻（tz-aware）。

    欧易「今日」以北京时间 00:00 切日；DB 里 close_date 一律 UTC，
    用返回值做窗口下界即可对齐欧易口径。
    """
    now = now or utc_now()
    return (
        (now + BJ_OFFSET).replace(hour=0, minute=0, second=0, microsecond=0)
        - BJ_OFFSET
    )


def to_float(value: Any, default: float = 0.0) -> float:
    """安全转 float。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> str | None:
    """naive datetime 视为 UTC，序列化为 ISO8601 + Z。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def round2(value: float | None, ndigits: int = 2) -> float:
    if value is None:
        return 0.0
    return round(float(value), ndigits)


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if not denominator:
        return default
    return numerator / denominator
