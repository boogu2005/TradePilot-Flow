"""
Unit tests for DailyLossBreaker.

Tests equity-based daily max loss circuit breaker:
  - Cross-day auto-reset at 00:00 UTC
  - Trip when daily loss reaches threshold (unrealized PnL included)
  - Guard: skip when equity data is invalid (≤0)
  - Within-day persistence: once tripped, stays tripped until next day
  - Manual force_reset
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ————————————————————————————————————————————————
# Helpers
# ————————————————————————————————————————————————

def _make_breaker(max_loss_pct: float = 0.10):
    """Create a fresh DailyLossBreaker instance."""
    from core.daily_loss_breaker import DailyLossBreaker
    return DailyLossBreaker(max_loss_pct=max_loss_pct)


def _mock_bt(equity: float = 1000.0):
    """Create a mock balance_tracker with the given equity."""
    mock = MagicMock()
    type(mock).equity = PropertyMock(return_value=equity)
    return mock


def _bt_patch(equity: float = 1000.0):
    """Context manager that patches _get_balance_tracker with mocked equity."""
    mock = _mock_bt(equity)
    return patch("core.daily_loss_breaker._get_balance_tracker", return_value=mock)


# ————————————————————————————————————————————————
# Cross-day reset
# ————————————————————————————————————————————————

def test_day_reset_resets_tripped_state():
    """Tripped breaker should auto-reset when date changes."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        # Day 1: normal operation
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, _ = breaker.evaluate()
            assert not blocked
            # Force trip
            breaker._tripped = True
            breaker._trip_reason = "test trip"

        # Day 2: should auto-reset
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()
            assert not blocked, f"Should reset on new day, got: {reason}"
            assert not breaker._tripped


def test_day_reset_captures_new_equity():
    """Start-of-day equity should be captured on date change."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(900.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()
            assert breaker._day_start_equity == 900.0

    # New day with different equity
    with _bt_patch(950.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()
            assert breaker._day_start_equity == 950.0


# ————————————————————————————————————————————————
# Trip on unrealized loss
# ————————————————————————————————————————————————

def test_trips_when_equity_drops_below_threshold():
    """Breaker should trip when equity drops >= max_loss_pct from day start."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()  # records start equity = 1000
            assert not breaker._tripped

    # Equity drops to 895U (10.5% loss — includes unrealized)
    with _bt_patch(895.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, reason = breaker.evaluate()

            assert blocked, f"Should trip at 10.5% loss, got: {reason}"
            assert "895" in reason
            assert "10.5" in reason


def test_does_not_trip_below_threshold():
    """Breaker should NOT trip when equity drop is less than max_loss_pct."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # Drop to 910U (9% loss — below 10% threshold)
    with _bt_patch(910.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()

            assert not blocked, "Should NOT trip at 9% loss"


def test_trips_exactly_at_threshold():
    """Breaker should trip when equity drop equals max_loss_pct exactly."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # Drop to 900U (exactly 10% loss)
    with _bt_patch(900.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, reason = breaker.evaluate()

            assert blocked, f"Should trip at exactly 10%, got: {reason}"


# ————————————————————————————————————————————————
# Guard: invalid equity data
# ————————————————————————————————————————————————

def test_skips_when_equity_is_zero():
    """Should skip breaker check (not trip) when equity is 0."""
    breaker = _make_breaker(max_loss_pct=0.10)
    breaker._day_start_equity = 1000.0

    with _bt_patch(0.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, reason = breaker.evaluate()

            assert not blocked, f"Should skip when equity=0, got blocked with: {reason}"
            assert "异常" in reason


def test_skips_when_equity_is_negative():
    """Should skip breaker check when equity is negative (data error)."""
    breaker = _make_breaker(max_loss_pct=0.10)
    breaker._day_start_equity = 1000.0

    with _bt_patch(-100.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, reason = breaker.evaluate()

            assert not blocked, f"Should skip when equity<0, got blocked with: {reason}"
            assert "异常" in reason


def test_initializes_start_equity_on_first_evaluate():
    """First evaluate() call should record start equity (via day reset) and pass."""
    breaker = _make_breaker(max_loss_pct=0.10)
    assert breaker._day_start_equity == 0.0

    with _bt_patch(5000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, reason = breaker.evaluate()

            assert not blocked, f"First eval should just init, got: {reason}"
            # _day_start_equity is set during _check_day_reset (day change detected)
            assert breaker._day_start_equity == 5000.0


# ————————————————————————————————————————————————
# Within-day persistence
# ————————————————————————————————————————————————

def test_stays_tripped_within_same_day():
    """Once tripped, breaker stays tripped for the rest of the day."""
    breaker = _make_breaker(max_loss_pct=0.05)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # Trip at -6%
    with _bt_patch(940.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()
            assert blocked
            assert breaker._tripped

    # Equity recovers to 990U — but should still be tripped
    with _bt_patch(990.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()
            assert blocked, "Should stay tripped even after equity recovery"


# ————————————————————————————————————————————————
# force_reset
# ————————————————————————————————————————————————

def test_force_reset_clears_trip():
    """force_reset() should clear tripped state and reset start equity."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    with _bt_patch(890.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()
        assert breaker._tripped

    # Force reset with equity=950
    with _bt_patch(950.0):
        breaker.force_reset()

        assert not breaker._tripped
        assert breaker._day_start_equity == 950.0
        assert breaker._trip_reason == ""


def test_force_reset_allows_new_entries():
    """After force_reset, evaluate() should return unblocked."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    with _bt_patch(890.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()
        assert breaker._tripped

    with _bt_patch(950.0):
        breaker.force_reset()

    with _bt_patch(950.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 13, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()
            assert not blocked


# ————————————————————————————————————————————————
# status() method
# ————————————————————————————————————————————————

def test_status_returns_correct_structure():
    """status() should return a dict with all expected keys."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

        status = breaker.status()
        assert "tripped" in status
        assert "day_start_equity" in status
        assert "current_equity_ws" in status
        assert "current_equity_rest" in status
        assert "daily_loss" in status
        assert "daily_loss_pct" in status
        assert "max_loss_pct" in status
        assert "trip_reason" in status
        assert "current_date" in status
        assert status["tripped"] is False
        assert status["day_start_equity"] == 1000.0
        assert status["max_loss_pct"] == 10.0


def test_status_shows_trip_when_tripped():
    """status() should reflect tripped state correctly."""
    breaker = _make_breaker(max_loss_pct=0.05)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # Use the same datetime mock so status() doesn't see a "new day"
    with _bt_patch(940.0), patch("core.daily_loss_breaker.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
        mock_dt.strftime = datetime.strftime
        blocked, _ = breaker.evaluate()
        assert blocked

    with _bt_patch(940.0), patch("core.daily_loss_breaker.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
        mock_dt.strftime = datetime.strftime
        status = breaker.status()
        assert status["tripped"] is True
        assert status["daily_loss"] == 60.0
        assert status["daily_loss_pct"] == 6.0


# ————————————————————————————————————————————————
# is_tripped property
# ————————————————————————————————————————————————

def test_is_tripped_property():
    """is_tripped property should reflect state without side effects."""
    breaker = _make_breaker(max_loss_pct=0.10)

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            assert breaker.is_tripped is False
            breaker._tripped = True
            assert breaker.is_tripped is True


# ————————————————————————————————————————————————
# Configurable threshold
# ————————————————————————————————————————————————

def test_custom_threshold():
    """max_loss_pct should be configurable."""
    breaker = _make_breaker(max_loss_pct=0.03)  # 3%

    with _bt_patch(1000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # 5% loss — should trip with 3% threshold
    with _bt_patch(950.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()
            assert blocked, "Should trip at 5% loss with 3% threshold"
