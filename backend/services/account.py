"""账户概览 + 机器人状态服务。"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select

from .. import config
from ..database import db
from ..models import bot_models
from . import okx
from .common import as_utc, bj_day_start, round2, safe_div, to_float, to_int, utc_now

# OKX 今日已实现净盈亏缓存（positions-history，5s TTL 对齐 WS 广播节奏）
_today_realized_cache: dict = {"ts": 0.0, "value": None}
_TODAY_PNL_TTL = 5.0


async def _okx_today_realized() -> dict | None:
    """OKX 今日（北京时间日）已实现净盈亏。

    直接读 OKX positions-history：每笔平仓的 realizedPnl 已是欧易算好的净额
    （= pnl + fee + fundingFee，含开平仓手续费与资金费，App 平仓记录同源），
    按 uTime ≥ 北京时间 00:00 求和即可，不依赖机器人 DB —— OKX 直接成交
    （TP/SL/手动平仓）的行可能没同步进 DB，按 DB 求和会漏计且滞后。

    失败返回 None，调用方回落 DB 口径。带 5s TTL 缓存避免每次 WS 广播重复拉。
    """
    if not okx.enabled():
        return None
    cached = _today_realized_cache
    now_mono = time.monotonic()
    if cached["value"] is not None and now_mono - cached["ts"] < _TODAY_PNL_TTL:
        return cached["value"]
    start = bj_day_start()
    try:
        ex = okx._get_exchange()
        resp = await ex.privateGetAccountPositionsHistory(
            {"instType": "SWAP", "limit": "100"}
        )
        data = resp.get("data") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"OKX 今日已实现盈亏拉取失败: {exc}")
        return None
    start_ms = int(start.timestamp() * 1000)
    realized = 0.0
    closes = 0
    for b in data:
        if int(b.get("uTime") or 0) < start_ms:
            continue
        closes += 1
        realized += to_float(b.get("realizedPnl"))
    value = {"realized": realized, "closes": closes}
    cached.update(ts=now_mono, value=value)
    return value


def _trades_table():
    return bot_models.get_table("trades")


def _snapshots_table():
    return bot_models.get_table("account_snapshots")


def _events_table():
    return bot_models.get_table("system_events")


def _last_snapshot() -> dict | None:
    table = _snapshots_table()
    if table is None:
        return None
    session = db.bot_session()
    try:
        row = session.execute(
            select(table).order_by(table.c.time.desc()).limit(1)
        ).mappings().first()
        if not row:
            return None
        return {
            "balance": to_float(row.get("balance")),
            "equity": to_float(row.get("equity")),
            "available": to_float(row.get("available")),
            "unrealized_pnl": to_float(row.get("unrealized_pnl")),
            "realized_pnl": to_float(row.get("realized_pnl")),
            "open_positions": to_int(row.get("open_positions")),
            "time": as_utc(row.get("time")),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取账户快照失败: {exc}")
        return None
    finally:
        session.close()


async def _live_balance() -> dict | None:
    try:
        return await okx.fetch_balance()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"获取实时余额失败: {exc}")
        return None


def _closed_trade_stats(
    start: datetime | None = None, end: datetime | None = None
) -> dict:
    """已平仓交易聚合统计。"""
    table = _trades_table()
    empty = {
        "trade_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "realized_profit": 0.0,
        "total_stake": 0.0,
        "win_rate": 0.0,
    }
    if table is None:
        return empty
    session = db.bot_session()
    try:
        stmt = select(
            func.count(table.c.id),
            func.coalesce(func.sum(table.c.realized_profit), 0),
            func.coalesce(func.sum(table.c.stake_amount), 0),
        ).where(table.c.is_open.is_(False))
        if start is not None:
            stmt = stmt.where(table.c.close_date >= start)
        if end is not None:
            stmt = stmt.where(table.c.close_date < end)
        count, profit, stake = session.execute(stmt).one()
        wins = session.execute(
            select(func.count(table.c.id)).where(
                table.c.is_open.is_(False),
                table.c.realized_profit > 0,
                *(
                    [table.c.close_date >= start] if start is not None else []
                ),
                *([table.c.close_date < end] if end is not None else []),
            )
        ).scalar() or 0
        losses = count - wins
        return {
            "trade_count": to_int(count),
            "win_count": to_int(wins),
            "loss_count": to_int(losses),
            "realized_profit": round2(to_float(profit)),
            "total_stake": round2(to_float(stake)),
            "win_rate": round2(safe_div(to_float(wins), to_int(count)) * 100, 2),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"交易统计失败: {exc}")
        return empty
    finally:
        session.close()


def _open_position_count() -> int:
    table = _trades_table()
    if table is None:
        return 0
    session = db.bot_session()
    try:
        return to_int(
            session.execute(
                select(func.count(table.c.id)).where(table.c.is_open.is_(True))
            ).scalar()
        )
    except Exception:  # noqa: BLE001
        return 0
    finally:
        session.close()


def _last_trade_time() -> str | None:
    table = _trades_table()
    if table is None:
        return None
    session = db.bot_session()
    try:
        row = session.execute(
            select(table.c.close_date, table.c.open_date)
            .order_by(table.c.id.desc())
            .limit(1)
        ).mappings().first()
        if not row:
            return None
        return as_utc(row.get("close_date") or row.get("open_date"))
    except Exception:  # noqa: BLE001
        return None
    finally:
        session.close()


def _latest_strategy() -> str | None:
    table = _trades_table()
    if table is None or "strategy" not in table.c:
        return None
    session = db.bot_session()
    try:
        return session.execute(
            select(table.c.strategy)
            .where(table.c.strategy.isnot(None))
            .order_by(table.c.id.desc())
            .limit(1)
        ).scalar()
    except Exception:  # noqa: BLE001
        return None
    finally:
        session.close()


def _last_event_time() -> str | None:
    table = _events_table()
    if table is None:
        return None
    session = db.bot_session()
    try:
        return as_utc(
            session.execute(
                select(table.c.time).order_by(table.c.id.desc()).limit(1)
            ).scalar()
        )
    except Exception:  # noqa: BLE001
        return None
    finally:
        session.close()


# systemd 服务状态缓存（避免每次请求都调 systemctl）
_bot_service_cache: dict = {"ts": 0.0, "active": False, "known": False, "checked": False}


def _bot_service_state() -> tuple[bool, bool]:
    """systemd 探测结果 (active, known)。

    known=True 表示 systemctl 探测成功 —— 此时其结论权威，
    known=False 表示本机无 systemd/探测失败，需退化为事件窗口启发。
    """
    now = time.monotonic()
    if _bot_service_cache["checked"] and now - _bot_service_cache["ts"] < 10:
        return _bot_service_cache["active"], _bot_service_cache["known"]
    active = known = False
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "trading-bot.service"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        state = result.stdout.strip()
        # is-active: active→0, inactive→3, failed→3 —— 只要返回了有效状态即 known
        active = state == "active"
        known = state in ("active", "inactive", "failed", "activating", "deactivating")
    except Exception:  # noqa: BLE001
        known = False
    _bot_service_cache.update(ts=now, active=active, known=known, checked=True)
    return active, known


def _bot_online() -> dict:
    """在线判断：systemd 可探测时以其为权威结论（已停止就是离线）；
    systemd 不可用（非 systemd 部署）时，回退到最近事件/成交时间窗口启发。"""
    active, known = _bot_service_state()
    window = timedelta(seconds=config.BOT_ONLINE_WINDOW_SECONDS)
    now = utc_now()
    last_event = _last_event_time()
    last_trade = _last_trade_time()

    def _parse(iso: str | None):
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return None

    candidates = [t for t in (_parse(last_event), _parse(last_trade)) if t]
    online = active
    if not known:
        online = bool(candidates and max(candidates) >= now - window)
    return {"online": online, "last_event_at": last_event, "last_trade_at": last_trade}


async def get_account_summary() -> dict:
    """账户概览（实时 OKX + 数据库兜底）。

    今日收益口径（与欧易 App 一致）：今日（北京时间日）已实现净盈亏
    （OKX positions-history realizedPnl，含手续费资金费）+ 当前持仓浮盈，
    全部来自 OKX 实时流水，随 WS 广播每 5s 刷新；OKX 不可用时回落
    DB trades.realized_profit 求和。
    """
    now = utc_now()
    today_start = bj_day_start(now)
    today_end = bj_day_start(now) + timedelta(days=1)

    today = _closed_trade_stats(start=today_start, end=today_end)
    # 近 7 个自然日（含今日，北京时间）已平仓统计 —— 胜率卡用
    week_start = bj_day_start(now) - timedelta(days=6)
    week7 = _closed_trade_stats(start=week_start, end=today_end)
    total = _closed_trade_stats()
    open_positions = _open_position_count()

    # ---- 余额来源：实时 OKX > 数据库快照 ----
    live = await _live_balance()
    snapshot = _last_snapshot()
    source = "none"
    balance = equity = available = unrealized = 0.0
    if live is not None:
        balance = round2(live.get("total"))
        available = round2(live.get("free"))
        unrealized = round2(live.get("unrealized_pnl"))
        equity = round2(balance + unrealized)
        source = "okx_live"
    elif snapshot is not None:
        balance = snapshot["balance"]
        equity = snapshot["equity"]
        available = snapshot["available"]
        unrealized = snapshot["unrealized_pnl"]
        source = "db_snapshot"

    # ---- 今日收益：OKX 实时流水优先（已实现净盈亏 + 当前浮盈），DB 兜底 ----
    today_pnl = round2(today["realized_profit"])
    today_pnl_source = "db"
    today_realized = round2(today["realized_profit"])
    today_unrealized = 0.0
    if source == "okx_live":
        okx_today = await _okx_today_realized()
        if okx_today is not None:
            today_realized = round2(okx_today["realized"])
            try:
                live_positions = await okx.fetch_positions()
                today_unrealized = round2(
                    sum(to_float(p.get("unrealized_pnl")) for p in live_positions)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"OKX 实时持仓获取失败(今日收益): {exc}")
                today_unrealized = round2(live.get("unrealized_pnl"))
            today_pnl = round2(today_realized + today_unrealized)
            today_pnl_source = "okx"

    today_roi = (
        round2(safe_div(today_pnl, equity) * 100) if equity > 0 else 0.0
    )
    total_roi = (
        round2(safe_div(total["realized_profit"], total["total_stake"]) * 100)
        if total["total_stake"] > 0
        else 0.0
    )

    status = _bot_online()
    # 充值/提现只做披露，绝不并入收益统计（收益口径 = trades.realized_profit 求和）
    from . import transfers

    await transfers.refresh()
    transfers_summary = transfers.summary()
    return {
        "account": {
            "balance": balance,
            "usdt_balance": balance,
            "equity": equity,
            "available": available,
            "open_positions": open_positions,
            "unrealized_pnl": unrealized,
            "today_pnl": today_pnl,
            "today_realized": today_realized,
            "today_unrealized": today_unrealized,
            "today_pnl_source": today_pnl_source,
            "today_roi_pct": today_roi,
            "total_pnl": total["realized_profit"],
            "total_roi_pct": total_roi,
            "total_trades": total["trade_count"],
            "total_win_rate": total["win_rate"],
            "transfers": transfers_summary,
            "source": source,
            "updated_at": as_utc(now),
        },
        "bot": {
            "online": status["online"],
            "last_trade_at": status["last_trade_at"],
            "today_trades": today["trade_count"],
            "today_wins": today["win_count"],
            "today_losses": today["loss_count"],
            "win_rate_7d": week7["win_rate"],
            "closed_7d": week7["trade_count"],
            "wins_7d": week7["win_count"],
            "losses_7d": week7["loss_count"],
            "strategy": _latest_strategy() or "—",
            "last_event_at": status["last_event_at"],
            "db_available": bot_models.is_available(),
        },
    }


def _today_live_point() -> dict | None:
    """今日最新实时点（来自 Dashboard 自采快照，用于曲线今日端点）。"""
    try:
        from ..models.dashboard_models import DashboardEquitySnapshot

        session = db.dashboard_session()
        try:
            snap = (
                session.execute(
                    select(DashboardEquitySnapshot).order_by(DashboardEquitySnapshot.time.desc())
                )
                .scalars()
                .first()
            )
            if snap is None:
                return None
            day = snap.time.strftime("%Y-%m-%d")
            if day != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                return None
            return {
                "time": as_utc(snap.time),
                "balance": round2(snap.balance),
                "equity": round2(snap.equity),
                "available": round2(snap.available),
                "unrealized_pnl": round2(snap.unrealized_pnl),
                "open_positions": to_int(snap.open_positions),
                "source": "okx",
            }
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取今日实时点失败: {exc}")
        return None


def get_account_history(days: int = 30, source: str = "auto") -> list[dict]:
    """资金曲线数据：优先 OKX 账单重建（真实资金曲线），
    其次机器人快照表，最后 Dashboard 自采快照。"""
    table = _snapshots_table()
    from_date = utc_now() - timedelta(days=days)
    rows: list[dict] = []

    # 优先：OKX 账单重建的每日资金曲线
    if source in ("auto", "okx"):
        try:
            from . import equity

            okx_curve = equity.load_daily_curve(days=days)
            if okx_curve:
                rows = okx_curve
                live = _today_live_point()
                if live is not None and rows:
                    # 今日端点替换为实时快照（更精确，含未实现盈亏）
                    rows[-1] = live
                return rows
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取 OKX 资金曲线失败: {exc}")
    if table is not None and source in ("auto", "bot"):
        session = db.bot_session()
        try:
            result = session.execute(
                select(table).where(table.c.time >= from_date).order_by(table.c.time)
            ).mappings()
            for row in result:
                rows.append(
                    {
                        "time": as_utc(row.get("time")),
                        "balance": round2(row.get("balance")),
                        "equity": round2(row.get("equity")),
                        "available": round2(row.get("available")),
                        "unrealized_pnl": round2(row.get("unrealized_pnl")),
                        "open_positions": to_int(row.get("open_positions")),
                        "source": "bot",
                    }
                )
        finally:
            session.close()
    if not rows and source in ("auto", "dashboard"):
        try:
            from ..models.dashboard_models import DashboardEquitySnapshot

            session = db.dashboard_session()
            result = session.execute(
                select(DashboardEquitySnapshot)
                .where(DashboardEquitySnapshot.time >= from_date)
                .order_by(DashboardEquitySnapshot.time)
            ).scalars()
            for snap in result:
                rows.append(
                    {
                        "time": as_utc(snap.time),
                        "balance": round2(snap.balance),
                        "equity": round2(snap.equity),
                        "available": round2(snap.available),
                        "unrealized_pnl": round2(snap.unrealized_pnl),
                        "open_positions": snap.open_positions,
                        "source": "dashboard",
                    }
                )
            session.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取 Dashboard 快照失败: {exc}")
    return rows
