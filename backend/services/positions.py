"""当前持仓服务。

数据源：机器人库 trades 表（is_open=1），可选叠加 OKX 实时行情计算未实现盈亏。
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from ..database import db
from ..models import bot_models
from . import okx
from .common import as_utc, round2, safe_div, to_float, to_int


def _trades_table():
    return bot_models.get_table("trades")


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _pair_to_inst_id(pair: str) -> str:
    """ccxt 币对 "ETH/USDT:USDT" -> OKX instId "ETH-USDT-SWAP"。"""
    try:
        base, rest = pair.split("/", 1)
        quote = rest.split(":", 1)[0]
        return f"{base}-{quote}-SWAP"
    except (ValueError, AttributeError):
        return pair.replace("/", "-")


def _unrealized_pnl(is_short: bool, open_rate: float, current_price: float, amount: float) -> float:
    if not amount or not open_rate or not current_price:
        return 0.0
    diff = (current_price - open_rate) if not is_short else (open_rate - current_price)
    return diff * amount


async def get_positions(use_live: bool = True) -> list[dict]:
    table = _trades_table()
    if table is None:
        return []
    session = db.bot_session()
    try:
        rows = session.execute(
            select(table).where(table.c.is_open.is_(True)).order_by(table.c.id.desc())
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取持仓失败: {exc}")
        return []
    finally:
        session.close()

    if not rows:
        return []

    # OKX 实时持仓（未实现盈亏/收益率的权威来源）
    okx_positions: dict[tuple[str, str], dict] = {}
    live_prices: dict[str, dict] = {}
    if use_live:
        try:
            for p in await okx.fetch_positions():
                inst, side = p.get("instId") or "", p.get("side") or ""
                if inst:
                    okx_positions[(inst, side)] = p
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"实时持仓获取失败: {exc}")
        # 兜底行情（OKX 上未匹配到的持仓，退化为按现价计算）
        symbols = list({r.get("pair") for r in rows if r.get("pair")})
        try:
            live_prices = await okx.fetch_tickers_batch(symbols)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"实时行情获取失败: {exc}")

    positions = []
    for row in rows:
        pair = row.get("pair") or "—"
        is_short = bool(row.get("is_short"))
        side = "short" if is_short else "long"
        open_rate = to_float(row.get("open_rate"))
        amount = to_float(row.get("amount")) or to_float(row.get("quantity"))
        margin = to_float(row.get("margin")) or to_float(row.get("stake_amount"))
        leverage = to_float(row.get("leverage"), 1.0)
        stop_loss = to_float(row.get("stop_loss"))
        tp1_price = to_float(row.get("tp1_price"))
        tp1_filled_at = _parse_dt(row.get("tp1_filled_at"))

        # 优先使用 OKX 实时持仓数据：币对 "ETH/USDT:USDT" -> instId "ETH-USDT-SWAP"
        inst_id = _pair_to_inst_id(pair) if pair else ""
        op = okx_positions.get((inst_id, side))

        if op is not None:
            # 开仓均价以 OKX avgPx 为准（减仓/多次成交后与 DB 原始记录会不一致）
            open_rate = to_float(op.get("avg_px")) or open_rate
            current_price = to_float(op.get("mark_price")) or to_float(op.get("mark_px")) or open_rate
            unrealized = to_float(op.get("unrealized_pnl")) if op.get("unrealized_pnl") is not None else _unrealized_pnl(is_short, open_rate, current_price, amount)
            if op.get("upl_ratio") is not None:
                roi_pct = round2(to_float(op.get("upl_ratio")) * 100)
            else:
                roi_pct = round2(to_float(op.get("percentage")))
            margin = to_float(op.get("margin")) or margin
            amount = to_float(op.get("contracts")) or amount
            leverage = to_float(op.get("leverage"), 1.0) or leverage
            price_source = "okx_position"
        else:
            ticker = live_prices.get(pair)
            if ticker and to_float(ticker.get("last")) > 0:
                current_price = to_float(ticker.get("last"))
                price_source = "okx_live"
            elif ticker and to_float(ticker.get("mark")) > 0:
                current_price = to_float(ticker.get("mark"))
                price_source = "okx_live"
            else:
                current_price = open_rate
                price_source = "open_rate"
            unrealized = _unrealized_pnl(is_short, open_rate, current_price, amount)
            roi_pct = round2(safe_div(unrealized, margin) * 100)

        # 止盈状态：以 DB 状态机 position_state 为准（open → tp1_filled → partial_tp）
        tp_state = "未触发"
        pos_state = (row.get("position_state") or "").lower()
        if pos_state == "partial_tp":
            # TP2 已触发，剩余仓位由移动止盈止损（Trailing）管理
            tp_state = "移动止盈止损中"
        elif tp1_filled_at is not None or pos_state == "tp1_filled":
            # TP1 已成交（平仓部分仓位），等待 TP2
            tp_state = "已触发TP1"
        elif tp1_price and current_price:
            hit = current_price >= tp1_price if not is_short else current_price <= tp1_price
            if hit:
                tp_state = "已达标"

        positions.append(
            {
                "id": to_int(row.get("id")),
                "pair": pair,
                "direction": "SHORT" if is_short else "LONG",
                "open_price": round2(open_rate, 6),
                "current_price": round2(current_price, 6),
                "price_source": price_source,
                "leverage": to_float(leverage),
                "margin": round2(margin),
                "quantity": round2(to_float(row.get("quantity")) or amount, 6),
                "amount": round2(amount, 6),
                "unrealized_pnl": round2(unrealized, 4),
                "roi_pct": roi_pct,
                "stop_loss": round2(stop_loss, 6) if stop_loss else None,
                "tp1_price": round2(tp1_price, 6) if tp1_price else None,
                "tp_state": tp_state,
                "position_state": row.get("position_state") or "unknown",
                "teacher": row.get("teacher") or row.get("teacher_name") or None,
                "leverage_level": round2(leverage),
                "open_time": as_utc(_parse_dt(row.get("open_date")) or row.get("opened_at")),
            }
        )
    return positions
