"""后台任务：账户权益快照采集 + WebSocket 广播。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from .. import config
from ..models.dashboard_models import DashboardEquitySnapshot
from ..database import db
from . import okx


# ---------- 账户快照 ----------

_last_snapshot_value: tuple[float, float] | None = None


async def _record_snapshot() -> bool:
    """采集一次账户快照，有变化才落库。"""
    global _last_snapshot_value
    balance = await okx.fetch_balance()
    if balance is None:
        return False
    total = balance.get("total", 0.0)
    unrealized = balance.get("unrealized_pnl", 0.0)
    if _last_snapshot_value is not None and abs(total - _last_snapshot_value[0]) < 0.5 and abs(unrealized - _last_snapshot_value[1]) < 0.5:
        return False
    _last_snapshot_value = (total, unrealized)
    session = db.dashboard_session()
    try:
        session.add(
            DashboardEquitySnapshot(
                time=datetime.now(timezone.utc).replace(tzinfo=None),
                balance=total,
                equity=total + unrealized,
                available=balance.get("free", 0.0),
                unrealized_pnl=unrealized,
                realized_pnl=0.0,
                open_positions=0,
                source="okx",
            )
        )
        session.commit()
        return True
    finally:
        session.close()


async def equity_snapshotter() -> None:
    """后台循环：周期采集权益快照。"""
    if not okx.enabled():
        logger.info("OKX 实时数据未启用，跳过权益快照任务")
        return
    logger.info("启动权益快照任务")
    while True:
        try:
            await _record_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"权益快照失败: {exc}")
        await asyncio.sleep(config.SNAPSHOT_INTERVAL_SECONDS)


# ---------- WebSocket 广播 ----------

class BroadcastHub:
    """简易 WebSocket 广播中心。"""

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: Any) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        logger.info(f"WS 客户端接入，当前 {len(self._clients)} 个")

    async def disconnect(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        async with self._lock:
            targets = list(self._clients)
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                async with self._lock:
                    self._clients.discard(ws)


hub = BroadcastHub()


async def ws_broadcaster() -> None:
    """后台循环：定期向所有 WS 客户端推送账户+持仓快照。"""
    from .account import get_account_summary
    from .positions import get_positions

    logger.info("启动 WS 广播任务")
    while True:
        try:
            account = await get_account_summary()
            positions = await get_positions(use_live=True)
            await hub.broadcast(
                {
                    "type": "snapshot",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "account": account.get("account"),
                    "bot": account.get("bot"),
                    "positions": positions,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"WS 广播失败: {exc}")
        await asyncio.sleep(5)
