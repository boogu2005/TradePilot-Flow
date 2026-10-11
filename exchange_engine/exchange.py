"""
CCXT 交易所封装 — 仅 OKX 合约。
提供：行情、余额、下单（市价/限价/止损/止盈）、撤单、杠杆设置。

======================= 速率限制优化 =======================
1. 单一 Exchange 实例 — 模块级创建一次，禁止多实例
2. load_markets 仅调用一次（_markets_loaded 守卫）
3. 统一 API 缓存层 (api_cache.py)：Ticker=1s / Position=2s / Balance=5s / OpenOrders=2s
4. asyncio.Lock 防并发重复请求（多个协程同时 fetch 只实际请求一次）
5. HTTP 429 自动指数退避：2s → 4s → 8s → 16s
6. [API] 日志输出 cache_hit / cache_miss / 耗时
============================================================
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import ccxt.async_support as ccxt
from loguru import logger

# ———— 缓存层 ————
from .api_cache import (
    ticker_cache, position_cache, balance_cache, open_orders_cache,
    count_api_call, get_api_stats, reset_api_stats,
)

# ———— 多交易所注册表 ————
_exchanges: dict[str, ccxt.Exchange] = {}
_markets_cache: dict[str, dict[str, dict]] = {}   # name → {SYMBOL: market_dict}
_active_exchanges: list[str] = []
# 重新连接用的凭据缓存
_exchange_configs: dict[str, dict] = {}
# load_markets 守卫：确保只调用一次
_markets_loaded: bool = False
_markets_loaded_lock = asyncio.Lock()
# 重连日志节流：每 12 小时最多打印一次
_last_reconnect_log_time: float = 0.0

# ———— Position Snapshot ————
# 统一由 PositionManager 管理（WS实时 + REST恢复 + Watchdog）
# 所有模块通过以下函数读取缓存，禁止直接 fetch_positions()


# ———— 交易所构建 ————
import os as _os

def _get_proxy() -> str | None:
    p = _os.getenv("EXCHANGE_PROXY", "").strip()
    return p if p else None

def _build_okx(api_key: str, api_secret: str, passphrase: str, testnet: bool = False) -> ccxt.okx:
    """构建 OKX 交易所实例。testnet=True → 模拟盘。默认实盘。"""
    params: dict = {
        "apiKey": api_key,
        "secret": api_secret,
        "password": passphrase,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    }
    if testnet:
        params["sandbox"] = True
    proxy = _get_proxy()
    if proxy:
        params["proxies"] = {"http": proxy, "https": proxy}
    return ccxt.okx(params)


# ———— 凭据验证 ————

def validate_credentials(name: str, api_key: str, api_secret: str, passphrase: str = ""):
    """验证交易所 API 凭据格式。"""
    issues = []
    secret = api_secret.strip()
    phrase = passphrase.strip()
    if len(secret) != len(api_secret) or (passphrase and len(phrase) != len(passphrase)):
        issues.append("Secret 或 Passphrase 前后有空格")
    if len(api_key) < 10:
        issues.append("API Key 格式异常（长度不足）")
    if passphrase and len(passphrase) < 4:
        issues.append("Passphrase 格式异常（长度不足）")
    if issues:
        logger.warning(f"[{name}] 凭据格式存在异常，请检查 .env 配置")
    else:
        logger.info(f"[{name}] 凭据格式检查通过")


# ———— 初始化 & 关闭 ————

async def init_exchange(name: str, api_key: str, api_secret: str,
                        passphrase: str = "", testnet: bool = False):
    """
    初始化单个交易所连接。
    load_markets 仅执行一次（_markets_loaded 守卫）。

    资源安全：
    - 先创建 → 再 load_markets → 成功后才注册到 _exchanges
    - load_markets 失败 → 立即 close() + 不注册
    - 确保每个创建的 ccxt 实例必然被 close()
    """
    global _exchanges, _markets_cache, _active_exchanges, _markets_loaded

    if not api_key or not api_secret:
        logger.warning(f"[{name}] API Key 未配置，跳过")
        return

    validate_credentials(name, api_key, api_secret, passphrase)

    if name != "okx":
        logger.warning(f"[{name}] 仅支持 OKX，忽略 {name}")
        return

    # ---- 单一 Exchange 实例守卫 ----
    if name in _exchanges:
        logger.info(f"[{name}] Exchange 实例已存在，跳过重复初始化")
        return

    # 1. 先创建实例
    ex = _build_okx(api_key, api_secret, passphrase, testnet)
    ex.options["hedgeMode"] = True
    ex.timeout = 30000

    _exchange_configs[name] = {
        "api_key": api_key, "api_secret": api_secret,
        "passphrase": passphrase, "testnet": testnet,
    }

    proxy = _get_proxy()
    if proxy:
        ex.trust_env = True
        logger.info(f"[{name}] 代理已配置 (HTTPS_PROXY={proxy})")

    # 2. try/finally 确保所有路径都释放资源（包括 CancelledError）
    registered = False
    try:
        async with _markets_loaded_lock:
            if not _markets_loaded:
                await ex.load_markets()
                _markets_loaded = True
                logger.info(f"[{name}] load_markets 完成 ({len(ex.markets)} 合约)")
            else:
                logger.info(f"[{name}] markets 已加载，跳过重复 load_markets")

        # 3. 注册到全局字典
        _exchanges[name] = ex
        registered = True

        # 缓存所有 USDT 永续合约
        if name not in _markets_cache or not _markets_cache[name]:
            cache: dict[str, dict] = {}
            for sym, m in ex.markets.items():
                if (m.get("swap") and m.get("linear")
                        and m.get("quote") == "USDT" and m.get("active")):
                    base = m["base"]
                    short = f"{base}USDT"
                    cache[short] = m
            _markets_cache[name] = cache
            _active_exchanges.append(name)

        logger.success(
            f"[{name}] 已连接 (testnet={testnet}), "
            f"USDT 永续合约 {len(_markets_cache.get(name, {}))} 个"
        )

    except asyncio.CancelledError:
        await _safe_close_exchange(ex, name)
        raise
    except Exception:
        await _safe_close_exchange(ex, name)
        raise


async def _safe_close_exchange(ex: ccxt.Exchange, label: str = "okx") -> None:
    """
    安全关闭单个交易所实例。
    确保 aiohttp ClientSession + connector 完全释放。

    1. 调用 ex.close()（关闭 CCXT 内建的 session）
    2. 额外清理任何残留 session（防御性代码，某些 CCXT 版本有 bug）
    3. 等待一小段时间让 aiohttp 完成清理
    4. 由 __del__ 触发的 "requires to release all resources" 警告不会再有
    """
    # 1. 调用 CCXT 的 close()
    try:
        await ex.close()
    except Exception as e:
        logger.debug(f"[{label}] exchange.close() 异常: {e}")

    # 2. 防御性清理：显式关闭内部 session
    try:
        if hasattr(ex, "_session") and ex._session is not None:
            if not ex._session.closed:
                await ex._session.close()
    except Exception:
        pass

    # 3. 等待让 aiohttp 完成清理（避免 __del__ 警告）
    await asyncio.sleep(0.05)

    # 让 pending 的 callback 有机会执行
    await asyncio.sleep(0.05)


async def close_all_exchanges():
    """
    关闭所有交易所连接。
    使用 _safe_close_exchange 确保 aiohttp 资源完全释放。
    """
    global _exchanges, _active_exchanges, _markets_loaded
    names = list(_exchanges.keys())
    for name in names:
        ex = _exchanges.pop(name, None)
        if ex is not None:
            await _safe_close_exchange(ex, name)
    _active_exchanges.clear()
    _markets_loaded = False
    # 给系统一点时间处理 pending 的 callbacks（避免 "Unclosed connector"）
    await asyncio.sleep(0.1)


async def try_reuse_exchange(name: str) -> bool:
    """
    尝试复用现有 Exchange 实例 — 不销毁、不重建。

    分级恢复第一级：
      1. 关闭旧的 aiohttp session（释放底层连接）
      2. 创建新的 aiohttp session（CCXT 自动懒创建）
      3. 用 fetch_time() 验证连通性

    优点：
      - 不触发 validate_credentials 日志
      - 不重新 load_markets（省掉 /asset/currencies 请求）
      - 不创建新的 Exchange 实例
      - 恢复速度快（< 2s vs 重建 10-30s）

    返回 True 表示复用成功，False 表示需要走 reconnect_exchange 重建。
    """
    old = _exchanges.get(name)
    if old is None:
        return False

    try:
        # 1. 关闭旧 session（释放底层 TCP 连接）
        await _safe_close_exchange(old, name)

        # 2. CCXT 会在下次 API 调用时自动创建新 session
        #    用 fetch_time() 做最轻量的连通性验证
        await asyncio.wait_for(old.fetch_time(), timeout=10.0)

        logger.info(f"[{name}] 实例复用成功（session 已重建）")
        return True

    except Exception as e:
        logger.debug(f"[{name}] 实例复用失败: {type(e).__name__}: {e}")
        return False


async def reconnect_exchange(name: str) -> bool:
    """
    重建交易所实例 — 分级恢复第二级（仅在 try_reuse_exchange 失败时调用）。

    流程：
      1. close() 旧实例（释放所有资源）
      2. 创建新实例
      3. load_markets（仅首次）
      4. 验证连通性

    资源安全：
    - 先 close 旧实例，再创建新实例
    - 如果创建失败，确保新实例被 close()
    - 使用 try/finally 确保所有路径都释放资源
    """
    global _exchanges, _markets_cache, _active_exchanges, _markets_loaded, _last_reconnect_log_time
    cfg = _exchange_configs.get(name)
    if not cfg:
        logger.warning(f"[{name}] 重连失败：无凭据缓存")
        return False

    # 1. 先 close 旧实例
    old = _exchanges.pop(name, None)
    if old:
        await _safe_close_exchange(old, name)

    _markets_cache.pop(name, None)
    _markets_loaded = False  # 重连强制重新 load_markets
    if name in _active_exchanges:
        _active_exchanges.remove(name)

    # 2. 创建新实例（失败时确保释放资源）
    try:
        await init_exchange(name, cfg["api_key"], cfg["api_secret"],
                            cfg["passphrase"], cfg["testnet"])
        logger.success(f"[{name}] 实例重建成功")
        _last_reconnect_log_time = time.time()
        return True
    except Exception as e:
        # 日志节流：每 12 小时最多打印一次
        now = time.time()
        if now - _last_reconnect_log_time >= 12 * 3600:
            logger.warning(f"[{name}] 实例重建失败: {type(e).__name__}: {e}")
            _last_reconnect_log_time = now

        # 3. 失败时确保新创建的实例被 close
        new_ex = _exchanges.pop(name, None)
        if new_ex:
            try:
                await _safe_close_exchange(new_ex, name)
            except Exception:
                pass

        return False


# ———— 路由 ————

def get_exchange(name: str = "okx") -> ccxt.Exchange:
    if name not in _exchanges:
        raise RuntimeError(f"交易所 [{name}] 未初始化")
    return _exchanges[name]


def get_markets(name: str = "okx") -> dict[str, dict]:
    return _markets_cache.get(name, {})


def get_active_exchanges() -> list[str]:
    return list(_active_exchanges)


def get_exchange_config(name: str = "okx") -> dict | None:
    """获取交易所配置（用于 PositionManager 初始化等）。"""
    return _exchange_configs.get(name)


# ———— 币种校验 ————

def validate_symbol(symbol: str, exchange: str = "okx") -> tuple[bool, str | None]:
    """验证 symbol 是否是有效的 USDT 永续合约。"""
    if not symbol or exchange not in _exchanges:
        return False, None
    s = str(symbol).upper().strip().replace(" ", "")
    markets = _markets_cache.get(exchange, {})
    if s in markets:
        return True, markets[s]["symbol"]
    if s.endswith("USDT") and len(s) >= 7:
        # BugFix: 处理 ccxt 格式 PI/USDT:USDT → PIUSDT
        # cache key 存储的是短格式 (PIUSDT)，不是 ccxt 标准格式 (PI/USDT:USDT)
        # 当 symbol 已被转换为 ccxt 格式后再次进入此函数时会匹配到这里
        short = s.replace("/", "").replace(":USDT", "")
        if short != s and short in markets:
            return True, markets[short]["symbol"]
    elif s and not s.endswith("USDT"):
        s2 = s + "USDT"
        if s2 in markets:
            return True, markets[s2]["symbol"]
    return False, None


def list_available_symbols(exchange: str = "okx", limit: int | None = None) -> list[str]:
    syms = sorted(_markets_cache.get(exchange, {}).keys())
    return syms[:limit] if limit else syms


# ———— 429 指数退避 ————

_retry_backoff: dict[str, float] = {}  # exchange_name → next_allowed_time
_BASE_BACKOFF = 2.0
_MAX_BACKOFF = 16.0
_BACKOFF_MULTIPLIER = 2.0


def _is_rate_limited(exchange: str = "okx") -> bool:
    """检查是否处于 429 冷却期。"""
    next_time = _retry_backoff.get(exchange, 0.0)
    return time.monotonic() < next_time


def _mark_rate_limited(exchange: str = "okx"):
    """触发 429 冷却。"""
    current = _retry_backoff.get(exchange, 0.0)
    delay = _BASE_BACKOFF
    if current > time.monotonic():
        # 已经冷却中 → 翻倍
        elapsed = time.monotonic() - (current - _BASE_BACKOFF)
        delay = min(_BASE_BACKOFF * _BACKOFF_MULTIPLIER ** (elapsed // _BASE_BACKOFF + 1), _MAX_BACKOFF)
    else:
        delay = _BASE_BACKOFF
    _retry_backoff[exchange] = time.monotonic() + delay
    logger.warning(f"[{exchange}] 触发 429 冷却 {delay:.0f}s")


def _mark_rate_ok(exchange: str = "okx"):
    """恢复正常（清除冷却）。"""
    _retry_backoff.pop(exchange, None)


async def _exponential_backoff_wrapper(coro_factory, exchange: str = "okx", label: str = ""):
    """
    带指数退避的执行包装器。
    如果检测到 429 / ExchangeNotAvailable，自动等待后退避重试。
    """
    max_attempts = 5
    last_error = None

    for attempt in range(max_attempts):
        # 检查冷却期
        if _is_rate_limited(exchange):
            wait = _retry_backoff[exchange] - time.monotonic()
            if wait > 0:
                logger.warning(f"[{exchange}] {label}: 429 冷却中，等待 {wait:.1f}s")
                await asyncio.sleep(wait)
                continue

        try:
            result = await coro_factory()
            _mark_rate_ok(exchange)
            return result
        except ccxt.RateLimitExceeded as e:
            _mark_rate_limited(exchange)
            last_error = e
            logger.warning(f"[{exchange}] {label}: RateLimitExceeded, 触发退避")
        except ccxt.ExchangeNotAvailable as e:
            _mark_rate_limited(exchange)
            last_error = e
            logger.warning(f"[{exchange}] {label}: ExchangeNotAvailable, 触发退避")
        except ccxt.NetworkError as e:
            _mark_rate_limited(exchange)
            last_error = e
            logger.warning(f"[{exchange}] {label}: NetworkError, 触发退避")
        except Exception as e:
            # 非网络类错误直接抛出
            raise

    # 重试耗尽
    raise last_error or RuntimeError(f"{label}: 重试耗尽")


# ———— 账户/行情（带缓存）————


async def fetch_balance(exchange: str = "okx") -> dict:
    """获取账户余额，带 5s 缓存。swap 返回 0 时 fallback 到 trading。"""
    async def _do_fetch():
        ex = get_exchange(exchange)
        bal = await _exponential_backoff_wrapper(
            lambda: ex.fetch_balance(params={"type": "swap"}),
            exchange=exchange, label="fetch_balance",
        )
        usdt = bal.get("USDT", {})
        free = usdt.get("free", 0) or 0
        total = usdt.get("total", 0) or 0

        # swap 返回 0 时尝试 trading 账户
        if free == 0 and total == 0:
            bal2 = await _exponential_backoff_wrapper(
                lambda: ex.fetch_balance(params={"type": "trading"}),
                exchange=exchange, label="fetch_balance_fallback",
            )
            u2 = bal2.get("USDT", {})
            free2 = u2.get("free", 0) or 0
            total2 = u2.get("total", 0) or 0
            if free2 > 0 or total2 > 0:
                logger.info(f"[Balance] swap=0, fallback trading: free={free2}")
                free, total = free2, total2

        logger.debug(f"[Balance] exchange={exchange} free={free} total={total}")
        return {
            "total": total,
            "free": free,
            "unrealized_pnl": float(bal.get("info", {}).get("totalUnrealizedProfit", 0) or 0),
        }

    await count_api_call("fetch_balance")
    return await balance_cache.get(f"balance:{exchange}", _do_fetch)


async def fetch_ticker(symbol: str, exchange: str = "okx") -> dict:
    """
    获取行情，带 1s 缓存 + asyncio.Lock 并发合并。
    """
    async def _do_fetch():
        ex = get_exchange(exchange)
        ccxt_sym = _to_ccxt_symbol(symbol, exchange)
        t = await _exponential_backoff_wrapper(
            lambda: ex.fetch_ticker(ccxt_sym),
            exchange=exchange, label=f"fetch_ticker({symbol})",
        )
        return {
            "bid": t.get("bid", 0),
            "ask": t.get("ask", 0),
            "last": t.get("last", 0),
            "mark": t.get("info", {}).get("markPrice", t.get("last", 0)),
        }

    await count_api_call("fetch_ticker")
    return await ticker_cache.get(f"ticker:{exchange}:{symbol}", _do_fetch)


async def fetch_positions(symbol: str | None = None, exchange: str = "okx") -> list[dict]:
    """
    获取持仓，带 2s 缓存 + asyncio.Lock 并发合并。
    始终一次获取 ALL 持仓并缓存，symbol 过滤在本地完成。
    ═══ 所有模块共享同一份缓存 = 整个系统只发一个 HTTP ═══
    """
    async def _do_fetch():
        ex = get_exchange(exchange)
        params = {"instType": "SWAP"}
        # 始终获取全部持仓 — 一次 HTTP 请求服务所有模块
        positions = await _exponential_backoff_wrapper(
            lambda: ex.fetch_positions(symbols=None, params=params),
            exchange=exchange, label="fetch_positions(*)",
        )
        if not positions:
            return []
        result = []
        for p in positions:
            try:
                contracts = float(p.get("contracts") or 0)
                if contracts <= 0:
                    continue
                result.append({
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "contracts": contracts,
                    "entry_price": float(p.get("entryPrice") or 0),
                    "mark_price": float(p.get("markPrice") or 0),
                    "unrealized_pnl": float(p.get("unrealizedPnl") or 0),
                    "leverage": int(p.get("leverage") or 1),
                    "liquidation_price": p.get("liquidationPrice"),
                    "margin_mode": p.get("marginMode") or "cross",
                })
            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"[{exchange}] 解析持仓行失败: {p.get('symbol')} {e}")
                continue
        return result

    await count_api_call("fetch_positions")
    # 单一缓存键 — 所有模块共享同一份数据
    key = f"positions:{exchange}:*"
    all_positions = await position_cache.get(key, _do_fetch)

    # symbol 过滤在本地完成
    if symbol:
        ccxt_target = _to_ccxt_symbol(symbol, exchange)
        return [p for p in all_positions if p["symbol"] == ccxt_target]
    return all_positions


async def fetch_positions_batch(exchange: str = "okx") -> list[dict]:
    """
    批量获取全部持仓 — 仅从 Position Snapshot 读取，不发 HTTP 请求。

    ═══════════════════════════════════════════════════════════════
    严禁：不再触发任何 API 调用。Snapshot 未就绪时返回空列表。
    ═══════════════════════════════════════════════════════════════
    """
    return get_full_snapshot(exchange)


async def count_positions(exchange: str = "okx") -> int:
    """统计持仓数（从 Position Snapshot 读取，零 HTTP）。"""
    positions = get_full_snapshot(exchange)
    return sum(1 for p in positions if float(p.get("contracts") or 0) > 0)


# ======================================================================
# Position Snapshot — 由 PositionManager 统一管理（WS实时 + REST恢复）
# 所有模块通过以下函数读取缓存，禁止直接 fetch_positions()
# ======================================================================

from .position_manager import position_manager as _pm


async def refresh_position_snapshot(exchange: str = "okx") -> bool:
    """
    [兼容] 委托 PositionManager 执行 REST 刷新。
    PositionManager 内部有 Refresh Lock，并发安全。
    仅在 WS 断线等场景由 Watchdog 触发，正常不应手动调用。
    """
    return await _pm.rest_refresh()


def get_position_from_snapshot(symbol: str, exchange: str = "okx",
                                side: str | None = None) -> dict | None:
    """
    Read single position from Runtime's PositionTracker (zero HTTP).
    Delegates to ExchangeRuntime — the single source of truth for all position data.
    """
    from core.exchange_runtime import runtime
    return runtime.get_position(_to_ccxt_symbol(symbol, exchange), side)


def get_full_snapshot(exchange: str = "okx") -> list[dict]:
    """返回原始持仓列表。统一由 PositionManager 提供缓存。"""
    return _pm.get_snapshot_raw()


def snapshot_age(exchange: str = "okx") -> float:
    """缓存已存在时间（秒）。"""
    return _pm.get_age()


def is_snapshot_healthy(exchange: str = "okx") -> bool:
    """检查缓存是否健康。"""
    return _pm.is_healthy()


def get_snapshot_metadata(exchange: str = "okx") -> dict:
    """获取 Snapshot 完整元数据。"""
    return _pm.get_metadata()


async def init_position_manager(api_key: str, api_secret: str, passphrase: str,
                                 exchange_name: str = "okx") -> None:
    """
    Start position management via ExchangeRuntime (WS + Watchdog).
    Called once during lifecycle initialization.
    """
    from core.exchange_runtime import runtime
    await runtime.start(api_key, api_secret, passphrase, exchange_name=exchange_name)


async def stop_position_manager() -> None:
    """Stop position management via ExchangeRuntime."""
    from core.exchange_runtime import runtime
    await runtime.shutdown()


# ———— 杠杆 ————


async def check_position_mode(exchange='okx'):
    try:
        ex = get_exchange(exchange)
        mode = await ex.fetch_position_mode()
        hedged = mode.get('hedged', False)
        nm = 'long_short_mode' if hedged else 'net_mode'
        logger.info('[PositionMode] account=' + exchange + ' mode=' + nm)
        return hedged
    except Exception as e:
        logger.warning('[PositionMode] query failed: ' + str(e))
        return True

async def set_margin_mode(symbol: str, mode: str = "isolated", exchange: str = "okx", leverage: int = 10) -> bool:
    """设置逐仓/全仓。仅传 lever，不传 posSide(属于setLeverage)。"""
    try:
        ex = get_exchange(exchange)
        ccxt_sym = _to_ccxt_symbol(symbol, exchange)
        params = {"lever": leverage}
        logger.info(f"[MarginMode Request] {ccxt_sym} params={params}")
        await ex.set_margin_mode(mode, ccxt_sym, params)
        logger.info(f"[OKX Margin] symbol={ccxt_sym} mode={mode} leverage={leverage}")
        return True
    except Exception as e:
        err = str(e).lower()
        if "already set" in err or "position" in err:
            logger.debug(f"[OKX Margin] {ccxt_sym} 已是 {mode} 模式")
            return True
        logger.error(f"[OKX Margin] {ccxt_sym} 设置 {mode} 失败: {e}")
        return False


async def set_leverage(symbol: str, leverage: int, exchange: str = "okx", side: str = "long") -> bool:
    """设置杠杆。hedgeMode 下使用 posSide: long/short。"""
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    ex = get_exchange(exchange)
    try:
        await ex.set_leverage(leverage, ccxt_sym, {"marginMode": "isolated", "posSide": side})
        return True
    except Exception as e:
        logger.error(f"[{exchange}] 设置杠杆失败 {symbol} {leverage}x: {e}")
        return False


# ———— 下单 ————

def _to_ccxt_symbol(symbol: str, exchange: str = "okx") -> str:
    """SOLUSDT → SOL/USDT:USDT (用缓存查找)"""
    if "/" in symbol:
        return symbol
    markets = _markets_cache.get(exchange, {})
    m = markets.get(symbol.upper())
    if m:
        return m["symbol"]
    return symbol


async def create_order(
    symbol: str, ordertype: str, side: str, amount: float,
    rate: float | None = None, reduce_only: bool = False,
    exchange: str = "okx", pos_side: str = "", **params_extra,
) -> dict:
    """创建订单。下完失效相关缓存。

    P1: 添加 clOrdId 实现幂等性（OKX V5 API 官方最佳实践）
    P1: 去掉冗余 fetch_order（create_order 已返回完整订单信息）
    """
    import uuid

    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    ex = get_exchange(exchange)
    if not pos_side:
        pos_side = "short" if (side == "buy") == reduce_only else "long"

    # 生成唯一 clOrdId（OKX 要求：仅字母数字，最大32位，不能有下划线）
    cl_ord_id = f"bot{uuid.uuid4().hex[:18]}"

    params = {
        "tdMode": "isolated",
        "posSide": pos_side,
        "reduceOnly": reduce_only,
        "clOrdId": cl_ord_id,  # P1: 幂等性保护
        **params_extra
    }
    logger.info(f"[{exchange}] CREATE ORDER type={ordertype} symbol={ccxt_sym} side={side} posSide={pos_side} amount={amount} price={rate} clOrdId={cl_ord_id}")

    from core.order_submission import submit_and_confirm
    raw = await submit_and_confirm(ex, ccxt_sym, ordertype, side, amount, rate, params,
                                   exchange=exchange, trigger=ordertype in ("stop_limit", "stop_market", "take_profit_limit"))

    # P1: create_order 已返回完整订单信息，无需再次 fetch_order
    # 参考：CCXT 官方文档 - create_order 返回标准化订单对象
    order = raw
    logger.info(f"[{exchange}] ORDER OK id={order.get('id')} clOrdId={cl_ord_id} status={order.get('status')}")

    # 下单后失效相关缓存（单一共享键）
    position_cache.invalidate(f"positions:{exchange}:*")
    balance_cache.invalidate(f"balance:{exchange}")
    return order


async def create_stoploss_order(
    symbol: str, side: str, amount: float, stop_price: float,
    order_type: str = "stop_market", exchange: str = "okx",
    client_order_id: str = "",
) -> dict:
    """
    止损单。支持 clientOrderId 实现幂等性。

    网络结果未知时仅查询原客户端订单号，不依赖交易所永久去重。
    """
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    ex = get_exchange(exchange)
    pos_side = "long" if side == "sell" else "short"
    params = {"tdMode": "isolated", "posSide": pos_side, "stopPrice": stop_price, "reduceOnly": True}
    import uuid
    params["clOrdId"] = client_order_id or f"bot{uuid.uuid4().hex[:18]}"
    logger.info(f"[{exchange}] create_stoploss: symbol={ccxt_sym} side={side} posSide={pos_side} amount={amount} stop={stop_price} clOrdId={client_order_id}")
    from core.order_submission import submit_and_confirm
    order = await submit_and_confirm(ex, ccxt_sym, order_type, side, amount, stop_price, params,
                                     exchange=exchange, trigger=True)
    logger.info(f"[{exchange}] 止损单完成 → {order.get('id')}")
    # 失效位置缓存（仓位可能变化）
    position_cache.invalidate(f"positions:{exchange}:*")
    return order


async def create_tp_order(
    symbol: str, side: str, amount: float, trigger_price: float,
    exchange: str = "okx",
    client_order_id: str = "",
) -> dict:
    """
    止盈限价单。支持 clientOrderId 实现幂等性。
    """
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    ex = get_exchange(exchange)
    pos_side = "long" if side == "sell" else "short"
    params = {"tdMode": "isolated", "posSide": pos_side, "stopPrice": trigger_price, "price": trigger_price, "reduceOnly": True}
    import uuid
    params["clOrdId"] = client_order_id or f"bot{uuid.uuid4().hex[:18]}"
    logger.info(f"[{exchange}] create_tp: symbol={ccxt_sym} side={side} posSide={pos_side} amount={amount} trigger={trigger_price} clOrdId={client_order_id}")
    from core.order_submission import submit_and_confirm
    order = await submit_and_confirm(ex, ccxt_sym, "take_profit_limit", side, amount, trigger_price, params,
                                     exchange=exchange, trigger=True)
    logger.info(f"[{exchange}] 止盈单完成 → {order.get('id')}")
    # 失效位置缓存（单一共享键）
    position_cache.invalidate(f"positions:{exchange}:*")
    return order


async def cancel_order(order_id: str, symbol: str, exchange: str = "okx"):
    """撤单。失效位置缓存。

    P1: 使用 _to_ccxt_symbol 统一 symbol 转换（CCXT 官方最佳实践）
    P2: 51400（订单不存在/已撤销）视为成功——订单已不在正是撤单目标，
        不抛异常、不触发熔断器，与 cancel_algo_order_by_id 行为一致。
    """
    ex = get_exchange(exchange)
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    try:
        await _exponential_backoff_wrapper(
            lambda: ex.cancel_order(order_id, ccxt_sym),
            exchange=exchange, label=f"cancel_order({order_id})",
        )
    except Exception as e:
        err_str = str(e)
        # 51400 = order does not exist or already cancelled
        if "51400" in err_str or "does not exist" in err_str.lower():
            logger.debug(f"[{exchange}] cancel_order {order_id[:16]}: already gone")
        else:
            raise
    # 撤单后失效位置缓存
    position_cache.invalidate(f"positions:{exchange}:*")


async def fetch_order(order_id: str, symbol: str, exchange: str = "okx", include_algo: bool = True) -> dict | None:
    """
    查询订单状态。不缓存。

    自动处理 OKX algo 订单（TP/SL/条件单）：
    - 先用普通订单端点查询
    - 如果没查到且 include_algo=True，自动用 algo 端点再查一次
    """
    await count_api_call("fetch_order")
    ex = get_exchange(exchange)
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)

    # 尝试1: 普通订单
    try:
        result = await _exponential_backoff_wrapper(
            lambda: ex.fetch_order(order_id, ccxt_sym),
            exchange=exchange, label=f"fetch_order({order_id})",
        )
        if result:
            return result
    except Exception:
        pass

    # 尝试2: algo 订单（TP/SL/条件单用不同的 API 端点）
    if include_algo:
        try:
            result = await _exponential_backoff_wrapper(
                lambda: ex.fetch_order(order_id, ccxt_sym, {"method": "privateGetTradeOrderAlgo"}),
                exchange=exchange, label=f"fetch_algo_order({order_id})",
            )
            if result:
                return result
        except Exception:
            pass

    return None


async def fetch_open_orders(symbol: str | None = None, exchange: str = "okx") -> list[dict]:
    """
    查询未成交订单，带 2s 缓存。
    始终一次获取 ALL 未成交单并缓存，symbol 过滤在本地完成。
    ═══ 所有模块共享同一份缓存 ═══
    """
    async def _do_fetch():
        ex = get_exchange(exchange)
        return await _exponential_backoff_wrapper(
            lambda: ex.fetch_open_orders(symbol=None),
            exchange=exchange, label="fetch_open_orders(*)",
        )

    await count_api_call("fetch_open_orders")
    all_orders = await open_orders_cache.get(f"open_orders:{exchange}:*", _do_fetch)

    if symbol:
        ccxt_target = _to_ccxt_symbol(symbol, exchange)
        return [o for o in all_orders if o.get("symbol") == ccxt_target]
    return all_orders


async def cancel_all_orders(symbol: str, exchange: str = "okx"):
    """取消所有订单。"""
    ex = get_exchange(exchange)
    await ex.cancel_all_orders(symbol)
    position_cache.invalidate(f"positions:{exchange}:*")


# ———— Algo 订单（TP/SL/条件单） ————


async def fetch_pending_algo_orders(symbol: str, exchange: str = "okx") -> list[dict]:
    """
    查询所有 pending 状态的条件单（TP/SL/Trigger/Trailing 等）。
    对应 OKX API: GET /api/v5/trade/orders-algo-pending

    返回: [{algoId, instId, ordType, side, sz, triggerPx, ...}, ...]
    """
    ex = get_exchange(exchange)
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    try:
        # v6: Cover ALL valid OKX ordType values for algo orders.
        # Ref: OKX API v5 GET /api/v5/trade/orders-algo-pending
        # Valid ordType: conditional, oco, trigger, move_order_stop, iceberg, twap
        all_data = []
        for ord_type in ("conditional", "move_order_stop", "trigger", "oco"):
            try:
                resp = await _exponential_backoff_wrapper(
                    lambda ot=ord_type: ex.privateGetTradeOrdersAlgoPending({
                        "instType": "SWAP",
                        "ordType": ot,
                        "state": "live",
                    }),
                    exchange=exchange, label=f"fetch_pending_algo({ord_type})",
                )
                data = resp.get("data", []) if isinstance(resp, dict) else []
                # Filter locally by instId — normalize BOTH sides to short format
                norm = ccxt_sym.upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")
                for d in data:
                    d_inst = (d.get("instId") or "").upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")
                    if d_inst == norm:
                        all_data.append(d)
            except Exception as inner_e:
                logger.warning(f"[{exchange}] fetch_pending_algo ordType={ord_type} failed: {inner_e}")
        return all_data
    except Exception as e:
        # v5: RAISE on complete failure
        logger.warning(f"[{exchange}] 查询条件单失败 {symbol}: {e}")
        raise


async def cancel_algo_orders(orders: list[dict], exchange: str = "okx") -> bool:
    """
    批量取消条件单。
    对应 OKX API: POST /api/v5/trade/cancel-algos

    orders: [{algoId, instId}, ...]
    返回: True 如果全部取消成功或无可取消订单
    """
    if not orders:
        return True

    ex = get_exchange(exchange)
    # OKX 批量取消：最多 20 个 per request
    batch_size = 20
    all_ok = True
    for i in range(0, len(orders), batch_size):
        batch = orders[i:i + batch_size]
        params = [
            {"algoId": o["algoId"], "instId": o["instId"]}
            for o in batch if o.get("algoId")
        ]
        if not params:
            continue
        label = f"cancel_algo({len(params)}项)"
        try:
            await _exponential_backoff_wrapper(
                lambda: ex.privatePostTradeCancelAlgos(params),
                exchange=exchange, label=label,
            )
            logger.info(f"[{exchange}] 批量取消条件单: {len(params)} 项成功")
        except Exception as e:
            logger.warning(f"[{exchange}] 批量取消条件单失败: {e}")
            all_ok = False

    if all_ok:
        position_cache.invalidate(f"positions:{exchange}:*")
    return all_ok


async def fetch_algo_order_by_id(algo_id: str, symbol: str, exchange: str = "okx") -> dict | None:
    """
    Get a single algo order by its algoId.
    Uses OKX GET /api/v5/trade/order-algo endpoint.

    Returns: {algoId, instId, ordType, state, side, sz, triggerPx, ...} or None.
    This is the CORRECT way to verify SL/TP existence — direct AlgoID lookup.

    NOTE: This queries a specific algoId, not the pending-algo list.
    It will find orders even if they've been triggered/filled.
    """
    ex = get_exchange(exchange)
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    try:
        resp = await _exponential_backoff_wrapper(
            lambda: ex.privateGetTradeOrderAlgo({
                "algoId": algo_id,
                "instId": ccxt_sym,
            }),
            exchange=exchange, label=f"fetch_algo_by_id({algo_id[:16]})",
        )
        data = resp.get("data", []) if isinstance(resp, dict) else []
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception as e:
        err_str = str(e)
        # 51400 = order does not exist
        if "51400" in err_str or "does not exist" in err_str.lower():
            return None
        logger.debug(f"[{exchange}] fetch_algo_by_id {algo_id[:16]} failed: {e}")
        raise


async def cancel_algo_order_by_id(algo_id: str, symbol: str, exchange: str = "okx") -> bool:
    """
    Cancel a single algo order by its algoId.
    Uses OKX POST /api/v5/trade/cancel-algos endpoint.

    Returns True if cancelled successfully or if order doesn't exist.
    """
    ex = get_exchange(exchange)
    ccxt_sym = _to_ccxt_symbol(symbol, exchange)
    try:
        await _exponential_backoff_wrapper(
            lambda: ex.privatePostTradeCancelAlgos([{
                "algoId": algo_id,
                "instId": ccxt_sym,
            }]),
            exchange=exchange, label=f"cancel_algo_by_id({algo_id[:16]})",
        )
        logger.info(f"[{exchange}] cancel_algo_by_id: {algo_id[:16]} success")
        position_cache.invalidate(f"positions:{exchange}:*")
        return True
    except Exception as e:
        err_str = str(e)
        # 51400 = order does not exist or already cancelled
        if "51400" in err_str or "does not exist" in err_str.lower():
            logger.debug(f"[{exchange}] cancel_algo_by_id {algo_id[:16]}: already gone")
            return True
        logger.warning(f"[{exchange}] cancel_algo_by_id {algo_id[:16]} failed: {err_str[:120]}")
        raise


# ———— 合约限额 ————

def _get_info_field(symbol: str, field: str, exchange: str = "okx") -> float | None:
    """从 OKX 原始 info 中读取字段。"""
    sym_upper = symbol.upper().replace("/", "").replace(":USDT", "")
    m = _markets_cache.get(exchange, {}).get(sym_upper)
    if m:
        val = (m.get("info") or {}).get(field)
        if val is not None:
            return float(val)
    return None


def get_ct_val(symbol: str, exchange: str = "okx") -> float:
    """合约面值。"""
    sym_upper = symbol.upper().replace("/", "").replace(":USDT", "")
    m = _markets_cache.get(exchange, {}).get(sym_upper)
    if m:
        cs = m.get("contractSize")
        if cs is not None:
            return float(cs)
    return _get_info_field(symbol, "ctVal", exchange) or 1.0


def get_max_market_size(symbol: str, exchange: str = "okx") -> float | None:
    """市价单最大允许合约张数。"""
    return _get_info_field(symbol, "maxMktSz", exchange)


# ———— 合约精度 ————

def amount_to_precision(symbol: str, amount: float, exchange: str = "okx") -> float:
    """数量按交易所精度对齐。"""
    ex = _exchanges.get(exchange)
    if ex:
        return float(ex.amount_to_precision(symbol, amount))
    return amount


def price_to_precision(symbol: str, price: float, exchange: str = "okx") -> float:
    """价格按交易所 tick size 对齐。"""
    ex = _exchanges.get(exchange)
    if ex:
        return float(ex.price_to_precision(symbol, price))
    return price


def get_precision_amount(symbol: str, exchange: str = "okx") -> int:
    """获取合约数量精度。"""
    sym_upper = symbol.upper().replace("/", "").replace(":USDT", "")
    m = _markets_cache.get(exchange, {}).get(sym_upper)
    if m:
        precision = m.get("precision", {}).get("amount")
        if precision is not None:
            return int(precision) if precision >= 0 else abs(int(precision))
    precisions = {
        "BTCUSDT": 3, "ETHUSDT": 2, "SOLUSDT": 0, "BNBUSDT": 1,
        "XRPUSDT": 0, "DOGEUSDT": 0, "ADAUSDT": 0, "AVAXUSDT": 0,
        "DOTUSDT": 0, "LINKUSDT": 1, "SUIUSDT": 0, "APTUSDT": 0,
        "PEPEUSDT": 0, "SHIBUSDT": 0, "WIFUSDT": 0, "BONKUSDT": 0,
        "FLOKIUSDT": 0,
    }
    return precisions.get(sym_upper, 0)


def get_default_leverage(symbol: str) -> int:
    """获取币种默认杠杆。BTC/ETH/DOGE/SOL = 20x，其他 = 10x。"""
    coin = symbol.upper().replace("/", "").replace(":USDT", "").replace("USDT", "")
    if coin in ("BTC", "ETH", "DOGE", "SOL"):
        return 20
    return 10


def get_max_amount(symbol: str, exchange: str = "okx") -> float:
    """获取限价单最大合约张数。"""
    return _get_info_field(symbol, "maxLmtSz", exchange) or 0.0


def get_min_contracts(symbol: str, exchange: str = "okx") -> float:
    """获取最小合约张数 (minSz / limits.amount.min) — 仅用于数量校验参考。"""
    sym_upper = symbol.upper().replace("/", "").replace(":USDT", "")
    m = _markets_cache.get(exchange, {}).get(sym_upper)
    if m:
        limits_min = m.get("limits", {}).get("amount", {}).get("min")
        if limits_min is not None:
            return float(limits_min)
    return _get_info_field(symbol, "minSz", exchange) or 0.001


# ======================================================================
# 仓位对账系统 — OKX = 唯一真相源
# ======================================================================


def normalize_order_amount(symbol: str, raw_amount: float, exchange: str = "okx") -> float | None:
    """
    统一数量规范化函数。

    返回值：
        float → 规范化后的合法数量
        None  → 无法生成合法下单数量，调用方应跳过

    精度处理：
        step >= 1 → 整数合约 → math.floor
        step < 1  → 委托 CCXT amount_to_precision（支持 0.01/0.05/0.25/0.125）
    """
    if raw_amount <= 0:
        return None

    sym_upper = symbol.upper().replace("/", "").replace(":USDT", "")
    markets = _markets_cache.get(exchange, {})
    m = markets.get(sym_upper)
    if not m:
        logger.debug(f"[Normalize][{exchange}] {sym_upper}: 无 market 信息，返回原始值 {raw_amount}")
        return raw_amount

    precision = m.get("precision", {}) or {}
    step = precision.get("amount", 0)
    if step is None:
        step = 0

    limits = m.get("limits", {}) or {}
    amt_limits = limits.get("amount", {}) or {}
    min_amt = float(amt_limits.get("min", 0) or 0)
    max_amt = float(amt_limits.get("max", 0) or 0)

    if step >= 1:
        normalized = math.floor(raw_amount)
        detail = f"step={step} floor={normalized}"
    else:
        try:
            ex = _exchanges.get(exchange)
            if ex:
                ns = ex.amount_to_precision(symbol, raw_amount)
                normalized = float(ns) if ns else 0.0
            else:
                normalized = raw_amount
            detail = f"step={step} ccxt={normalized}"
        except Exception as e:
            logger.debug(f"[Normalize][{exchange}] {sym_upper}: CCXT 失败: {e}")
            normalized = raw_amount
            detail = f"step={step} fallback={normalized}"

    if normalized is None or normalized <= 0:
        logger.warning(f"[Normalize][{exchange}] {sym_upper}: raw={raw_amount} {detail} → None")
        return None

    if min_amt > 0 and normalized < min_amt:
        logger.warning(f"[Normalize][{exchange}] {sym_upper}: raw={raw_amount} {detail} → None (<min={min_amt})")
        return None

    if max_amt > 0 and normalized > max_amt:
        logger.warning(f"[Normalize][{exchange}] {sym_upper}: raw={raw_amount} {detail} → {max_amt} (capped)")
        normalized = max_amt

    logger.debug(f"[Normalize][{exchange}] {sym_upper}: raw={raw_amount} {detail} final={normalized}")
    return float(normalized)


async def fetch_position_raw(symbol: str, exchange: str = "okx",
                              side: str | None = None) -> dict | None:
    """
    读取单币种持仓 — 仅从 Position Snapshot 读取，不发 HTTP 请求。

    ═══════════════════════════════════════════════════════════════
    严禁：此函数不再触发任何 API 调用。
    所有持仓数据必须来自监控循环中 refresh_position_snapshot()
    预先填充的 Position Snapshot。
    ═══════════════════════════════════════════════════════════════
    """
    snap_pos = get_position_from_snapshot(symbol, exchange, side)
    if snap_pos is not None:
        return snap_pos

    # Position Snapshot 未就绪 → 返回 None，不再触发 API 调用
    logger.warning(
        f"[{exchange}] Position Snapshot 未就绪，{symbol} 返回 None "
        f"（避免 API 请求 — 请确保 refresh_position_snapshot 已在监控循环中调用）"
    )
    return None


async def reconcile_single_position(
    session,
    trade,
    symbol: str,
    exchange: str = "okx",
    label: str = "",
    pos: dict | None = None,
) -> float | None:
    """
    单仓位对账 — OKX vs DB（从共享 position_cache 读取）。

    1. fetch_position_raw() 从共享缓存读取（不单独发 HTTP）
    2. 如果 OKX 无仓位 → 返回 None，**不再修改 Trade 状态**
       （trade 的关闭应由 reconciler 的 sync_trade_with_okx 统一处理）
    3. 如果数量不一致 → 更新 DB 数量，返回 OKX 数量
    4. 记录 [Reconciliation] 日志

    参数：
        session: SQLAlchemy Session
        trade: Trade 对象（会被修改）
        symbol: 币种
        exchange: 交易所名
        label: 日志标签（如 "挂TP前" / "挂SL前"）
        pos: 可选的已有持仓数据（调用方已获取时传入，避免重复查询）

    返回：
        OKX 实际合约数（> 0）或 None（无仓位）

    ═══ 优化原则：所有模块共享同一份 position_cache ═══
    ═══ 修复：不再在负缓存时关闭 Trade（修复 Reply 匹配失败 Bug） ═══
    """
    if pos is None:
        pos = await fetch_position_raw(symbol, exchange)

    if pos is None:
        # 缓存未命中 → 返回 None
        # 注意：此处不再关闭 Trade。Position Snapshot 可能只是尚未刷新。
        # 正确的 Trade 关闭应由 reconciler.sync_trade_with_okx() 统一处理，
        # 该函数有完善的健康检查和"未过期入场单保护"机制。
        if trade:
            logger.debug(
                f"[Reconciliation] {label} {symbol}: "
                f"OKX 缓存无仓位，跳过（trade.id={trade.id} 等待 Snapshot 刷新）"
            )
        return None

    ex_amount = pos["contracts"]

    if trade and trade.amount != ex_amount:
        old_amt = trade.amount
        trade.amount = ex_amount
        logger.info(
            f"[Reconciliation] {label} {symbol}: "
            f"DB={old_amt} OKX={ex_amount} → Database corrected."
        )

    return ex_amount


def normalize_tp_levels(
    symbol: str,
    raw_tp_levels: list,
    total_contracts: float,
    exchange: str = "okx",
) -> list[dict]:
    """
    TP 自动重分配 — 确保所有 TP 数量之和严格等于实际持仓。

    规则：
    - 持仓太小无法拆分 → 全部放在 TP1，其余 TP 为 0
    - 可拆分 → 按比例分配，最后一个 TP 自动补足剩余
    - 所有数量经过 normalize_order_amount 校验

    返回：
        [{"price": float, "close_pct": float, "contracts": float}, ...]
        close_pct 基于 total_contracts 计算
        contracts 是绝对合约数
    """
    if not raw_tp_levels or total_contracts <= 0:
        return []

    # 提取有效 TP 价格
    prices = []
    for tp in raw_tp_levels:
        if isinstance(tp, dict) and tp.get("price", 0) > 0:
            price = float(tp["price"])
            close_pct = float(tp.get("close_pct", 100.0 / max(1, len(raw_tp_levels))))
            prices.append({"price": price, "close_pct": close_pct})

    if not prices:
        return []

    n = len(prices)
    result = []

    # — 仓位太小，无法拆分 → 全部 TP1 —
    if total_contracts < n:
        tp1_amt = normalize_order_amount(symbol, total_contracts, exchange)
        if tp1_amt is None or tp1_amt <= 0:
            return []
        result.append({
            "price": prices[0]["price"],
            "close_pct": 100.0,
            "contracts": tp1_amt,
        })
        for i in range(1, n):
            result.append({
                "price": prices[i]["price"],
                "close_pct": 0.0,
                "contracts": 0.0,
            })
        return result

    # — 正常分配 —
    assigned = 0.0
    for i in range(n):
        if i == n - 1:
            # 最后一个 TP → 补足剩余
            contracts = normalize_order_amount(symbol, total_contracts - assigned, exchange)
        else:
            raw_ct = total_contracts * (prices[i]["close_pct"] / 100.0)
            contracts = normalize_order_amount(symbol, raw_ct, exchange)
            if contracts is None or contracts <= 0:
                continue
            # 确保不超额分配
            if assigned + contracts > total_contracts:
                contracts = normalize_order_amount(symbol, total_contracts - assigned, exchange)

        if contracts is None or contracts <= 0:
            result.append({
                "price": prices[i]["price"],
                "close_pct": 0.0,
                "contracts": 0.0,
            })
        else:
            assigned += contracts
            result.append({
                "price": prices[i]["price"],
                "close_pct": (contracts / total_contracts * 100.0) if total_contracts > 0 else 0.0,
                "contracts": contracts,
            })

    return result


def find_market(symbol: str, exchange: str = "okx") -> dict | None:
    """获取 market 信息。"""
    sym_upper = symbol.upper().replace("/", "").replace(":USDT", "")
    return _markets_cache.get(exchange, {}).get(sym_upper)
