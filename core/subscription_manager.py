"""
Subscription Manager — caches and restores WebSocket channel subscriptions.

After reconnect, all subscriptions are automatically restored from cache.
No manual re-subscription needed. No hardcoded channel lists in WS loops.

Tracks subscription state per channel: pending, confirmed, failed.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable, Optional

from loguru import logger

L = logger.bind(module="subscription")


class SubState(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass
class Subscription:
    channel: str
    inst_type: str = "SWAP"
    inst_family: str = ""
    extra_args: dict = field(default_factory=dict)
    state: SubState = SubState.PENDING
    subscribed_at: float = 0.0


class SubscriptionManager:
    """
    Manages WebSocket channel subscriptions.

    Usage:
        sub_mgr = SubscriptionManager()
        sub_mgr.add("positions", inst_type="SWAP")
        sub_mgr.add("orders", inst_type="SWAP")
        sub_mgr.add("account", inst_type="SWAP")

        # On initial connect or reconnect:
        for sub in sub_mgr.get_pending():
            await ws.send(subscribe_message(sub))
            # On confirmation:
            sub_mgr.confirm(sub.channel)
    """

    def __init__(self, exchange_name: str = "okx"):
        self._exchange_name = exchange_name
        self._subscriptions: dict[str, Subscription] = {}
        self._send_fn: Optional[Callable[[dict], Awaitable[None]]] = None

    def set_send_fn(self, fn: Callable[[dict], Awaitable[None]]) -> None:
        """Set the function to send WS messages."""
        self._send_fn = fn

    def add(self, channel: str, inst_type: str = "SWAP",
            inst_family: str = "", **extra_args) -> None:
        """
        Add a channel subscription. Idempotent — duplicate adds are ignored.
        If the channel already exists and is CONFIRMED, it stays CONFIRMED.
        If PENDING or FAILED, it resets to PENDING for re-subscription.
        """
        if channel in self._subscriptions:
            existing = self._subscriptions[channel]
            if existing.state == SubState.CONFIRMED:
                return  # Already subscribed, don't reset
            existing.state = SubState.PENDING
            L.info(f"[Subscription] {channel}: reset to PENDING for re-subscription")
            return

        self._subscriptions[channel] = Subscription(
            channel=channel,
            inst_type=inst_type,
            inst_family=inst_family,
            extra_args=extra_args,
            state=SubState.PENDING,
        )
        L.info(f"[Subscription] {channel}: added (instType={inst_type})")

    def remove(self, channel: str) -> None:
        """Remove a channel subscription."""
        self._subscriptions.pop(channel, None)
        L.info(f"[Subscription] {channel}: removed")

    def confirm(self, channel: str) -> None:
        """Mark a subscription as confirmed."""
        sub = self._subscriptions.get(channel)
        if sub:
            sub.state = SubState.CONFIRMED
            sub.subscribed_at = time.time()
            L.info(f"[Subscription] {channel}: CONFIRMED")

    def mark_failed(self, channel: str) -> None:
        """Mark a subscription as failed."""
        sub = self._subscriptions.get(channel)
        if sub:
            sub.state = SubState.FAILED
            L.warning(f"[Subscription] {channel}: FAILED")

    def get_pending(self) -> list[Subscription]:
        """Get all subscriptions that need to be (re)subscribed."""
        return [s for s in self._subscriptions.values() if s.state == SubState.PENDING]

    def get_confirmed(self) -> list[Subscription]:
        """Get all confirmed subscriptions."""
        return [s for s in self._subscriptions.values() if s.state == SubState.CONFIRMED]

    def get_all(self) -> list[Subscription]:
        return list(self._subscriptions.values())

    def get_channel_names(self) -> list[str]:
        return list(self._subscriptions.keys())

    def mark_all_pending(self) -> None:
        """Mark all subscriptions as pending (on disconnect)."""
        for sub in self._subscriptions.values():
            if sub.state == SubState.CONFIRMED:
                sub.state = SubState.PENDING
        L.info(f"[Subscription] {len(self._subscriptions)} channels marked PENDING for reconnect")

    def has_channel(self, channel: str) -> bool:
        return channel in self._subscriptions

    async def subscribe_all(self) -> bool:
        """
        Send subscribe messages for all pending channels.
        Returns True if all were sent successfully.
        """
        pending = self.get_pending()
        if not pending:
            L.info(f"[Subscription] No pending subscriptions")
            return True

        if not self._send_fn:
            L.error(f"[Subscription] No send function set")
            return False

        # OKX supports batch subscribe: one message with multiple args
        args = []
        for sub in pending:
            arg = {"channel": sub.channel, "instType": sub.inst_type}
            if sub.inst_family:
                arg["instFamily"] = sub.inst_family
            arg.update(sub.extra_args)
            args.append(arg)

        subscribe_msg = {"op": "subscribe", "args": args}

        try:
            await self._send_fn(subscribe_msg)
            L.info(f"[Subscription] Subscribed to {len(args)} channels: {[s.channel for s in pending]}")
            return True
        except Exception as e:
            L.error(f"[Subscription] Subscribe failed: {e}")
            for sub in pending:
                self.mark_failed(sub.channel)
            return False

    @property
    def channel_count(self) -> int:
        return len(self._subscriptions)

    @property
    def pending_count(self) -> int:
        return len(self.get_pending())

    @property
    def confirmed_count(self) -> int:
        return len(self.get_confirmed())

    def stats(self) -> dict:
        return {
            "channels": self.get_channel_names(),
            "total": self.channel_count,
            "confirmed": self.confirmed_count,
            "pending": self.pending_count,
        }
