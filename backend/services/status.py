"""机器人运行状态服务（纯只读）。

为「机器人状态」页提供三类数据，全部只读：
1. systemd 服务状态（systemctl show 单次子进程拿全字段，带短缓存）；
2. 机器人 stdout 日志尾部（只读文件末 256KB，绝不整读 45MB）；
3. 今日成交概况 + 最近平仓（只读机器人库 trades 表）。

本模块不提供任何控制能力（无启停/无写库/无参数修改）。
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import func, select

from .. import config
from ..database import db
from ..models import bot_models
from .common import as_utc, round2, safe_div, to_float, to_int, utc_now

# systemd 探测缓存（避免轮询页高频调用 systemctl）
_systemd_cache: dict = {"ts": 0.0, "value": None}
# 日志尾部缓存
_log_cache: dict = {"ts": 0.0, "value": None, "max_lines": 0}

_SYSTEMD_PROPS = (
    "ActiveState,SubState,LoadState,MainPID,ActiveEnterTimestamp,"
    "ExecMainStartTimestamp,NRestarts,MemoryCurrent"
)

_TS_RE = re.compile(r"\w{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_LOCAL_TZ = timezone(timedelta(hours=8))  # 服务器本地时区（CST/UTC+8）


def _parse_systemd_ts(raw: str | None) -> str | None:
    """"Fri 2026-09-04 00:20:02 CST" → ISO8601(+08:00);解析失败返回 None。"""
    if not raw:
        return None
    m = _TS_RE.search(raw)
    if not m:
        return None
    try:
        naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=_LOCAL_TZ).isoformat()


def probe_systemd() -> dict:
    """systemd 服务状态（systemctl show 一次取全字段，8s 缓存）。"""
    now = time.monotonic()
    if _systemd_cache["value"] is not None and now - _systemd_cache["ts"] < 8:
        return _systemd_cache["value"]

    empty = {
        "name": config.BOT_SERVICE_NAME,
        "probe_ok": False,
        "active_state": "unknown",
        "sub_state": "",
        "load_state": "",
        "pid": None,
        "since": None,
        "since_raw": None,
        "restarts": None,
        "memory_bytes": None,
    }
    try:
        result = subprocess.run(
            ["systemctl", "show", config.BOT_SERVICE_NAME, "--property=" + _SYSTEMD_PROPS],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            value = empty
        else:
            fields: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, _, val = line.partition("=")
                fields[key] = val.strip()

            def _int_or_none(key: str) -> int | None:
                raw = fields.get(key, "")
                try:
                    parsed = int(raw)
                except (TypeError, ValueError):
                    return None
                return parsed or None  # 0（无进程）→ None

            since_raw = fields.get("ExecMainStartTimestamp") or fields.get("ActiveEnterTimestamp")
            value = {
                "name": config.BOT_SERVICE_NAME,
                "probe_ok": True,
                "active_state": fields.get("ActiveState", "unknown"),
                "sub_state": fields.get("SubState", ""),
                "load_state": fields.get("LoadState", ""),
                "pid": _int_or_none("MainPID"),
                "since": _parse_systemd_ts(since_raw),
                "since_raw": since_raw or None,
                "restarts": _int_or_none("NRestarts"),
                "memory_bytes": _int_or_none("MemoryCurrent"),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"systemctl show 探测失败: {exc}")
        value = empty
    _systemd_cache.update(ts=now, value=value)
    return value


def read_log_tail(max_lines: int = 300) -> dict:
    """机器人 stdout 日志尾部（末 256KB → 取末 max_lines 行，新→旧）。"""
    now = time.monotonic()
    if (
        _log_cache["value"] is not None
        and now - _log_cache["ts"] < 5
        and _log_cache["max_lines"] == max_lines
    ):
        return _log_cache["value"]

    path = Path(config.BOT_LOG_PATH)
    empty = {"file": path.name, "available": False, "lines": [], "truncated": False}
    if not path.is_file():
        _log_cache.update(ts=now, value=empty, max_lines=max_lines)
        return empty

    tail_bytes = 256 * 1024  # 256KB
    lines: list[str] = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            if fh.tell() > 0:
                fh.readline()  # 丢弃半行
            chunk = fh.read().decode("utf-8", errors="replace")
        all_lines = chunk.splitlines()
        picked = all_lines[-max_lines:]
        picked.reverse()  # 新 → 旧，前端直接顺序渲染
        lines = [ln.strip() for ln in picked]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取机器人日志失败: {exc}")
        lines = []
    value = {
        "file": path.name,
        "available": True,
        "lines": lines,
        "truncated": len(lines) >= max_lines,
    }
    _log_cache.update(ts=now, value=value, max_lines=max_lines)
    return value


def _today_window() -> tuple[datetime, datetime]:
    """北京时间(UTC+8)今日窗口 —— 与欧易 App「今日」口径一致（DB close_date 为 UTC）。"""
    now = utc_now()
    start = (
        (now + timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(hours=8)
    )
    return start, start + timedelta(days=1)


def today_and_recent() -> dict:
    """今日成交概况 + 最近 5 条已平仓（只读 trades 表）。"""
    table = bot_models.get_table("trades")
    empty = {
        "trade_count": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "realized_profit": 0.0,
        "open_positions": 0,
        "last_close_at": None,
    }
    if table is None:
        return {"today": empty, "recent": []}

    session = db.bot_session()
    try:
        start, end = _today_window()
        cols = set(table.c.keys())
        closed = table.c.is_open.is_(False)
        stmt = select(func.count(table.c.id), func.coalesce(func.sum(table.c.realized_profit), 0))
        if "close_date" in cols:
            stmt = stmt.where(table.c.close_date >= start, table.c.close_date < end)
        count, profit = session.execute(stmt).one()
        wins = 0
        if count:
            win_stmt = select(func.count(table.c.id)).where(
                closed, table.c.realized_profit > 0
            )
            if "close_date" in cols:
                win_stmt = win_stmt.where(
                    table.c.close_date >= start, table.c.close_date < end
                )
            wins = to_int(session.execute(win_stmt).scalar())

        open_positions = to_int(
            session.execute(
                select(func.count(table.c.id)).where(table.c.is_open.is_(True))
            ).scalar()
        )

        last_close = None
        if "close_date" in cols:
            last_close = as_utc(
                session.execute(
                    select(func.max(table.c.close_date)).where(closed)
                ).scalar()
            )

        today = {
            "trade_count": to_int(count),
            "wins": to_int(wins),
            "losses": to_int(count) - to_int(wins),
            "win_rate": round2(safe_div(to_int(wins), to_int(count)) * 100, 2),
            "realized_profit": round2(to_float(profit)),
            "open_positions": open_positions,
            "last_close_at": last_close,
        }

        # 最近 5 条已平仓（停机时也展示历史，保证页面不空）
        recent: list[dict] = []
        sel_cols = [table.c.id, table.c.pair, table.c.close_date, table.c.exit_reason]
        if "is_short" in cols:
            sel_cols.append(table.c.is_short)
        for col in ("realized_profit", "close_profit_abs"):
            if col in cols:
                sel_cols.append(table.c[col])
        rows = session.execute(
            select(*sel_cols).where(closed).order_by(table.c.id.desc()).limit(5)
        ).mappings()
        for row in rows:
            is_short = bool(row.get("is_short"))
            pnl = row.get("realized_profit")
            if pnl is None:
                pnl = row.get("close_profit_abs")
            recent.append(
                {
                    "id": to_int(row.get("id")),
                    "pair": str(row.get("pair") or "—"),
                    "direction": "SHORT" if is_short else "LONG",
                    "pnl": round2(to_float(pnl)),
                    "close_time": as_utc(row.get("close_date")) if row.get("close_date") else None,
                    "exit_reason": str(row.get("exit_reason") or ""),
                }
            )
        return {"today": today, "recent": recent}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"机器人状态聚合失败: {exc}")
        return {"today": empty, "recent": []}
    finally:
        session.close()


def get_bot_status(max_lines: int = 200) -> dict:
    """状态页聚合入口：systemd + 日志尾部 + 今日概况。"""
    return {
        "service": probe_systemd(),
        "logs": read_log_tail(max_lines=max_lines),
        "bot_db_available": bot_models.is_available(),
        **today_and_recent(),
        "generated_at": as_utc(utc_now()),
    }
