"""OKX 账户资金曲线服务（基于账单重建）。

OKX 没有现成的历史权益曲线接口，但 `/api/v5/account/bills-archive` 返回的每条
账户账单都带 `bal`（该币种变动后的余额）和 `ts`（时间戳）。把近 N 天的账单
按天聚合成"每日余额"即可得到真实的账户资金曲线（含手续费、资金费、转账等
全部资金变动）。

数据落 Dashboard 自有库 okx_equity_points，绝不写机器人库。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from .. import config
from ..database import db
from ..models.dashboard_models import OkxEquityPoint
from . import okx

UTC = timezone.utc


async def _fetch_bills(since_ms: int, max_pages: int = 200) -> list[tuple[int, float]]:
    """分页拉取 OKX 账户账单，返回 [(ts_ms, usdt_balance_after), ...]（时间升序）。"""
    if not okx.enabled():
        return []
    ex = okx._get_exchange()
    rows: list[tuple[int, float]] = []
    after: str | None = None
    for _ in range(max_pages):
        params: dict[str, str] = {"limit": "100"}
        if after:
            params["after"] = after
        try:
            resp = await ex.privateGetAccountBillsArchive(params)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"OKX 账单分页拉取失败: {exc}")
            break
        data = resp.get("data") or []
        if not data:
            break
        # 只保留 USDT 余额变动行（bills 最新在前）
        for r in data:
            bal = r.get("bal")
            if r.get("ccy") == "USDT" and bal not in (None, ""):
                rows.append((int(r["ts"]), float(bal)))
        oldest_bill_id = data[-1].get("billId")
        oldest_ts = int(data[-1]["ts"])
        if oldest_ts < since_ms:
            break
        if not oldest_bill_id or oldest_bill_id == after:
            break  # 分页无进展，避免死循环
        after = oldest_bill_id
    rows.sort(key=lambda x: x[0])
    return rows


def _build_daily_curve(rows: list[tuple[int, float]], days: int) -> list[dict]:
    """把账单聚合成每日余额点（无账单的日沿用上一日余额 = 资金未变）。"""
    now = datetime.now(UTC)
    start_day = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # 该日最后一笔账单后的余额
    day_last: dict[str, float] = {}
    for ts, bal in rows:
        d = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")
        day_last[d] = bal
    # 窗口起始前最近一笔余额作为初始值
    carry = 0.0
    for ts, bal in rows:
        d = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")
        if d < start_day.strftime("%Y-%m-%d"):
            carry = bal
        else:
            break

    points: list[dict] = []
    cur = start_day
    while cur.date() <= now.date():
        key = cur.strftime("%Y-%m-%d")
        if key in day_last:
            carry = day_last[key]
        points.append(
            {
                "day": key,
                "time": f"{key}T00:00:00Z",
                "balance": round(carry, 8),
                "equity": round(carry, 8),
            }
        )
        cur += timedelta(days=1)
    return points


async def rebuild_daily_curve(days: int = 30) -> int:
    """重建近 N 天每日余额曲线并落库，返回写入/更新的天数。"""
    if not okx.enabled():
        logger.info("OKX 未启用，跳过资金曲线重建")
        return 0
    since_ms = int(
        (datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000
    )
    rows = await _fetch_bills(since_ms)
    if not rows:
        logger.warning("OKX 账单为空，资金曲线未更新")
        return 0
    points = _build_daily_curve(rows, days)
    session = db.dashboard_session()
    try:
        session.execute(OkxEquityPoint.__table__.delete())
        now = datetime.now(UTC).replace(tzinfo=None)
        for p in points:
            session.add(
                OkxEquityPoint(
                    day=p["day"], balance=p["balance"], equity=p["equity"], updated_at=now
                )
            )
        session.commit()
        logger.info(
            f"资金曲线已重建：{len(points)} 天（{points[0]['day']} ~ {points[-1]['day']}）"
        )
        return len(points)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.debug(f"资金曲线落库失败: {exc}")
        return 0
    finally:
        session.close()


def load_daily_curve(days: int = 30) -> list[dict]:
    """读取已存储的每日资金曲线（时间升序）。"""
    session = db.dashboard_session()
    try:
        rows = session.execute(
            select(OkxEquityPoint).order_by(OkxEquityPoint.day)
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取资金曲线失败: {exc}")
        return []
    finally:
        session.close()
    from_date = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [
        {
            "time": f"{p.day}T00:00:00Z",
            "balance": round(p.balance, 8),
            "equity": round(p.equity, 8),
            "available": round(p.balance, 8),
            "unrealized_pnl": 0.0,
            "open_positions": 0,
            "source": "okx",
        }
        for p in rows
        if p.day >= from_date
    ]


async def equity_curve_builder() -> None:
    """后台任务：启动时重建一次资金曲线，之后周期性刷新（保持今日数据新鲜）。"""
    if not okx.enabled():
        return
    logger.info("启动资金曲线构建任务（OKX 账单）")
    await rebuild_daily_curve(config.EQUITY_CURVE_DAYS)
    while True:
        await asyncio.sleep(config.EQUITY_REFRESH_SECONDS)
        try:
            await rebuild_daily_curve(config.EQUITY_CURVE_DAYS)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"资金曲线周期刷新失败: {exc}")
