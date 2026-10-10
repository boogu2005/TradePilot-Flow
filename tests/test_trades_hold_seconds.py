"""历史交易页面的持仓时长计算。"""

from datetime import datetime, timezone

from backend.services.trades import _hold_seconds


def test_hold_seconds_accepts_mixed_naive_and_aware_utc_times():
    open_time = datetime(2026, 10, 10, 12, 0, 0)
    close_time = datetime(2026, 10, 10, 12, 1, 30, tzinfo=timezone.utc)

    assert _hold_seconds(open_time, close_time) == 90
