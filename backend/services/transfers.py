"""充值/提现记录服务。

收益口径铁律：所有收益统计（总收益/今日盈亏/ROI/老师排行榜）只来自交易
（trades.realized_profit），充值提现只记录在此表并在账户概览里单独披露，
绝不混入收益。

数据源：OKX /api/v5/asset/deposit-history 与 withdrawal-history（ccxt 私有接口），
6 小时缓存，幂等 upsert。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from ..database import db
from ..models.dashboard_models import Transfer
from . import okx

_CACHE: dict = {"ts": 0.0, "fetched": False}
_CACHE_TTL = 6 * 3600


def _ensure_table() -> None:
    from ..models.dashboard_models import Base

    Base.metadata.create_all(db.dashboard_engine())


async def _fetch_okx() -> list[dict]:
    ex = okx._get_exchange()
    rows: list[dict] = []
    try:
        resp = await ex.privateGetAssetDepositHistory({"ccy": "USDT", "limit": "100"})
        for d in resp.get("data") or []:
            if (d.get("state") or "") == "2":  # 已完成
                rows.append({
                    "tx_id": f"dep-{d.get('txId')}",
                    "kind": "deposit",
                    "amount": float(d.get("amt") or 0),
                    "ts": datetime.fromtimestamp(int(d["ts"]) / 1000, tz=timezone.utc).replace(tzinfo=None),
                })
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"拉取充值历史失败: {exc}")
    try:
        resp = await ex.privateGetAssetWithdrawalHistory({"ccy": "USDT", "limit": "100"})
        for d in resp.get("data") or []:
            if (d.get("state") or "") == "2":
                rows.append({
                    "tx_id": f"wd-{d.get('wdId')}",
                    "kind": "withdrawal",
                    "amount": float(d.get("amt") or 0),
                    "ts": datetime.fromtimestamp(int(d["ts"]) / 1000, tz=timezone.utc).replace(tzinfo=None),
                })
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"拉取提现历史失败: {exc}")
    # 资金账户账单（内部划转/充值到资金账户等，amt 为空时用 bal 差值）
    try:
        resp = await ex.privateGetAssetBills({"ccy": "USDT", "limit": "100"})
        data = resp.get("data") or []
        prev_bal = None
        for d in reversed(data):  # 接口按时间倒序，反转成升序算 bal 差值
            bal = d.get("bal")
            if bal in (None, ""):
                continue
            bal = float(bal)
            amt = bal - prev_bal if prev_bal is not None else None
            if amt is not None and abs(amt) > 1e-9:
                rows.append({
                    "tx_id": f"fb-{d.get('billId')}",
                    "kind": "transfer",
                    "amount": round(amt, 6),
                    "ts": datetime.fromtimestamp(int(d["ts"]) / 1000, tz=timezone.utc).replace(tzinfo=None),
                })
            prev_bal = bal
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"拉取资金账户账单失败: {exc}")
    return rows


def _upsert(rows: list[dict]) -> None:
    _ensure_table()
    session = db.dashboard_session()
    try:
        for r in rows:
            existing = session.execute(
                select(Transfer).where(Transfer.tx_id == r["tx_id"])
            ).scalar_one_or_none()
            if existing is None:
                session.add(Transfer(**r, state="completed", created_at=datetime.utcnow()))
        session.commit()
    finally:
        session.close()


async def refresh(force: bool = False) -> None:
    """按需刷新充值/提现记录（6h 缓存）。失败静默，不影响其他接口。"""
    now = time.monotonic()
    if not force and _CACHE["fetched"] and now - _CACHE["ts"] < _CACHE_TTL:
        return
    try:
        rows = await _fetch_okx()
        _upsert(rows)
        _CACHE.update(ts=now, fetched=True)
        logger.info(f"[Transfers] 刷新完成: {len(rows)} 条")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[Transfers] 刷新失败: {exc}")


def summary(since: datetime | None = None) -> dict:
    """返回 {deposits, withdrawals, transfers, net}（只做披露，绝不并入收益）。"""
    _ensure_table()
    empty = {"deposits": 0.0, "withdrawals": 0.0, "transfers": 0.0, "net": 0.0}
    session = db.dashboard_session()
    try:
        deposits = withdrawals = transfers_amt = 0.0
        stmt = select(Transfer)
        if since is not None:
            stmt = stmt.where(Transfer.ts >= since)
        for t in session.execute(stmt).scalars():
            if t.kind == "deposit":
                deposits += t.amount or 0.0
            elif t.kind == "withdrawal":
                withdrawals += t.amount or 0.0
            else:
                transfers_amt += t.amount or 0.0
        return {
            "deposits": round(deposits, 2),
            "withdrawals": round(withdrawals, 2),
            "transfers": round(transfers_amt, 2),
            "net": round(deposits - withdrawals + transfers_amt, 2),
        }
    except Exception:  # noqa: BLE001 —— 表不存在等场景
        return empty
    finally:
        session.close()
