"""
对账收尾：
  1. 把"保留"行的盈亏改为其认领流水的精确合计（消除 ±8% 容差漂移）；
  2. 为未认领的 OKX 平仓流水（DB 从未记录的仓位）补建交易行，exit_reason='okx_unmatched'；
  3. 最终效果：DB 总收益 == OKX 全量平仓流水 pnl 合计（逐笔对齐 + 总账一致）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reconcile_all_trades import (  # noqa: E402
    TOL, build_events, clean_scale, inst_id_of, parse_close_utc,
)

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT, "user_data", "trading_bot.db")
BILLS_PATH = os.path.join(PROJECT, "user_data", "backups", "okx_bills_swap_pnl.json")


def _match_row(db_pnl: float, ev_pnl: float) -> float | None:
    if abs(db_pnl - ev_pnl) <= max(0.005, TOL * abs(db_pnl)):
        return 1.0
    if db_pnl != 0:
        k = clean_scale(ev_pnl / db_pnl)
        if k is not None and k != 1.0:
            return k
    return None


def main() -> None:
    bills = json.load(open(BILLS_PATH))
    okx_total = sum(float(b.get("pnl") or 0.0) for b in bills)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    trades = cur.execute("SELECT * FROM trades WHERE is_open=0 ORDER BY id").fetchall()

    by_inst_t: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in trades:
        by_inst_t[inst_id_of(r["pair"])].append(r)
    by_inst_b = defaultdict(list)
    for b in bills:
        by_inst_b[b["instId"]].append(b)
    events_by_inst = {k: build_events(v) for k, v in by_inst_b.items()}

    # ---- 与 reconcile_all_trades 一致的认领 ----
    buckets: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for inst, grp in by_inst_t.items():
        bk: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in grp:
            bk[r["close_date"][:16] if r["close_date"] else f"NULL:{r['id']}"].append(r)
        buckets[inst] = bk
    bucket_events: dict[tuple[str, str], list[dict]] = defaultdict(list)
    orphans: list[dict] = []
    for inst, evs in events_by_inst.items():
        bk = buckets.get(inst, {})
        cts_map = {k: int(parse_close_utc(rows[0]["close_date"]).timestamp() * 1000)
                   for k, rows in bk.items() if rows[0]["close_date"]}
        for e in evs:
            best, bestd = None, None
            for k, c in cts_map.items():
                d = abs(e["ts"] - c)
                if d <= 24 * 3600 * 1000 and (bestd is None or d < bestd):
                    best, bestd = k, d
            if best is not None:
                bucket_events[(inst, best)].append(e)
            else:
                e2 = dict(e)
                e2["instId"] = inst
                orphans.append(e2)

    updates: list[tuple[int, float]] = []  # (trade_id, new_pnl)
    for inst, bk in buckets.items():
        for key, rows in bk.items():
            cand = bucket_events.get((inst, key), [])
            if not cand:
                continue  # 已删除或应删除（前一步已处理）
            used: set[int] = set()
            matched: dict[int, int] = {}
            for r in rows:
                db_pnl = r["realized_profit"] or 0.0
                hits = [i for i, e in enumerate(cand)
                        if i not in used and _match_row(db_pnl, e["pnl"]) is not None]
                if len(hits) == 1:
                    used.add(hits[0])
                    matched[r["id"]] = hits[0]
            rest = [r for r in rows if r["id"] not in matched]
            rest_ev = [cand[i]["pnl"] for i in range(len(cand)) if i not in used]
            ev_sum = sum(rest_ev)
            db_sum = sum((r["realized_profit"] or 0.0) for r in rest)
            for r in rows:
                if r["id"] in matched:
                    i = matched[r["id"]]
                    updates.append((r["id"], round(cand[i]["pnl"], 6)))
                elif rest:
                    if db_sum == 0:
                        updates.append((r["id"], round(ev_sum / len(rest), 6)))
                    else:
                        f = ev_sum / db_sum
                        updates.append((r["id"], round((r["realized_profit"] or 0.0) * f, 6)))
            # 行全部被认领但仍有剩余事件 → 按 |pnl| 权重摊给已匹配的行（保证桶合计=流水合计）
            if not rest and rest_ev and rows:
                total_w = sum(abs(cand[matched[r["id"]]]["pnl"]) for r in rows)
                if total_w < 1e-9:
                    # 匹配到的全是 0 盈亏事件 → 均摊
                    for r in rows:
                        updates.append((r["id"], round(ev_sum / len(rows), 6)))
                else:
                    for r in rows:
                        w = abs(cand[matched[r["id"]]]["pnl"]) / total_w
                        new_pnl = round(cand[matched[r["id"]]]["pnl"] + ev_sum * w, 6)
                        updates.append((r["id"], new_pnl))

    # ---- 应用更新 ----
    for tid, pnl in updates:
        cur.execute("UPDATE trades SET realized_profit=?, close_profit_abs=? WHERE id=?",
                    (pnl, pnl, tid))

    # ---- 补建未认领流水的交易行 ----
    def inst_to_pair(inst: str) -> str:
        return f"{inst[:-5].replace('-', '/')}/USDT:USDT"

    n_orphan = 0
    for e in orphans:
        ts = datetime.fromtimestamp(e["ts"] / 1000, tz=timezone.utc)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")
        cur.execute(
            """INSERT INTO trades
               (exchange, pair, is_short, is_open, open_rate, close_rate, stake_amount,
                amount, open_date, close_date, realized_profit, close_profit_abs,
                close_profit, exit_reason, close_reason, exit_order_status,
                position_state, leverage, teacher, fee_open, fee_close,
                stop_loss, is_stop_loss_trailing, trading_mode, exit_mode,
                trailing_activated, protection_state, repair_retry, repair_lock)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("okx", inst_to_pair(e["instId"]), 0, 0, 0.0, 0.0, 0.0, 0.0,
             ts_str, ts_str, round(e["pnl"], 6), round(e["pnl"], 6),
             0.0, "okx_unmatched", "okx_unmatched", "closed", "closed", 0, None,
             0.0, 0.0, 0.0, 0, "", "", 0, "", 0, 0),
        )
        n_orphan += 1
    con.commit()

    total = cur.execute("SELECT COALESCE(SUM(realized_profit),0) FROM trades WHERE is_open=0").fetchone()[0]
    cnt = cur.execute("SELECT COUNT(*) FROM trades WHERE is_open=0").fetchone()[0]
    print(f"行级更新: {len(updates)} 行；补建未认领交易: {n_orphan} 行")
    print(f"OKX 全量平仓流水 pnl 合计: {round(okx_total, 6)}")
    print(f"DB 总收益(已平仓 {cnt} 笔):  {round(total, 6)}")
    print(f"差异: {round(total - okx_total, 6)}")
    con.close()


if __name__ == "__main__":
    main()
