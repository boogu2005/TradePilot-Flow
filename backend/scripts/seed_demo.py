"""生成演示交易数据（独立 demo 库，不触碰机器人真实库）。

用途：在没有真实交易数据时预览 Dashboard 效果。

用法：
    .venv\\Scripts\\python.exe backend/scripts/seed_demo.py
    然后启动后端时指定 DATABASE_URL=sqlite:///user_data/demo_trading.db
"""
from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEMO_DB = PROJECT_ROOT / "user_data" / "demo_trading.db"


TEACHERS = [
    ("比特币教父", "BTC-USDT-SWAP", 0.62, 45.0, 18.0, 0.03),
    ("趋势猎手", "ETH-USDT-SWAP", 0.71, 32.0, 22.0, 0.02),
    ("合约女王", "SOL-USDT-SWAP", 0.55, 60.0, 28.0, 0.04),
    ("短线之王", "DOGE-USDT-SWAP", 0.58, 25.0, 15.0, 0.015),
    ("均线大师", "XRP-USDT-SWAP", 0.66, 20.0, 12.0, 0.02),
    ("闪电侠", "BNB-USDT-SWAP", 0.78, 15.0, 10.0, 0.01),
    ("狙击手", "AVAX-USDT-SWAP", 0.50, 55.0, 30.0, 0.035),
    ("稳健王", "LINK-USDT-SWAP", 0.83, 12.0, 9.0, 0.008),
]


def main() -> None:
    random.seed(42)
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    conn = sqlite3.connect(DEMO_DB)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange VARCHAR(25) NOT NULL,
            pair VARCHAR(25) NOT NULL,
            is_open BOOLEAN NOT NULL,
            is_short BOOLEAN NOT NULL,
            open_rate FLOAT NOT NULL,
            close_rate FLOAT,
            realized_profit FLOAT NOT NULL,
            close_profit_abs FLOAT,
            stake_amount FLOAT NOT NULL,
            amount FLOAT NOT NULL,
            margin FLOAT,
            quantity FLOAT,
            leverage FLOAT NOT NULL,
            stop_loss FLOAT NOT NULL,
            open_date DATETIME NOT NULL,
            close_date DATETIME,
            exit_reason VARCHAR(255),
            strategy VARCHAR(100),
            tp1_price FLOAT,
            tp1_filled_at DATETIME,
            position_state VARCHAR(25) NOT NULL,
            teacher VARCHAR(255)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time DATETIME NOT NULL,
            balance FLOAT NOT NULL,
            equity FLOAT NOT NULL,
            available FLOAT NOT NULL,
            margin FLOAT NOT NULL,
            unrealized_pnl FLOAT NOT NULL,
            realized_pnl FLOAT NOT NULL,
            open_positions INTEGER NOT NULL,
            details TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time DATETIME NOT NULL,
            level VARCHAR(16) NOT NULL,
            module VARCHAR(64) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            message TEXT NOT NULL,
            trade_id INTEGER,
            symbol VARCHAR(32),
            details TEXT
        )
        """
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    trade_id = 0
    base_price = {
        "BTC-USDT-SWAP": 62000.0,
        "ETH-USDT-SWAP": 3300.0,
        "SOL-USDT-SWAP": 165.0,
        "DOGE-USDT-SWAP": 0.13,
        "XRP-USDT-SWAP": 0.55,
        "BNB-USDT-SWAP": 590.0,
        "AVAX-USDT-SWAP": 27.0,
        "LINK-USDT-SWAP": 13.5,
    }

    for teacher, pair, win_rate, avg_win, avg_loss, pct in TEACHERS:
        for i in range(random.randint(25, 45)):
            is_win = random.random() < win_rate
            is_short = random.random() < 0.5
            open_price = base_price[pair] * random.uniform(0.92, 1.08)
            pnl = abs(random.gauss(avg_win, avg_win * 0.5)) if is_win else -abs(random.gauss(avg_loss, avg_loss * 0.5))
            stake = random.uniform(500, 2500)
            amount = stake / open_price
            days_ago = random.uniform(0, 60)
            open_dt = now - timedelta(days=days_ago, hours=random.uniform(0, 20))
            hold_hours = random.uniform(1, 72)
            close_dt = open_dt + timedelta(hours=hold_hours)
            close_price = open_price * (1 + (pnl / stake)) * (1 if not is_short else -1) * -1
            # 简化：用相对价格变动近似
            if is_short:
                close_price = open_price - (pnl / amount)
            else:
                close_price = open_price + (pnl / amount)
            trade_id += 1
            cur.execute(
                "INSERT INTO trades (exchange,pair,is_open,is_short,open_rate,close_rate,realized_profit,close_profit_abs,stake_amount,amount,margin,quantity,leverage,stop_loss,open_date,close_date,exit_reason,strategy,tp1_price,tp1_filled_at,position_state,teacher) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "okx", pair, 0, is_short, open_price, close_price, pnl, pnl,
                    stake, amount, stake / 10, amount, 10,
                    open_price * (0.95 if not is_short else 1.05),
                    open_dt, close_dt,
                    "roi" if is_win else "stop_loss", "follow",
                    open_price * (1.02 if not is_short else 0.98),
                    open_dt + timedelta(hours=hold_hours / 2) if is_win else None,
                    "closed", teacher,
                ),
            )

    # 当前持仓 3 个
    for i, (teacher, pair, *_rest) in enumerate(TEACHERS[:3]):
        open_price = base_price[pair] * random.uniform(0.95, 1.05)
        stake = random.uniform(800, 2000)
        amount = stake / open_price
        open_dt = now - timedelta(hours=random.uniform(2, 36))
        trade_id += 1
        cur.execute(
            "INSERT INTO trades (exchange,pair,is_open,is_short,open_rate,close_rate,realized_profit,close_profit_abs,stake_amount,amount,margin,quantity,leverage,stop_loss,open_date,close_date,exit_reason,strategy,tp1_price,tp1_filled_at,position_state,teacher) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "okx", pair, 1, i % 2 == 0, open_price, None, 0, None,
                stake, amount, stake / 10, amount, 10,
                open_price * (0.95 if i % 2 == 0 else 1.05),
                open_dt, None, None, "follow",
                open_price * (1.02 if i % 2 == 0 else 0.98), None,
                "open", teacher,
            ),
        )

    # 账户快照：过去 60 天，每 4 小时一条
    equity = 10000.0
    snap_time = now - timedelta(days=60)
    while snap_time <= now:
        equity += random.gauss(8, 25)
        equity = max(equity, 8000)
        cur.execute(
            "INSERT INTO account_snapshots (time,balance,equity,available,margin,unrealized_pnl,realized_pnl,open_positions,details) VALUES (?,?,?,?,?,?,?,?,?)",
            (snap_time, equity - 200, equity, equity - 600, 400, random.uniform(-80, 120), random.uniform(-40, 60), random.randint(1, 4), None),
        )
        snap_time += timedelta(hours=4)

    # 系统事件：证明在线
    cur.execute(
        "INSERT INTO system_events (time,level,module,event_type,message) VALUES (?,?,?,?,?)",
        (now - timedelta(seconds=30), "info", "dashboard", "heartbeat", "bot running"),
    )
    conn.commit()
    conn.close()
    print(f"演示数据已生成: {DEMO_DB}")
    print(f"  交易数: {trade_id} | 老师数: {len(TEACHERS)} | 账户快照: 360 条")


if __name__ == "__main__":
    main()
