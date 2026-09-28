"""
OKX 收益率回填任务 — 每 10 分钟把最近平仓的 OKX 仓位收益率（pnlRatio）写入数据库。

口径（用户确认）：
  - OKX 仓位历史 /api/v5/account/positions-history 的 pnlRatio 是收益率的唯一真相源
    （= 净盈亏 realizedPnl / 保证金，已含手续费与资金费）；
  - 写入 trades.okx_pnl_ratio / fee_close_cost / funding_fees，并按 OKX 保证金校正
    stake_amount（margin = realizedPnl / pnlRatio）；
  - 不修改 realized_profit（账本盈亏口径保持不变，总收益不受影响）；
  - 覆盖窗口 = positions-history 的 7 天，每 10 分钟增量回填一次，
    只补 okx_pnl_ratio IS NULL 的已平仓交易。

Dashboard 展示盈亏比例时优先使用 okx_pnl_ratio（见 backend/services/trades.py）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from database.db import get_session
from database.models import Trade

INTERVAL_SECONDS = 600
POSITIONS_HISTORY_PATH = "/api/v5/account/positions-history"


def _inst_to_pair(inst: str) -> str:
    """'TRIA-USDT-SWAP' → 'TRIA/USDT:USDT'（注意只取首个 -USDT）。"""
    base = inst[:-5]
    i = base.find("-USDT")
    if i > 0:
        base = base[:i] + "/" + base[i + 1:]
    return f"{base}/USDT:USDT" if ":" not in base else base


def _okx_get(path: str, params: dict) -> dict:
    api_key = os.getenv("OKX_API_KEY", "")
    secret = os.getenv("OKX_API_SECRET", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
    qs = urllib.parse.urlencode(params)
    msg = ts + "GET" + path + "?" + qs
    sign = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    req = urllib.request.Request(
        "https://www.okx.com" + path + "?" + qs,
        headers={
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2.0 * (i + 1))
                continue
            raise
    raise RuntimeError("429 重试耗尽")


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").replace("Z", "")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def backfill_once() -> dict:
    """拉一次 positions-history 并回填缺失的 okx_pnl_ratio。返回统计。"""
    stats = {"positions": 0, "matched": 0, "errors": 0}
    try:
        resp = _okx_get(POSITIONS_HISTORY_PATH, {"instType": "SWAP", "limit": "100"})
        data = resp.get("data") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[RatioBackfill] 拉取 positions-history 失败: {exc}")
        stats["errors"] += 1
        return stats
    stats["positions"] = len(data)

    session = get_session()
    try:
        for p in data:
            if p.get("closeAvgPx") in (None, ""):
                continue
            u_time = datetime.fromtimestamp(int(p["uTime"]) / 1000, tz=timezone.utc)
            pair = _inst_to_pair(p["instId"])
            pnl_ratio = float(p.get("pnlRatio") or 0)
            realized = float(p.get("realizedPnl") or 0)
            rows = session.execute(
                select(Trade).where(
                    Trade.is_open.is_(False),
                    Trade.pair == pair,
                    Trade.okx_pnl_ratio.is_(None),
                )
            ).scalars().all()
            best, best_diff = None, None
            for t in rows:
                dt = _parse_dt(t.close_date.isoformat() if isinstance(t.close_date, datetime) else str(t.close_date or ""))
                if dt is None or abs((dt - u_time).total_seconds()) > 1500:
                    continue
                d = abs((t.realized_profit or 0.0) - realized)
                if best_diff is None or d < best_diff:
                    best, best_diff = t, d
            if best is not None and best_diff <= max(0.05, 0.15 * abs(realized)):
                margin = realized / pnl_ratio if abs(pnl_ratio) > 1e-9 else None
                best.okx_pnl_ratio = pnl_ratio
                best.fee_close_cost = float(p.get("fee") or 0)
                best.funding_fees = float(p.get("fundingFee") or 0)
                if margin and 0 < margin < 100000:
                    best.stake_amount = round(margin, 6)
                stats["matched"] += 1
        session.commit()
        if stats["matched"]:
            logger.info(f"[RatioBackfill] 回填 {stats['matched']} 笔 OKX 收益率（共 {stats['positions']} 条仓位记录）")
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.warning(f"[RatioBackfill] 回填异常: {exc}")
        stats["errors"] += 1
    finally:
        session.close()
    return stats


async def run_ratio_backfill(shutdown_event: asyncio.Event) -> None:
    """后台任务：每 10 分钟回填一次（可被 shutdown_event 中断）。"""
    logger.info("[RatioBackfill] 已启动（每 10 分钟回填 OKX 平仓收益率）")
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=INTERVAL_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
        if shutdown_event.is_set():
            break
        await asyncio.to_thread(backfill_once)
    logger.info("[RatioBackfill] 已停止")
