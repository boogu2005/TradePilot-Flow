"""
修复 9/4 之前的可疑大额交易（stake>2.5 或 |realized_profit|>1.5）。

规则（用户确认）：
  - 用 OKX bills-archive（type=2 平仓流水）在 close_date ±30min 内找对应平仓流水；
  - 找到 → 修正 realized_profit / close_profit_abs 为流水 pnl 合计；
    若 DB 盈亏与流水呈 10/100/1000 倍单位错误，stake_amount 同步按比例缩放；
  - 找不到任何流水 → 删除该交易及关联 orders / close_history / executions 记录；
  - 流水归属不明确（同币种多笔重叠）→ 标记 ambiguous，不动，交给人工。

用法：
  venv/bin/python repair_tools/repair_big_trades.py          # 试运行，只打印决策
  venv/bin/python repair_tools/repair_big_trades.py --apply  # 应用修改
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT, ".env"), override=False)

DB_PATH = os.path.join(PROJECT, "user_data", "trading_bot.db")
KEY = os.getenv("OKX_API_KEY")
SECRET = os.getenv("OKX_API_SECRET")
PASS = os.getenv("OKX_PASSPHRASE")

# 盈亏单位错误缩放比例集合（1、0.1、0.01、0.001），容差 8%
SCALES = (1.0, 0.1, 0.01, 0.001)
TOL = 0.08


def okx_get(path: str, params: dict, retries: int = 4) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
    qs = urllib.parse.urlencode(params)
    msg = ts + "GET" + path + "?" + qs
    sign = base64.b64encode(
        hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    req = urllib.request.Request(
        "https://www.okx.com" + path + "?" + qs,
        headers={
            "OK-ACCESS-KEY": KEY,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": PASS,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            raise
    raise RuntimeError("429 重试耗尽")


def parse_close_utc(close_date: str) -> datetime:
    """DB close_date 均为 UTC（T 格式与空格格式都是；已与 OKX 流水时间戳逐秒核对）。"""
    s = close_date.replace("Z", "")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def inst_id_of(pair: str) -> str:
    base = pair.split(":")[0].replace("/", "-")
    return f"{base}-SWAP"


def fetch_close_bills(pair: str, close_utc: datetime, hours: float = 24.0) -> list[dict]:
    """close ±hours 内的平仓流水（type=2）。close_date 是对账发现时间，可能晚于实际平仓数小时。"""
    lo = close_utc - timedelta(hours=hours)
    hi = close_utc + timedelta(hours=hours)
    inst = inst_id_of(pair)
    r = okx_get(
        "/api/v5/account/bills-archive",
        {
            "instType": "SWAP",
            "type": "2",
            "begin": str(int(lo.timestamp() * 1000)),
            "end": str(int(hi.timestamp() * 1000)),
            "limit": "100",
        },
    )
    return [b for b in r.get("data", []) if b.get("instId") == inst]


def _clean_scale(f: float) -> float | None:
    """f 是否落在 1/0.1/0.01/0.001 的 ±8% 内；返回对应比例或 None。"""
    for s in SCALES:
        if abs(f / s - 1.0) <= TOL:
            return s
    return None


def decide(db_row: dict, bills: list[dict]) -> dict:
    """返回决策：{action: fix|delete|keep|ambiguous, ...}"""
    db_pnl = db_row["realized_profit"] or 0.0
    bill_pnl = round(sum(float(b.get("pnl") or 0.0) for b in bills), 6)
    nz = [float(b.get("pnl") or 0.0) for b in bills if abs(float(b.get("pnl") or 0.0)) > 1e-9]

    if not bills:
        # 完全没有流水 → 找不到正确数据 → 删除
        return {"action": "delete", "reason": "OKX 无平仓流水"}

    if db_pnl == 0:
        if abs(bill_pnl) < 0.05:
            return {"action": "keep", "reason": "DB盈亏≈0 与流水一致", "bill_pnl": bill_pnl}
        return {"action": "fix", "reason": "DB盈亏=0 但流水有盈亏", "bill_pnl": bill_pnl}

    # 单一非零流水 → 强证据：按它修正（能识别干净单位错误则同步缩放 stake）
    if len(nz) == 1:
        f = nz[0] / db_pnl
        s = _clean_scale(f)
        if s is not None:
            return {
                "action": "fix" if abs(nz[0] - db_pnl) > 0.005 else "keep",
                "reason": f"单一流水匹配(f={f:.4f})",
                "bill_pnl": round(nz[0], 6),
                "scale": s,
            }
        # 不构成整数单位错误，但窗口内仅此一笔平仓流水 → 仍以交易所值为准，只修盈亏不缩放 stake
        return {
            "action": "fix",
            "reason": f"单一流水，非整数比例(f={f:.4f})，以交易所值为准",
            "bill_pnl": round(nz[0], 6),
            "scale": None,
        }

    # 多条非零流水：
    # 1) 先找单条流水与 DB 盈亏的整数倍缩放匹配（部分平仓里某一条就是本单主平仓）
    for s in SCALES:
        target = db_pnl * s
        if abs(target) < 1e-9:
            continue
        for v in nz:
            if abs(v - target) <= TOL * abs(target):
                return {
                    "action": "fix",
                    "reason": f"多条流水中单条匹配(f={v / db_pnl:.4f})",
                    "bill_pnl": round(v, 6),
                    "scale": s,
                }
    # 2) 合计 vs 缩放后 DB 值
    for s in SCALES:
        target = db_pnl * s
        if abs(target) > 1e-9 and abs(bill_pnl - target) <= TOL * abs(target):
            return {
                "action": "fix" if abs(bill_pnl - db_pnl) > 0.005 else "keep",
                "reason": f"多条流水合计匹配(f={bill_pnl / db_pnl:.4f})",
                "bill_pnl": bill_pnl,
                "scale": s,
            }
    return {"action": "narrow"}


def main() -> None:
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        """SELECT * FROM trades WHERE is_open=0 AND open_date < '2026-09-04'
           AND (stake_amount > 2.5 OR ABS(realized_profit) > 1.5) ORDER BY id"""
    ).fetchall()

    # 人工复核覆盖：多笔同币种流水纠缠时，按时间邻近+流水批次归属的结论
    OVERRIDES = {
        248: {"action": "fix", "bill_pnl": 0.85716, "scale": None, "reason": "人工: 16:05+03:16 两笔分批平仓合计"},
        255: {"action": "fix", "bill_pnl": 0.5097, "scale": None, "reason": "人工: HBAR 三笔共享一笔 1.3096 流水，按比例分摊"},
        243: {"action": "fix", "bill_pnl": -0.528, "scale": None, "reason": "人工: 19:41Z 同批次多笔平仓合计"},
        314: {"action": "fix", "bill_pnl": 0.4761, "scale": None, "reason": "人工: 05:40Z 批次平仓合计"},
        240: {"action": "fix", "bill_pnl": -0.35586, "scale": None, "reason": "人工: 18:32Z 唯一负盈亏流水"},
        579: {"action": "delete", "reason": "人工: 窗口内无匹配流水"},
    }

    decisions = []
    for r in rows:
        try:
            close_utc = parse_close_utc(r["close_date"])
        except Exception:
            decisions.append({"id": r["id"], "action": "ambiguous", "reason": "close_date 解析失败"})
            continue
        try:
            bills = fetch_close_bills(r["pair"], close_utc)
        except Exception as e:
            decisions.append({"id": r["id"], "action": "ambiguous", "reason": f"OKX 查询失败: {e}"})
            time.sleep(1)
            continue
        d = decide(dict(r), bills)
        if r["id"] in OVERRIDES:
            d = dict(OVERRIDES[r["id"]])
        if d["action"] == "narrow":
            # 宽窗口归属不明 → 用 ±3h 窄窗口的流水合计作为交易所真值
            try:
                nb = fetch_close_bills(r["pair"], close_utc, hours=3.0)
            except Exception:
                nb = []
            nsum = round(sum(float(b.get("pnl") or 0.0) for b in nb), 6)
            nz_n = sum(1 for b in nb if abs(float(b.get("pnl") or 0.0)) > 1e-9)
            if nb and nz_n > 0:
                d = {
                    "action": "fix",
                    "reason": f"窄窗口(±3h)流水合计={nsum}（{len(nb)}条）",
                    "bill_pnl": nsum,
                    "scale": None,
                }
            else:
                d = {"action": "delete", "reason": "宽窗口流水归属不明且窄窗口无真实盈亏流水"}
        d["id"] = r["id"]
        d["pair"] = r["pair"]
        d["db_stake"] = round(r["stake_amount"] or 0, 4)
        d["db_pnl"] = round(r["realized_profit"] or 0, 4)
        d["n_bills"] = len(bills)
        decisions.append(d)
        time.sleep(0.3)

    # ---- 展示 + 应用 ----
    n_fix = n_del = n_keep = n_amb = 0
    for d in decisions:
        mark = {"fix": "修", "delete": "删", "keep": "留", "ambiguous": "?"}[d["action"]]
        print(
            f"[{mark}] id={d['id']:<4} {d['pair']:<16} stake={d.get('db_stake'):>10} "
            f"pnl={d.get('db_pnl'):>9} 流水数={d.get('n_bills', 0)}  → {d['reason']}"
        )
        if d["action"] == "fix":
            n_fix += 1
        elif d["action"] == "delete":
            n_del += 1
        elif d["action"] == "keep":
            n_keep += 1
        else:
            n_amb += 1

    print(f"\n合计: 修正 {n_fix} / 删除 {n_del} / 保留 {n_keep} / 存疑 {n_amb}")

    if not apply:
        print("（试运行，未修改数据库。加 --apply 应用。）")
        con.close()
        return

    for d in decisions:
        tid = d["id"]
        if d["action"] == "fix":
            bill_pnl = round(d["bill_pnl"], 6)
            if d.get("scale") is not None and abs(d["scale"] - 1.0) > 1e-9:
                new_stake = round((cur.execute("SELECT stake_amount FROM trades WHERE id=?", (tid,)).fetchone()[0] or 0) * d["scale"], 6)
                cur.execute(
                    "UPDATE trades SET realized_profit=?, close_profit_abs=?, stake_amount=? WHERE id=?",
                    (bill_pnl, bill_pnl, new_stake, tid),
                )
            else:
                cur.execute(
                    "UPDATE trades SET realized_profit=?, close_profit_abs=? WHERE id=?",
                    (bill_pnl, bill_pnl, tid),
                )
        elif d["action"] == "delete":
            cur.execute("DELETE FROM orders WHERE ft_trade_id=?", (tid,))
            cur.execute("DELETE FROM close_history WHERE trade_id=?", (tid,))
            cur.execute("DELETE FROM executions WHERE trade_id=?", (tid,))
            cur.execute("DELETE FROM trades WHERE id=?", (tid,))
    con.commit()

    total = cur.execute("SELECT COALESCE(SUM(realized_profit),0) FROM trades WHERE is_open=0").fetchone()[0]
    cnt = cur.execute("SELECT COUNT(*) FROM trades WHERE is_open=0").fetchone()[0]
    print(f"\n已应用。修复后已平仓 {cnt} 笔，总收益合计 = {round(total, 4)} USDT")
    con.close()


if __name__ == "__main__":
    main()
