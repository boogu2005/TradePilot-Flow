"""
统一 API 缓存层 — 带 TTL 的缓存 + asyncio.Lock 防止并发重复请求。

缓存项：
  Position Cache  TTL=2s  — fetch_positions()
  OpenOrder Cache TTL=2s  — fetch_open_orders()
  Balance Cache   TTL=5s  — fetch_balance()
  Ticker Cache    TTL=1s  — fetch_ticker()

核心特性：
  - 每个缓存附带 KeyLock（per-symbol / per-exchange lock）
  - 多个协程同时请求同一资源时仅执行一次，其余等待结果
  - [API] 日志标记 cache_hit / cache_miss / 耗时
  - 429 自动指数退避（由 exchange.py 配合）
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from loguru import logger

# ======================================================================
# 缓存条目
# ======================================================================

class CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at


# ======================================================================
# 每个缓存键的锁 — 防止并发重复请求
# ======================================================================

class KeyLock:
    """每个 cache_key 一把锁。同一个 key 同时只会有一个真正请求。"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._pending_result: Any = None
        self._pending_event: asyncio.Event | None = None

    async def acquire(self, fetcher: Callable, *args, **kwargs) -> Any:
        """
        获取锁。如果已有等待中的请求，等待其完成并共享结果。
        如果自己是第一个，执行 fetcher 并通知其他等待者。
        """
        # 如果已经有正在进行的请求，等待它完成
        if self._pending_event is not None:
            event = self._pending_event
            await event.wait()
            return self._pending_result

        # 自己是第一个
        self._pending_event = asyncio.Event()
        try:
            result = await fetcher(*args, **kwargs)
            self._pending_result = result
            return result
        finally:
            self._pending_event.set()
            # 清理，以便下次重新锁
            if self._pending_event is not None:
                # 给其他等待者短暂时间读取结果
                await asyncio.sleep(0)
                self._pending_event = None
                self._pending_result = None


# ======================================================================
# API 缓存容器
# ======================================================================

class ApiCache:
    """带 TTL 的缓存 + 异步锁。"""

    def __init__(self, name: str, default_ttl: float):
        self.name = name
        self.default_ttl = default_ttl
        self._store: dict[str, CacheEntry] = {}
        self._locks: dict[str, KeyLock] = {}

    def _get_lock(self, key: str) -> KeyLock:
        if key not in self._locks:
            self._locks[key] = KeyLock()
        return self._locks[key]

    async def get(
        self, key: str,
        fetcher: Callable,
        ttl: float | None = None,
        *args, **kwargs,
    ) -> Any:
        """
        获取缓存。如果缓存未过期 → 直接返回。
        如果过期 → 加锁执行 fetcher（并发重复请求合并）。
        """
        now = time.monotonic()
        entry = self._store.get(key)

        # 缓存命中且未过期
        if entry is not None and not entry.expired:
            logger.debug(f"[API] {self.name} cache_hit=True key={key} age={now - (entry.expires_at - self.default_ttl):.0f}ms")
            return entry.value

        # 缓存过期或不存在 → 加锁请求
        lock = self._get_lock(key)
        effective_ttl = ttl if ttl is not None else self.default_ttl

        start = time.perf_counter()
        try:
            value = await lock.acquire(fetcher, *args, **kwargs)
        except Exception:
            # 如果之前有缓存，且请求失败，用过期缓存兜底（stale-while-revalidate）
            if entry is not None:
                logger.warning(f"[API] {self.name} fetch_failed key={key} 使用过期缓存兜底")
                return entry.value
            raise

        elapsed = (time.perf_counter() - start) * 1000
        is_hit = entry is not None and entry.expired

        if is_hit:
            logger.debug(f"[API] {self.name} cache_hit=False(key_expired) key={key} 耗时={elapsed:.0f}ms")
        else:
            logger.debug(f"[API] {self.name} cache_hit=False key={key} 耗时={elapsed:.0f}ms TTL={effective_ttl:.0f}s")

        # 写入缓存
        self._store[key] = CacheEntry(value, effective_ttl)
        return value

    def invalidate(self, key: str):
        """主动失效某个缓存（例如开仓后）。"""
        self._store.pop(key, None)

    def invalidate_all(self):
        """全部失效。"""
        self._store.clear()

    def get_stats(self) -> dict:
        """当前缓存状态。"""
        now = time.monotonic()
        alive = 0
        expired = 0
        for entry in self._store.values():
            if entry.expired:
                expired += 1
            else:
                alive += 1
        return {
            "name": self.name,
            "entries": len(self._store),
            "alive": alive,
            "expired": expired,
        }


# ======================================================================
# 全局缓存实例
# ======================================================================

# Ticker 缓存 — TTL = 1 秒
ticker_cache = ApiCache("fetch_ticker", default_ttl=1.0)

# Position 缓存 — TTL = 2 秒
position_cache = ApiCache("fetch_positions", default_ttl=2.0)

# Balance 缓存 — TTL = 5 秒
balance_cache = ApiCache("fetch_balance", default_ttl=5.0)

# Open Orders 缓存 — TTL = 2 秒
open_orders_cache = ApiCache("fetch_open_orders", default_ttl=2.0)


# ======================================================================
# 缓存统计（供日志/health 使用）
# ======================================================================

_api_counters: dict[str, int] = {}
_api_counter_lock = asyncio.Lock()


async def count_api_call(name: str):
    """记录 API 调用次数（线程安全）。"""
    async with _api_counter_lock:
        _api_counters[name] = _api_counters.get(name, 0) + 1


def get_api_stats() -> dict:
    """获取所有 API 调用统计。"""
    caches = {
        "ticker": ticker_cache.get_stats(),
        "positions": position_cache.get_stats(),
        "balance": balance_cache.get_stats(),
        "open_orders": open_orders_cache.get_stats(),
    }
    return {
        "caches": caches,
        "counts": dict(_api_counters),
    }


def reset_api_stats():
    """重置统计（测试用）。"""
    _api_counters.clear()
    ticker_cache.invalidate_all()
    position_cache.invalidate_all()
    balance_cache.invalidate_all()
    open_orders_cache.invalidate_all()
