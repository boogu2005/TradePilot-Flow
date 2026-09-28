"""老师月度仓位快照 — Dashboard 只读展示层（UI 增强）。

数据源：机器人库 trade 库的 teacher_month_ratio_snapshots 表（由机器人侧每月 1 号
拉取 90 天收益率排行榜生成并锁定）。本模块**只读**，仅把当月锁定档位附加到
排行榜响应，供前端展示「本月锁定档位 | 本月单笔仓位比例」两列。

口径约定（与机器人侧 core/position_tier.py 保持一致）：
- 快照排名 1–5 名 → 0.07；6–12 名 → 0.04；13–23 名 → 0.01
- 实时排名 ≠ 交易档位：UI 展示的仓位比例永远取当月快照，不是实时排行计算值
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import text

from ..database import db

_SNAPSHOT_TABLE = "teacher_month_ratio_snapshots"
_BAND_TEXT = {0.07: "1-5 名档", 0.04: "6-12 名档", 0.01: "13-23 名档"}
_AT_RE = re.compile(r"@([A-Za-z0-9_]+)")

_cache: dict = {"month": None, "map": {}}


def _normalize_teacher_key(name: str | None) -> str:
    """与机器人侧同名函数一致：'姓名(@user)'/'(@user)' → '@user' 小写。"""
    if not name:
        return ""
    m = _AT_RE.search(name)
    if m:
        return f"@{m.group(1)}".lower()
    return name.strip().lower()


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def get_snapshot_map(force: bool = False) -> dict:
    """返回 {teacher_key: {"rank": int, "ratio": float, "band": str}}（当月快照）。

    表不存在/当月无快照 → 空 dict（前端显示"—"），不影响排行榜本身。
    短缓存：与机器人月度锁定的语义天然一致，缓存到月变化即可。
    """
    month = _current_month()
    if not force and _cache["month"] == month:
        return _cache["map"]

    result: dict = {}
    session = db.bot_session()
    try:
        exists = session.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"
            ),
            {"t": _SNAPSHOT_TABLE},
        ).scalar()
        if exists:
            rows = session.execute(
                text(
                    f"SELECT teacher_key, snapshot_rank, position_ratio "
                    f"FROM {_SNAPSHOT_TABLE} WHERE snapshot_month=:m"
                ),
                {"m": month},
            ).mappings()
            for row in rows:
                ratio = float(row["position_ratio"])
                result[row["teacher_key"]] = {
                    "rank": int(row["snapshot_rank"]),
                    "ratio": ratio,
                    "band": _BAND_TEXT.get(ratio, ""),
                }
    except Exception:  # noqa: BLE001 —— 展示层失败不影响排行榜
        result = {}
    finally:
        session.close()
    _cache.update(month=month, map=result)
    return result


def attach_current_tier(items: list[dict]) -> None:
    """就地给排行榜 items 附加当月快照字段（未命中老师 → 各字段 None）。"""
    snapshot = get_snapshot_map()
    for item in items:
        hit = snapshot.get(_normalize_teacher_key(item.get("teacher")))
        if hit:
            item["snapshot_month"] = _current_month()
            item["tier_rank"] = hit["rank"]
            item["position_ratio"] = hit["ratio"]
            item["tier_band"] = hit["band"]
        else:
            item["snapshot_month"] = None
            item["tier_rank"] = None
            item["position_ratio"] = None
            item["tier_band"] = None
