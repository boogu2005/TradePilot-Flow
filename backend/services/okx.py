"""OKX 实时数据客户端（可选）。

Dashboard 可选用机器人同款 OKX 凭证拉取实时余额/行情/持仓，
失败时静默降级（返回 None），绝不阻断只读数据库功能。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from .. import config


_exchange = None
_balance_cache: dict[str, Any] | None = None
_balance_ts = 0.0
_ticker_cache: dict[str, dict] = {}
_ticker_ts: dict[str, float] = {}
_positions_cache: list[dict] | None = None
_positions_ts = 0.0

BALANCE_TTL = 5.0
TICKER_TTL = 3.0
POSITIONS_TTL = 5.0


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _to_ccxt_symbol(symbol: str) -> str:
    """把 OKX instId(BTC-USDT-SWAP) 或 ccxt(BTC/USDT:USDT) 统一成 ccxt 符号。"""
    if not symbol:
        return ""
    if ":" in symbol:
        return symbol  # 已是 ccxt 格式
    return f"{symbol.replace('-SWAP', '').replace('-', '/')}:USDT"


def enabled() -> bool:
    if not config.LIVE_OKX_ENABLED:
        return False
    return bool(config.OKX_API_KEY and config.OKX_API_SECRET and config.OKX_PASSPHRASE)


def _get_exchange():
    global _exchange
    if _exchange is None:
        import ccxt.async_support as ccxt_async

        kwargs: dict[str, Any] = {
            "apiKey": config.OKX_API_KEY,
            "secret": config.OKX_API_SECRET,
            "password": config.OKX_PASSPHRASE,
            "enableRateLimit": True,
            "options": {"defaultType": "swap", "adjustForTimeDifference": True},
        }
        if config.EXCHANGE_PROXY:
            kwargs["aiohttp_proxy"] = config.EXCHANGE_PROXY
        _exchange = ccxt_async.okx(kwargs)
    return _exchange


async def close() -> None:
    global _exchange
    if _exchange is not None:
        try:
            await _exchange.close()
        except Exception:  # noqa: BLE001
            pass
        _exchange = None


async def fetch_balance() -> dict | None:
    """返回 {total, free, unrealized_pnl} 或 None。"""
    global _balance_cache, _balance_ts
    if not enabled():
        return None
    now = time.monotonic()
    if _balance_cache is not None and now - _balance_ts < BALANCE_TTL:
        return _balance_cache
    try:
        ex = _get_exchange()
        bal = await ex.fetch_balance(params={"type": "swap"})
        usdt = bal.get("USDT", {})
        free = float(usdt.get("free", 0) or 0)
        total = float(usdt.get("total", 0) or 0)
        if free == 0 and total == 0:
            bal2 = await ex.fetch_balance(params={"type": "trading"})
            u2 = bal2.get("USDT", {})
            free2 = float(u2.get("free", 0) or 0)
            total2 = float(u2.get("total", 0) or 0)
            if free2 or total2:
                free, total = free2, total2
        unrealized = 0.0
        try:
            unrealized = float(
                bal.get("info", {}).get("totalUnrealizedProfit", 0) or 0
            )
        except (TypeError, ValueError):
            pass
        result = {"total": total, "free": free, "unrealized_pnl": unrealized}
        _balance_cache, _balance_ts = result, now
        return result
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"OKX 实时余额获取失败: {exc}")
        return None


async def fetch_ticker(symbol: str) -> dict | None:
    """返回 {last, mark} 或 None。symbol 如 BTC-USDT-SWAP。"""
    if not enabled():
        return None
    now = time.monotonic()
    if symbol in _ticker_ts and now - _ticker_ts[symbol] < TICKER_TTL:
        return _ticker_cache[symbol]
    try:
        ex = _get_exchange()
        ccxt_sym = _to_ccxt_symbol(symbol)
        t = await ex.fetch_ticker(ccxt_sym)
        info = t.get("info", {}) or {}
        result = {
            "last": float(t.get("last", 0) or 0),
            "mark": float(info.get("markPx") or t.get("last") or 0),
        }
        _ticker_cache[symbol] = result
        _ticker_ts[symbol] = now
        return result
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"OKX 实时行情获取失败 {symbol}: {exc}")
        return None


async def fetch_tickers_batch(symbols: list[str]) -> dict[str, dict]:
    """批量获取行情，逐 symbol 容错。"""
    result: dict[str, dict] = {}
    if not symbols:
        return result
    tasks = [fetch_ticker(s) for s in symbols]
    values = await asyncio.gather(*tasks)
    for symbol, value in zip(symbols, values):
        if value:
            result[symbol] = value
    return result


async def fetch_positions() -> list[dict]:
    """OKX 实时持仓（规范化字段）。失败返回 []，带 5s 缓存。

    未实现盈亏/收益率直接使用 OKX 权威数据：
    - unrealized_pnl = upl（USDT）
    - upl_ratio / percentage = 收益率（-0.5 表示 -50%；percentage 已是百分比）
    """
    global _positions_cache, _positions_ts
    if not enabled():
        return []
    now = time.monotonic()
    if _positions_cache is not None and now - _positions_ts < POSITIONS_TTL:
        return _positions_cache
    try:
        ex = _get_exchange()
        raw = await ex.fetch_positions()
        result: list[dict] = []
        for p in raw:
            info = p.get("info", {}) or {}
            result.append(
                {
                    "instId": info.get("instId") or "",
                    "side": (p.get("side") or "").lower(),  # long / short
                    "entry_price": _to_float(p.get("entryPrice")),
                    "mark_price": _to_float(p.get("markPrice")),
                    "contracts": _to_float(p.get("contracts")),
                    "unrealized_pnl": _to_float(p.get("unrealizedPnl")),
                    "percentage": _to_float(p.get("percentage")),
                    "leverage": _to_float(p.get("leverage"), 1.0),
                    "upl": _to_float(info.get("upl")),
                    "upl_ratio": _to_float(info.get("uplRatio")),
                    # OKX 孤立保证金仓位: margin 字段才是当前保证金, imr 可能是空串
                    "margin": _to_float(info.get("margin")) or _to_float(info.get("imr")),
                    "avg_px": _to_float(info.get("avgPx")),
                    "mark_px": _to_float(info.get("markPx")),
                    "pos": _to_float(info.get("pos")),
                }
            )
        _positions_cache, _positions_ts = result, now
        return result
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"OKX 实时持仓获取失败: {exc}")
        return []
