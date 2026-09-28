"""
修复缺失的平仓数据 — 通过 OKX API 获取真实成交记录回填 trades 表。

使用方式:
  # 手动运行
  cd /root/bot1.1 && python3 -m repair_tools.repair_close_data

  # 或通过 cron（已在 crontab 中配置每天 00:00 运行）
"""

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger

from exchange_engine import exchange as ex

# ============================================================
# 配置
# ============================================================
DB_PATH = os.path.join(_PROJECT_ROOT, "user_data", "trading_bot.db")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 日志配置：同时输出到文件和控制台
log_file = os.path.join(LOG_DIR, "repair_{time:YYYY-MM-DD}.log")
logger.remove()  # 清除默认 handler
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")
logger.add(log_file, level="DEBUG", rotation="30 days", retention="90 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}")


def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_symbol(symbol: str, exchange_name: str = "okx") -> str:
    """将各种格式的 symbol 统一为 CCXT 可识别的格式。

    支持格式:
      BTC/USDT:USDT  → BTC/USDT:USDT (CCXT 标准)
      BTCUSDT        → BTC/USDT:USDT
      BTC-USDT-SWAP  → BTC/USDT:USDT (OKX 原始 id)
    """
    if "/" in symbol:
        return symbol

    norm = symbol.upper().replace("-SWAP", "").replace("-", "")
    markets = ex.get_markets(exchange_name)
    m = markets.get(norm)
    if m:
        return m["symbol"]
    return symbol


async def fetch_closed_trades_for_symbol(
    symbol: str,
    since: datetime,
    exchange_name: str = "okx",
) -> list[dict]:
    """
    通过 CCXT fetch_my_trades 获取某个币种从 since 至今的历史成交记录。
    """
    ccxt_ex = ex.get_exchange(exchange_name)
    markets = ex.get_markets(exchange_name)

    ccxt_symbol = _normalize_symbol(symbol, exchange_name)

    norm_base = symbol.upper().replace("/", "").replace(":USDT", "").replace("-SWAP", "").replace("-", "")
    m = markets.get(norm_base)
    market_id = m.get("id") if m else None

    since_ms = int(since.timestamp() * 1000)
    all_trades: list[dict] = []

    symbol_attempts = [ccxt_symbol]
    if market_id and market_id != ccxt_symbol:
        symbol_attempts.append(market_id)

    for sym in symbol_attempts:
        try:
            trades = await ccxt_ex.fetch_my_trades(sym, since=since_ms, limit=100)
            if trades:
                all_trades.extend(trades)
                logger.info(f"  {symbol}: fetch_my_trades({sym}) 获取到 {len(trades)} 条成交")
                break
        except Exception as e:
            logger.debug(f"  {symbol}: fetch_my_trades({sym}) 失败: {e}")
            continue

    if not all_trades:
        for sym in symbol_attempts:
            try:
                orders = await ccxt_ex.fetch_closed_orders(sym, since=since_ms, limit=100)
                if orders:
                    logger.info(f"  {symbol}: fetch_closed_orders({sym}) 获取到 {len(orders)} 条订单")
                    for o in orders:
                        if o.get("status") == "closed" and o.get("filled", 0) > 0:
                            avg_price = o.get("average") or o.get("price")
                            all_trades.append({
                                "symbol": o.get("symbol"),
                                "side": o.get("side"),
                                "price": avg_price,
                                "amount": o.get("filled"),
                                "cost": o.get("cost"),
                                "fee": o.get("fee"),
                                "datetime": o.get("datetime"),
                                "timestamp": o.get("timestamp"),
                                "order": o.get("id"),
                            })
                    break
            except Exception:
                continue

    return all_trades


async def repair_single_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    pair: str,
    direction: str,
    open_date: datetime,
    close_date: datetime | None = None,
    exchange_name: str = "okx",
) -> bool:
    """修复单个交易的平仓数据。"""
    close_side = "sell" if direction == "long" else "buy"

    logger.info(f"[{trade_id}] {pair} ({direction}) 开仓: {open_date}"
                + (f" 已有close_date: {close_date}" if close_date else ""))

    search_since = open_date - timedelta(hours=1)
    trades = await fetch_closed_trades_for_symbol(pair, search_since, exchange_name)

    if not trades:
        logger.warning(f"[{trade_id}] {pair}: 未获取到任何成交记录，跳过")
        return False

    close_trades = [t for t in trades if t.get("side", "").lower() == close_side]
    if not close_trades:
        logger.warning(f"[{trade_id}] {pair}: 未找到 {close_side} 方向的成交")
        return False

    open_ts = open_date.timestamp() * 1000
    # ⚠️ 跳过开仓瞬间(±1.5s)的成交 — 避免入场与相邻平仓同刻混入
    close_trades_after_open = [t for t in close_trades if t.get("timestamp", 0) >= open_ts + 1500]

    if not close_trades_after_open:
        logger.warning(f"[{trade_id}] {pair}: 开仓后未找到平仓成交")
        return False

    cursor = conn.cursor()
    cursor.execute(
        "SELECT open_rate, stake_amount, amount_requested, leverage FROM trades WHERE id = ?",
        (trade_id,),
    )
    row = cursor.fetchone()
    if not row:
        logger.warning(f"[{trade_id}] 数据库记录不存在")
        return False

    open_rate = row["open_rate"]
    stake_amount = row["stake_amount"]
    leverage = row["leverage"] or 1
    amount_requested = row["amount_requested"] or 0

    # ———— 聚合归属: 按开仓全仓量 amount_requested 精确归集平仓成交 ————
    # (此前用"离 close_date 最近的单笔成交"启发式: 分批止盈仓位只取到其中一笔,
    #  T 格式 close_date 还使窗口偏移 8h 混入相邻交易的成交 — 2026-09-04 事故)
    target_qty = float(amount_requested or 0)
    pool = []
    got = 0.0
    for t in close_trades_after_open:
        pool.append(t)
        got += float(t.get("amount") or 0)
        if target_qty > 0 and got >= target_qty - max(target_qty * 0.02, 1e-9):
            break
    if target_qty <= 0 or abs(got - target_qty) > max(target_qty * 0.05, 1.0):
        logger.warning(
            f"[{trade_id}] {pair}: 平仓成交数量 {got:.4g} 与开仓量 {target_qty:.4g} 不符，跳过"
            f" (可能成交记录不全或非本仓位)"
        )
        return False

    # 合约乘数 (OKX: amount 为张数, 盈亏需 × contractSize)
    ct_val = 1.0
    try:
        mkt = ex.get_exchange(exchange_name).market(_normalize_symbol(pair, exchange_name))
        ct_val = float(mkt.get("contractSize") or 1)
    except Exception:
        pass

    close_timestamp = 0
    pnl_net = 0.0
    fee_total = 0.0
    qty_w = 0.0
    price_w = 0.0
    for t in pool:
        px = float(t.get("price") or t.get("average") or 0)
        qty = float(t.get("amount") or 0)
        ts = int(t.get("timestamp") or 0)
        if px <= 0 or qty <= 0:
            continue
        if direction == "long":
            mv = (px - open_rate) * qty * ct_val
        else:
            mv = (open_rate - px) * qty * ct_val
        pnl_net += mv
        try:
            fee = t.get("fee") or {}
            fee_total += abs(float(fee.get("cost") or 0))
        except (ValueError, TypeError):
            pass
        close_timestamp = max(close_timestamp, ts)
        qty_w += qty
        price_w += px * qty

    close_price = price_w / qty_w if qty_w > 0 else 0
    pnl_net -= fee_total
    if not close_price:
        logger.warning(f"[{trade_id}] {pair}: 平仓成交无价格")
        return False

    notional = (stake_amount or 0) * leverage
    if open_rate and open_rate > 0 and notional > 0:
        # bot 语义: realized_profit/close_profit_abs = 绝对值(USDT), close_profit = 比率
        realized_profit_abs = pnl_net
        realized_profit_pct = pnl_net / notional
    else:
        realized_profit_abs = 0
        realized_profit_pct = 0

    # close_date 统一写 UTC 空格格式(与 bot 一致), 不再用本地时区 isoformat
    close_date_str = (
        datetime.utcfromtimestamp(close_timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S.%f")
        if close_timestamp else datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    )

    logger.info(
        f"[{trade_id}] {pair}: "
        f"close_price={close_price:.4f} "
        f"profit_pct={realized_profit_pct*100:.2f}% "
        f"profit_abs={realized_profit_abs:.4f}U"
    )

    cursor.execute(
        """
        UPDATE trades SET
            close_rate = ?,
            close_date = ?,
            realized_profit = ?,
            close_profit = ?,
            close_profit_abs = ?,
            is_open = 0,
            signal_meta = CASE
                WHEN signal_meta IS NULL THEN NULL
                ELSE REPLACE(signal_meta, '"_pnl_estimated": true', '"_pnl_estimated": false')
            END
        WHERE id = ?
          AND is_open = 0   -- 只允许修已平仓行, 防止把持仓中的仓位误标关闭 (2026-09-04 事故)
          AND (close_rate IS NULL OR close_rate = 0
               OR signal_meta LIKE '%"_pnl_estimated": true%')
        """,
        # bot 语义: realized_profit/close_profit_abs = USDT 绝对值; close_profit = 比率
        (close_price, close_date_str,
         realized_profit_abs, realized_profit_pct, realized_profit_abs,
         trade_id),
    )
    conn.commit()

    if cursor.rowcount > 0:
        logger.info(f"[{trade_id}] {pair}: ✅ 已更新")
        return True
    else:
        logger.info(f"[{trade_id}] {pair}: ⏭️ 无需更新")
        return False


async def repair_all():
    """修复过去 7 天内开仓、close_rate 缺失或带 _pnl_estimated 估算标记的交易
    (估算行是实时记账的 ticker 价估算, 此处用真实 fills 精化含手续费的盈亏)"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, pair, is_short, open_date, close_date
        FROM trades
        WHERE is_open = 0   -- 只处理已平仓行 (2026-09-04 曾误改持仓中仓位)
          AND (close_rate IS NULL OR signal_meta LIKE '%"_pnl_estimated": true%')
          AND NOT (open_rate IS NULL OR open_rate = 0)
          AND open_date >= datetime('now', '-7 days')
        ORDER BY open_date DESC
    """)
    rows = cursor.fetchall()
    total = len(rows)
    logger.info(f"需要修复的交易总数: {total} 笔")

    if total == 0:
        logger.info("没有需要修复的交易，数据库已完整。")
        conn.close()
        return 0, 0

    fixed = 0
    skipped = 0
    for idx, row in enumerate(rows, 1):
        trade_id = row["id"]
        pair = row["pair"]
        direction = "short" if row["is_short"] else "long"
        open_date = datetime.fromisoformat(row["open_date"]) if isinstance(row["open_date"], str) else row["open_date"]
        close_date = None
        if row["close_date"]:
            close_date = datetime.fromisoformat(row["close_date"]) if isinstance(row["close_date"], str) else row["close_date"]

        logger.info(f"\n[{idx}/{total}] ", end="")
        ok = await repair_single_trade(conn, trade_id, pair, direction, open_date, close_date)
        if ok:
            fixed += 1
        else:
            skipped += 1

        await asyncio.sleep(0.8)

    conn.close()
    logger.info(f"\n\n===== 修复完成: {fixed} 成功, {skipped} 跳过/失败 =====")
    return fixed, skipped


async def main():
    logger.info("=" * 60)
    logger.info(f"修复缺失的平仓数据 — OKX API  |  {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)

    # 加载 .env
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        from dotenv import load_dotenv
        load_dotenv(env_path)
        logger.info(f"已加载环境变量: {env_path}")
    else:
        logger.warning(f".env 文件不存在: {env_path}")

    api_key = os.getenv("OKX_API_KEY", "")
    api_secret = os.getenv("OKX_API_SECRET", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    is_testnet = os.getenv("OKX_MODE", "live") == "testnet"

    if not api_key or not api_secret:
        logger.error("请在 .env 中配置 OKX_API_KEY 和 OKX_API_SECRET")
        return

    logger.info(f"交易所模式: {'testnet' if is_testnet else 'live'}")
    await ex.init_exchange("okx", api_key, api_secret, passphrase, testnet=is_testnet)

    try:
        await repair_all()
    finally:
        await ex.close_all_exchanges()

    logger.info("✅ 修复任务完成")


if __name__ == "__main__":
    asyncio.run(main())
