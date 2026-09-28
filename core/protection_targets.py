"""Resolve the protection prices requested by a signal for one position."""

from __future__ import annotations

import math


def positive_price(value) -> float | None:
    """Accept finite, positive prices only; never turn a flag into a price."""
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        value = value.get("price", value.get("target"))
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def normalize_tp_prices(raw) -> list[float]:
    """Keep up to two ordered, distinct teacher targets."""
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    prices = []
    for value in values:
        price = positive_price(value)
        if price is not None and price not in prices:
            prices.append(price)
        if len(prices) == 2:
            break
    return prices


def teacher_tp_prices(trade) -> list[float]:
    meta = trade.signal_meta or {}
    raw = meta.get("_teacher_tp_prices", meta.get("take_profit"))
    return normalize_tp_prices(raw)


def teacher_tp_start_index(trade) -> int:
    meta = trade.signal_meta or {}
    return 2 if meta.get("_teacher_tp_start_index") == 2 else 1


def single_teacher_tp(trade) -> bool:
    return len(teacher_tp_prices(trade)) == 1


def teacher_tp_stage_confirmed(trade, index: int, live_contracts: float) -> bool:
    """Advance a two-level teacher TP stage only after its share actually closes."""
    if len(teacher_tp_prices(trade)) != 2:
        return False
    original = positive_price(getattr(trade, "amount_requested", None))
    if original is None or live_contracts <= 0:
        return False
    remaining = 0.70 if index == 1 else 0.35
    return live_contracts <= original * remaining * 1.03


def teacher_sl_price(trade) -> float | None:
    meta = trade.signal_meta or {}
    return positive_price(meta.get("_teacher_sl_price", meta.get("stop_loss")))


def initial_sl_price(trade) -> float | None:
    """Rehang at the teacher's latest SL, or at the fixed initial SL."""
    return teacher_sl_price(trade) or positive_price(trade.initial_stop_loss)


def tp_price_for(trade, index: int) -> float | None:
    """A teacher TP list is complete: absent levels must stay absent."""
    teacher = teacher_tp_prices(trade)
    if teacher:
        offset = index - teacher_tp_start_index(trade)
        return teacher[offset] if 0 <= offset < len(teacher) else None
    stored = positive_price(getattr(trade, f"tp{index}_price", None))
    if stored is not None:
        return stored
    entry = positive_price(trade.open_rate)
    if entry is None:
        return None
    from core.config_loader import load_config
    cfg = load_config().get("risk", {}).get(f"tp{index}", {})
    pct = float(cfg.get("profit_pct", 0.03 if index == 1 else 0.06))
    return entry * (1 - pct if trade.is_short else 1 + pct)
