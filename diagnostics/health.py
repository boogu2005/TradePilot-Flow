"""Deterministic health classification; business-message silence is not failure."""
from enum import Enum


class HealthStatus(str, Enum):
    NORMAL = "NORMAL"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


def classify(snapshot):
    if snapshot.state == "FAILED":
        return HealthStatus.FAILED
    if not snapshot.ws.connected:
        return HealthStatus.DEGRADED
    if snapshot.ws.last_ping_sent_ago < snapshot.ws.last_pong_ago and snapshot.ws.last_ping_sent_ago > 10:
        return HealthStatus.DEGRADED
    if not snapshot.rest.healthy:
        return HealthStatus.DEGRADED
    if snapshot.trackers.last_rest_sync_ago == float("inf"):
        return HealthStatus.UNKNOWN
    if snapshot.trackers.last_rest_sync_ago > 600:
        return HealthStatus.STALE
    return HealthStatus.NORMAL
