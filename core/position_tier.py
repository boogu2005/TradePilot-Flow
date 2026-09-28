"""
老师月度分层仓位（Position Tier）— 单笔仓位比例的唯一决策入口。

机制（与 Dashboard 只做数据对接，不自行统计收益率、不自建排行榜）：
  1. 每月 1 号（本地时间 00:05）拉取 Dashboard 现成的 90 天收益率排行榜
     GET /api/teachers/ranking?period=90&type=roi （Basic 鉴权，凭证取 .env）；
  2. 按名次分档并写入快照表 teacher_month_ratio_snapshots（可追溯审计）：
       排名 1–5    → 0.07
       排名 6–12   → 0.04
       排名 13–23  → 0.01（23 名之外仍按榜外兜底 0.01，见 rank_to_ratio）
  3. 整个自然月内开仓都读取当月锁定比例，月内不随实时排名浮动；
  4. 容错降级（用户确认的策略）：
       - 当月快照缺失但有上月 → 沿用上月档位并告警；
       - 完全无任何历史快照 / 老师不在榜内 / 名称失配 / sender 为空
         → 一律最低档 0.01 并告警（绝不静默用旧 5%）。

本模块只被 main.py（开仓 Step C 取比例、启动加载、月度调度注册）调用，
不触碰交易引擎/风控/信号/下单链路。

CLI（人工补快照 / 强制重拉 / 查看当月档位）：
    cd /root/bot1.1 && venv/bin/python -m core.position_tier --ensure --show
    cd /root/bot1.1 && venv/bin/python -m core.position_tier --ensure --force --show
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, select

# 允许 .env 里的 DASHBOARD_* 在独立进程（CLI/测试）下也可用；bot 主进程已自行加载，重复加载无害
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if (_PROJECT_ROOT / ".env").is_file():
    try:
        from dotenv import load_dotenv

        load_dotenv(_PROJECT_ROOT / ".env", override=False)
    except Exception:  # noqa: BLE001
        pass

from database.models import TeacherMonthRatioSnapshot


def _session():
    """惰性初始化后取独立短 session（init_db 幂等；CLI/独立进程/主进程均可用）。"""
    from database.db import get_session, init_db

    init_db()
    return get_session()

# 档位映射；BAND_TEXT 由 TIER_BANDS 推导，调区间只需改这一处
TIER_BANDS: list[tuple[int, int, float]] = [
    (1, 5, 0.07),    # 快照排名 1–5 名
    (6, 12, 0.04),   # 快照排名 6–12 名
    (13, 23, 0.01),  # 快照排名 13–23 名
]
BAND_TEXT: dict[float, str] = {ratio: f"{lo}-{hi} 名档" for lo, hi, ratio in TIER_BANDS}
# 榜外 / 无任何快照时的保守兜底比例（用户确认）
DEFAULT_RATIO = 0.01

RANKING_API_PATH = "/api/teachers/ranking"
_AT_RE = re.compile(r"@([A-Za-z0-9_]+)")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# 内存缓存：来源月份 + 命中表；month 变化/拉取失败时失效重读
_CACHE: dict = {"month": None, "source_month": None, "items": {}, "fallback_warned": False}
_warned_keys: set[str] = set()


def _load_env_dashboard() -> tuple[str, str]:
    return os.getenv("DASHBOARD_USERNAME", ""), os.getenv("DASHBOARD_PASSWORD", "")


def current_month() -> str:
    """本地自然月 'YYYY-MM'（快照按月锁定的基准）。"""
    return datetime.now().strftime("%Y-%m")


def rank_to_ratio(rank: int) -> float | None:
    for lo, hi, ratio in TIER_BANDS:
        if lo <= rank <= hi:
            return ratio
    return None  # 榜外（>23 名）：不入快照，月内按榜外策略 0.01


def normalize_teacher_key(name: str | None) -> str:
    """老师标识归一化：'姓名(@user)'/'(@user)' → '@user'（小写）；取不到 @ 时整串小写。"""
    if not name:
        return ""
    m = _AT_RE.search(name)
    if m:
        return f"@{m.group(1)}".lower()
    return name.strip().lower()


def fetch_ranking(base_url: str | None = None) -> list[dict]:
    """调 Dashboard 现成 90 天动态仓位分配榜单（风险调整收益，不自行统计）。失败抛异常由调用方处理。"""
    import requests

    base = (base_url or os.getenv("DASHBOARD_BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
    user, pwd = _load_env_dashboard()
    resp = requests.get(
        f"{base}{RANKING_API_PATH}",
        params={"period": 90, "type": "risk_adjusted", "limit": 200},
        auth=(user, pwd),
        timeout=10,
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("items") or []
    rows = []
    for item in items:
        rank = item.get("rank")
        teacher = item.get("teacher")
        if isinstance(rank, int) and teacher:
            rows.append({"rank": rank, "teacher": str(teacher)})
    return rows


# ---------------- DB 读写（独立短 session） ----------------

def _read_month_rows(month: str) -> list[dict]:
    session = _session()
    try:
        stmt = select(TeacherMonthRatioSnapshot).where(
            TeacherMonthRatioSnapshot.snapshot_month == month
        )
        rows = [
            {
                "teacher_key": r.teacher_key,
                "teacher_name": r.teacher_name,
                "snapshot_rank": r.snapshot_rank,
                "position_ratio": r.position_ratio,
                "snapshot_timestamp": r.snapshot_timestamp,
            }
            for r in session.execute(stmt).scalars()
        ]
        return rows
    finally:
        session.close()


def upsert_month(month: str, ranking_items: list[dict]) -> dict:
    """按当月排行榜生成/覆盖当月快照（单事务先删后插，幂等）。返回分档统计。"""
    if not _MONTH_RE.match(month):
        raise ValueError(f"非法月份: {month}")
    prepared = []
    for item in ranking_items:
        rank = int(item.get("rank") or 0)
        if rank <= 0:
            continue
        ratio = rank_to_ratio(rank)
        if ratio is None:
            continue  # 榜外（>23 名）不入快照
        key = normalize_teacher_key(item["teacher"])
        if not key:
            continue
        prepared.append(
            {"rank": rank, "ratio": ratio, "teacher_key": key,
             "teacher_name": item["teacher"]}
        )
    session = _session()
    try:
        session.execute(
            delete(TeacherMonthRatioSnapshot).where(
                TeacherMonthRatioSnapshot.snapshot_month == month
            )
        )
        if prepared:
            session.add_all(
                [
                    TeacherMonthRatioSnapshot(
                        snapshot_month=month,
                        teacher_key=r["teacher_key"],
                        teacher_name=r["teacher_name"],
                        snapshot_rank=r["rank"],
                        position_ratio=r["ratio"],
                    )
                    for r in prepared
                ]
            )
        session.commit()
    finally:
        session.close()
    summary = {
        "month": month,
        "total": len(prepared),
        "7%": sum(1 for r in prepared if r["ratio"] == 0.07),
        "4%": sum(1 for r in prepared if r["ratio"] == 0.04),
        "1%": sum(1 for r in prepared if r["ratio"] == 0.01),
    }
    return summary


def ensure_sync(force: bool = False, base_url: str | None = None) -> dict:
    """当月快照缺失（或 force）→ 拉取 Dashboard 并入库。返回 {ok, summary|error}。"""
    month = current_month()
    if not force:
        try:
            if _read_month_rows(month):
                _invalidate_cache()
                return {"ok": True, "skipped": True, "month": month}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PositionTier] 当月快照存在性检查失败: {exc}")
    try:
        items = fetch_ranking(base_url=base_url)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"[PositionTier] 拉取 Dashboard 90 天收益率排行榜失败: {exc} "
            f"— 本月仓位将按兜底策略(沿用上月/最低档 0.01),可稍后手工补: "
            f"venv/bin/python -m core.position_tier --ensure"
        )
        _invalidate_cache()
        return {"ok": False, "error": str(exc), "month": month}
    try:
        summary = upsert_month(month, items)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[PositionTier] 快照入库失败: {exc}")
        _invalidate_cache()
        return {"ok": False, "error": str(exc), "month": month}
    _invalidate_cache()
    logger.info(
        f"[PositionTier] {month} 快照已生成: 共 {summary['total']} 位 "
        f"(7%档 {summary['7%']} / 4%档 {summary['4%']} / 1%档 {summary['1%']})"
    )
    return {"ok": True, "summary": summary, "month": month}


async def ensure_current_month(force: bool = False, base_url: str | None = None) -> bool:
    """异步版（main.py 启动钩子 / 月度调度使用），避免阻塞事件循环。"""
    result = await asyncio.to_thread(ensure_sync, force, base_url)
    return bool(result.get("ok"))


# ---------------- 月度调度（复用 cleanup_scheduler 模式） ----------------

async def run_monthly_snapshot_scheduler(shutdown_event: asyncio.Event) -> None:
    """
    每月 1 号 00:05（本地时间）执行一次排行榜快照；与实时榜解耦，整月锁定。
    照抄 core/cleanup_scheduler 的 wait_for 可中断模式。
    """
    logger.info("[PositionTier] 月度快照调度已启动（每月 1 号 00:05 本地时间拉取 90 天收益率榜）")
    while not shutdown_event.is_set():
        now = datetime.now()
        if now.month == 12:
            next_run = datetime(now.year + 1, 1, 1, 0, 5)
        else:
            next_run = datetime(now.year, now.month + 1, 1, 0, 5)
        wait_seconds = (next_run - now).total_seconds()
        logger.debug(f"[PositionTier] 下次快照: {next_run:%Y-%m-%d %H:%M} (等待 {wait_seconds / 86400:.1f} 天)")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=max(1.0, wait_seconds))
        except asyncio.TimeoutError:
            pass
        if shutdown_event.is_set():
            break
        await ensure_current_month(force=True)


# ---------------- 比例查询（开仓 Step C 唯一入口） ----------------

def _load_effective_map() -> tuple[str, dict[str, float]]:
    """返回 (来源月份, {teacher_key: ratio})。

    优先当月；当月缺失时沿用上月并告警一次；都无则空 map（调用方按 0.01 兜底）。
    """
    month = current_month()
    if _CACHE["month"] == month:
        return _CACHE["source_month"], _CACHE["items"]

    rows = _read_month_rows(month)
    source_month = month
    fallback = False
    if not rows:
        # 当月缺失 → 沿用上月（容错策略，日志告警留档）
        try:
            last = datetime.now().replace(day=1) - timedelta(days=1)
            last_month = last.strftime("%Y-%m")
            rows = _read_month_rows(last_month)
            if rows:
                source_month = last_month
                fallback = True
        except Exception:  # noqa: BLE001
            rows = []

    items = {r["teacher_key"]: r["position_ratio"] for r in rows}
    _CACHE.update(month=month, source_month=source_month, items=items)
    if fallback and not _CACHE["fallback_warned"]:
        _CACHE["fallback_warned"] = True
        logger.warning(
            f"[PositionTier] {month} 当月快照缺失，临时沿用 {source_month} 档位 "
            f"（请检查 Dashboard 或执行 venv/bin/python -m core.position_tier --ensure）"
        )
    return source_month, items


def _invalidate_cache() -> None:
    _CACHE.update(month=None, source_month=None, items={}, fallback_warned=False)
    _warned_keys.clear()


def ratio_for(teacher_name: str | None) -> float:
    """老师 → 本月锁定仓位比例。

    命中快照 → 锁定值；当月缺失沿用上月；榜外/失配/空 → DEFAULT_RATIO(0.01) + 告警。
    此函数在开仓 Step C 每信号调用一次（低频），告警对同一 key 只打一次。
    """
    key = normalize_teacher_key(teacher_name)
    source_month, items = _load_effective_map()
    if not items:
        logger.warning(
            f"[PositionTier] 无任何历史快照可用，teacher={teacher_name!r} 按兜底 {DEFAULT_RATIO:.0%} 开仓"
        )
        return DEFAULT_RATIO
    if not key:
        logger.warning(
            f"[PositionTier] 信号老师标识为空(unknown)，按兜底 {DEFAULT_RATIO:.0%} 开仓"
        )
        return DEFAULT_RATIO
    ratio = items.get(key)
    if ratio is None:
        if key not in _warned_keys:
            _warned_keys.add(key)
            logger.warning(
                f"[PositionTier] 老师 {teacher_name!r} 不在 {source_month} 快照内（新老师/改名/榜外），"
                f"按兜底 {DEFAULT_RATIO:.0%} 开仓，下月快照自动纳入"
            )
        return DEFAULT_RATIO
    return ratio


def snapshot_rows_for_ui(month: str | None = None) -> list[dict]:
    """供 Dashboard 只读展示当月档位表（UI 增强数据源，非交易路径）。"""
    target = month or current_month()
    return _read_month_rows(target)


# ---------------- CLI ----------------

def _print_show() -> None:
    month = current_month()
    source, items = _load_effective_map()
    rows = _read_month_rows(month)
    if not rows and source != month:
        rows = _read_month_rows(source)
    if not rows:
        print(f"{month}: 无任何快照（当月与上月均无）→ 全部老师按兜底 1%")
        return
    print(f"来源月份: {source}（{'当月' if source == month else '沿用上月'}） 共 {len(rows)} 位:")
    for r in sorted(rows, key=lambda x: x["snapshot_rank"]):
        print(
            f"  rank {r['snapshot_rank']:>2}  {r['teacher_name']:<26} "
            f"ratio {r['position_ratio']:.0%}  {BAND_TEXT.get(r['position_ratio'], '')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="老师月度分层仓位快照工具")
    parser.add_argument("--ensure", action="store_true", help="立即拉取 Dashboard 并生成当月快照")
    parser.add_argument("--force", action="store_true", help="与 --ensure 连用：当月快照已存在也强制重拉覆盖")
    parser.add_argument("--show", action="store_true", help="打印当前生效档位表")
    args = parser.parse_args()
    if args.ensure:
        result = ensure_sync(force=args.force)
        sys.exit(0 if result.get("ok") else 1)
    if args.show:
        _print_show()
        return
    parser.print_help()


if __name__ == "__main__":
    main()
