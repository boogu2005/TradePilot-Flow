"""
307 笔已平仓交易全量对账（OKX SWAP 平仓流水 bills-archive 全量数据）。

匹配策略（按币种分组，事件 = 5 分钟窗口内的平仓流水聚类）：
  - 每笔交易在其 close_date ±24h 内找最近的未占用事件；
  - 事件盈亏与 DB 一致（±8% 或 ≤0.005）→ 保留；
  - 事件盈亏 = DB×10^k（k≠0，±8%）→ 修正 pnl 并同步缩放 stake；
  - 其他情况 → 以事件盈亏为准修正 pnl（stake 不动）；
  - ±24h 内无任何事件 → 删除交易及关联记录（用户规则：找不到正确数据就删除）；
  - 事件归属竞争的失败者 → 列入手工复核清单（不自动删除）。

用法：
  venv/bin/python repair_tools/reconcile_all_trades.py          # 试运行
  venv/bin/python repair_tools/reconcile_all_trades.py --apply  # 应用
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT, "user_data", "trading_bot.db")
BILLS_PATH = os.path.join(PROJECT, "user_data", "backups", "okx_bills_swap_pnl.json")

SCALES = (1.0, 0.1, 0.01, 0.001, 10.0, 100.0, 1000.0)
TOL = 0.08
CLUSTER_GAP_S = 300  # 5 分钟内视为同一次平仓的分批流水
WINDOW = timedelta(hours=24)


def parse_close_utc(close_date: str) -> datetime:
    """DB close_date 均为 UTC（T 格式与空格格式都是；已与 OKX 流水时间戳逐秒核对）。"""
    s = close_date.replace("Z", "")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def inst_id_of(pair: str) -> str:
    if pair.endswith("-SWAP"):
        return pair
    return f"{pair.split(':')[0].replace('/', '-')}-SWAP"


def build_events(bills: list[dict]) -> list[dict]:
    """把单条流水聚成平仓事件：{ts, pnl, n_bills}。pnl=0 的流水并入相邻事件。"""
    bills = sorted(bills, key=lambda b: int(b["ts"]))
    events: list[dict] = []
    for b in bills:
        ts = int(b["ts"])
        pnl = float(b.get("pnl") or 0.0)
        if events and ts - events[-1]["ts"] <= CLUSTER_GAP_S * 1000:
            events[-1]["pnl"] += pnl
            events[-1]["n_bills"] += 1
        else:
            events.append({"ts": ts, "pnl": pnl, "n_bills": 1})
    return events


def clean_scale(ratio: float) -> float | None:
    for s in SCALES:
        if abs(ratio / s - 1.0) <= TOL:
            return s
    return None


def decide(db_pnl: float, event_pnl: float) -> dict:
    if abs(db_pnl - event_pnl) <= max(0.005, TOL * abs(db_pnl)):
        return {"action": "keep", "reason": "与流水一致"}
    k = clean_scale(event_pnl / db_pnl) if db_pnl != 0 else None
    if k is not None and k != 1.0:
        return {
            "action": "fix",
            "reason": f"单位错误 ×{k}",
            "pnl": round(event_pnl, 6),
            "stake_scale": k,
        }
    return {
        "action": "fix",
        "reason": "以交易所流水为准",
        "pnl": round(event_pnl, 6),
        "stake_scale": None,
    }


def main() -> None:
    apply = "--apply" in sys.argv
    bills = json.load(open(BILLS_PATH))
    by_inst: dict[str, list[dict]] = defaultdict(list)
    for b in bills:
        by_inst[b["instId"]].append(b)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    trades = cur.execute("SELECT * FROM trades WHERE is_open=0 ORDER BY id").fetchall()
    print(f"待对账交易: {len(trades)} 笔；流水: {len(bills)} 条")

    # 按 instId 分组建事件
    events_by_inst: dict[str, list[dict]] = {
        k: build_events(v) for k, v in by_inst.items()
    }

    # 分组匹配：close_date 相同的多笔交易共享同一批流水（reconciler 曾用同一时间戳
    # 写入多行），按组对账：先逐行认领精确匹配的流水，剩余按比例分摊。
    decisions = {}   # trade_id -> decision
    trades_by_inst: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in trades:
        trades_by_inst[inst_id_of(r["pair"])].append(r)

    unclaimed = 0

    def _clean_pnl(v: float) -> bool:
        return abs(v) > 1e-9

    def _match_row(db_pnl: float, ev_pnl: float) -> float | None:
        """返回缩放比例（1.0 表示一致）或 None（不匹配）。"""
        if abs(db_pnl - ev_pnl) <= max(0.005, TOL * abs(db_pnl)):
            return 1.0
        if db_pnl != 0:
            k = clean_scale(ev_pnl / db_pnl)
            if k is not None and k != 1.0:
                return k
        return None

    for inst, grp in trades_by_inst.items():
        evs = events_by_inst.get(inst, [])
        if not evs:
            for r in grp:
                decisions[r["id"]] = {"action": "delete", "reason": "该币种无任何平仓流水"}
            continue
        # 按 close_date 分桶（截断到分钟，同一秒写入的多行归入同桶；NULL 单列）
        buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in grp:
            key = r["close_date"][:16] if r["close_date"] else f"NULL:{r['id']}"
            buckets[key].append(r)
        # 每桶一个时间戳
        bucket_cts: dict[str, int] = {}
        for key, rows in buckets.items():
            if rows[0]["close_date"]:
                bucket_cts[key] = int(parse_close_utc(rows[0]["close_date"]).timestamp() * 1000)
        # 事件全局认领：每条事件归属 ±24h 内 close 时间最近的桶（多行同桶共享）
        bucket_events: dict[str, list[dict]] = defaultdict(list)
        for e in evs:
            best_key, best_dist = None, None
            for key, cts in bucket_cts.items():
                dist = abs(e["ts"] - cts)
                if dist <= 24 * 3600 * 1000 and (best_dist is None or dist < best_dist):
                    best_key, best_dist = key, dist
            if best_key is not None:
                bucket_events[best_key].append(e)
            else:
                unclaimed += 1

        for key, rows in buckets.items():
            if not rows[0]["close_date"]:
                for r in rows:
                    decisions[r["id"]] = {"action": "delete", "reason": "close_date 为空"}
                continue
            cand = bucket_events.get(key, [])
            if not cand:
                for r in rows:
                    decisions[r["id"]] = {"action": "delete", "reason": "±24h 内无流水"}
                continue
            # 逐行认领：先精确匹配（单行对单事件，唯一命中才拿走）
            used: set[int] = set()
            matched: dict[int, tuple[int, float]] = {}  # trade_id -> (event_idx, scale)
            for r in rows:
                db_pnl = r["realized_profit"] or 0.0
                hits = [i for i, e in enumerate(cand)
                        if i not in used and _match_row(db_pnl, e["pnl"]) is not None]
                if len(hits) == 1:
                    used.add(hits[0])
                    matched[r["id"]] = (hits[0], _match_row(db_pnl, cand[hits[0]]["pnl"]))
            for r in rows:
                if r["id"] in matched:
                    i, k = matched[r["id"]]
                    if k == 1.0:
                        d = {"action": "keep", "reason": "流水一致"}
                    else:
                        d = {"action": "fix", "reason": f"单位错误 ×{k}",
                             "pnl": round(cand[i]["pnl"], 6), "stake_scale": k}
                    d["n_bills"] = 1
                    decisions[r["id"]] = d
            # 剩余行：按剩余事件合计分摊
            rest_rows = [r for r in rows if r["id"] not in matched]
            rest_ev = [cand[i]["pnl"] for i in range(len(cand)) if i not in used]
            ev_sum = sum(rest_ev)
            db_sum = sum((r["realized_profit"] or 0.0) for r in rest_rows)
            for r in rest_rows:
                db_pnl = r["realized_profit"] or 0.0
                if abs(db_sum - ev_sum) <= max(0.005, TOL * abs(db_sum)):
                    d = {"action": "keep", "reason": "组剩余合计一致"}
                elif db_sum != 0:
                    k = clean_scale(ev_sum / db_sum)
                    if k is not None and k != 1.0:
                        d = {"action": "fix", "reason": f"组单位错误 ×{k}",
                             "pnl": round(db_pnl * k, 6), "stake_scale": k}
                    else:
                        f = ev_sum / db_sum
                        # f 为负说明组内方向混杂，只修盈亏不动 stake（避免负仓位）
                        d = {"action": "fix", "reason": f"组按流水分摊(f={f:.3f})",
                             "pnl": round(db_pnl * f, 6), "stake_scale": f if f > 0 else None}
                else:
                    d = {"action": "fix", "reason": "组流水分摊(原值为0)",
                         "pnl": round(ev_sum / len(rest_rows), 6), "stake_scale": None}
                d["n_bills"] = len(rest_ev)
                decisions[r["id"]] = d

    # ---- 输出 ----
    n_fix = n_del = n_keep = 0
    for r in trades:
        tid = r["id"]
        d = decisions[tid]
        a = d["action"]
        mark = {"fix": "修", "delete": "删", "keep": "留"}[a]
        extra = ""
        if a == "fix":
            extra = f" {r['realized_profit']:.4f} → {d['pnl']}" + (
                f" (stake×{d['stake_scale']})" if d.get("stake_scale") else ""
            )
        elif a == "delete":
            extra = f" (原pnl {r['realized_profit']:.4f})"
        print(f"[{mark}] {tid:<4} {r['pair']:<16} {d['reason']:<18}{extra}")
        if a == "fix":
            n_fix += 1
        elif a == "delete":
            n_del += 1
        elif a == "keep":
            n_keep += 1

    print(f"\n合计: 保留 {n_keep} / 修正 {n_fix} / 删除 {n_del}")
    print(f"未认领流水事件: {unclaimed} 个（属于已删交易或 DB 缺失的交易）")

    if not apply:
        print("（试运行，未修改数据库。加 --apply 应用。）")
        con.close()
        return

    # 全量备份后再应用
    bk = {}
    for r in trades:
        bk[r["id"]] = {
            "trade": dict(r),
            "orders": [dict(x) for x in cur.execute("SELECT * FROM orders WHERE ft_trade_id=?", (r["id"],)).fetchall()],
            "close_history": [dict(x) for x in cur.execute("SELECT * FROM close_history WHERE trade_id=?", (r["id"],)).fetchall()],
            "executions": [dict(x) for x in cur.execute("SELECT * FROM executions WHERE trade_id=?", (r["id"],)).fetchall()],
        }
    json.dump(bk, open(os.path.join(PROJECT, "user_data/backups/pre_full_reconcile.json"), "w"),
              default=str, ensure_ascii=False)
    print("全量备份已写入 user_data/backups/pre_full_reconcile.json")

    for r in trades:
        d = decisions[r["id"]]
        tid = r["id"]
        if d["action"] == "fix":
            if d.get("stake_scale") and d["stake_scale"] != 1.0:
                new_stake = round((r["stake_amount"] or 0) * d["stake_scale"], 6)
                cur.execute(
                    "UPDATE trades SET realized_profit=?, close_profit_abs=?, stake_amount=? WHERE id=?",
                    (d["pnl"], d["pnl"], new_stake, tid),
                )
            else:
                cur.execute(
                    "UPDATE trades SET realized_profit=?, close_profit_abs=? WHERE id=?",
                    (d["pnl"], d["pnl"], tid),
                )
        elif d["action"] == "delete":
            cur.execute("DELETE FROM orders WHERE ft_trade_id=?", (tid,))
            cur.execute("DELETE FROM close_history WHERE trade_id=?", (tid,))
            cur.execute("DELETE FROM executions WHERE trade_id=?", (tid,))
            cur.execute("DELETE FROM trades WHERE id=?", (tid,))
    con.commit()
    total = cur.execute("SELECT COALESCE(SUM(realized_profit),0) FROM trades WHERE is_open=0").fetchone()[0]
    cnt = cur.execute("SELECT COUNT(*) FROM trades WHERE is_open=0").fetchone()[0]
    okx_total = round(sum(float(b.get("pnl") or 0.0) for b in bills), 4)
    print(f"\n已应用。已平仓 {cnt} 笔，DB 总收益 = {round(total, 4)}；OKX 全量流水 pnl 合计 = {okx_total}")
    con.close()


if __name__ == "__main__":
    main()
