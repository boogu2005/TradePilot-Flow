"""
时区处理单元测试

测试目标：
1. ensure_utc() — 统一时间转换函数
2. utc_now() — 当前时间生成
3. dt_seconds_ago() — 时间差计算
4. 集成测试：SQLite naive datetime vs aware datetime 比较
5. reconciler _is_entry_order_stale 时间比较

背景：
SQLAlchemy + SQLite 存储的 DateTime 字段读取后无 tzinfo（naive）。
代码中大量使用 datetime.now(timezone.utc) 生成 aware datetime。
直接比较会抛出 TypeError: can't compare offset-naive and offset-aware datetimes。
"""
import pytest
from datetime import datetime, timezone, timedelta
from utils.time import ensure_utc, utc_now, dt_seconds_ago


class TestEnsureUtc:
    """测试 ensure_utc 工具函数"""

    def test_none_returns_none(self):
        """None → None"""
        assert ensure_utc(None) is None

    def test_naive_datetime_gets_utc(self):
        """naive datetime → 添加 timezone.utc"""
        dt = datetime(2026, 7, 13, 12, 0, 0)
        result = ensure_utc(dt)
        assert result.tzinfo is timezone.utc
        assert result.hour == 12
        assert result.minute == 0

    def test_utc_aware_datetime_unchanged(self):
        """UTC aware datetime → 不变"""
        dt = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
        result = ensure_utc(dt)
        assert result.tzinfo is timezone.utc
        assert result == dt

    def test_non_utc_aware_converted_to_utc(self):
        """非 UTC aware datetime → 转换到 UTC"""
        # UTC+8 = 北京时间 20:00 → UTC 12:00
        tz_cn = timezone(timedelta(hours=8))
        dt = datetime(2026, 7, 13, 20, 0, 0, tzinfo=tz_cn)
        result = ensure_utc(dt)
        assert result.tzinfo is timezone.utc
        assert result.hour == 12  # 20:00 CST = 12:00 UTC

    def test_naive_vs_aware_comparison_no_crash(self):
        """
        核心测试：naive 和 aware 比较不应抛出 TypeError。
        模拟 DB 读取的 naive datetime 与 aware datetime 比较。
        """
        db_naive = datetime(2026, 7, 13, 12, 0, 0)  # 模拟 SQLite 读取
        now_aware = datetime(2026, 7, 13, 12, 0, 1, tzinfo=timezone.utc)

        db_utc = ensure_utc(db_naive)
        # 不会抛出 TypeError
        assert db_utc < now_aware


class TestUtcNow:
    """测试 utc_now 工具函数"""

    def test_returns_aware_datetime(self):
        """返回的 datetime 应该带 timezone.utc"""
        now = utc_now()
        assert now.tzinfo is timezone.utc

    def test_recent_time(self):
        """应该在合理的时间范围内"""
        now = utc_now()
        ref = datetime.now(timezone.utc)
        diff = abs((ref - now).total_seconds())
        assert diff < 5  # 5秒内


class TestDtSecondsAgo:
    """测试 dt_seconds_ago 工具函数"""

    def test_none_returns_none(self):
        """None → None"""
        assert dt_seconds_ago(None) is None

    def test_naive_datetime(self):
        """naive datetime 应该正确计算秒数"""
        dt = datetime.now(timezone.utc) - timedelta(seconds=60)
        naive = dt.replace(tzinfo=None)  # 模拟 SQLite 读取
        elapsed = dt_seconds_ago(naive)
        assert elapsed is not None
        assert 55 < elapsed < 65  # 60秒左右

    def test_aware_datetime(self):
        """aware datetime 应该正确计算秒数"""
        dt = datetime.now(timezone.utc) - timedelta(seconds=120)
        elapsed = dt_seconds_ago(dt)
        assert elapsed is not None
        assert 115 < elapsed < 125


class TestIntegration:
    """
    集成测试：模拟实际使用场景。

    SQLite → Python datetime 无时区 → ensure_utc 转换 → 比较
    """

    @pytest.fixture
    def mock_db_trade(self):
        """模拟从 SQLite Trade 表读取的时间字段"""
        class MockTrade:
            def __init__(self):
                # SQLAlchemy+SQLite 读取的 DateTime 无 tzinfo
                self.open_date = datetime(2026, 7, 13, 10, 0, 0)
                self.updated_at = datetime(2026, 7, 13, 10, 30, 0)
                self.close_date = None
        return MockTrade()

    def test_trade_age_check(self, mock_db_trade):
        """
        模拟 Trade 持仓时间检查。
        场景: 判断 Trade 是否超过 30 秒。
        """
        open_date = ensure_utc(mock_db_trade.open_date)
        now = datetime.now(timezone.utc)

        # 不会抛出 TypeError
        elapsed = (now - open_date).total_seconds()
        assert elapsed > 0

    def test_snapshot_expiry_check(self, mock_db_trade):
        """
        模拟 Snapshot 过期判断。
        场景: 判断 updated_at 是否超过 60 秒。
        """
        updated_at = ensure_utc(mock_db_trade.updated_at)
        now = datetime.now(timezone.utc)

        # 不会抛出 TypeError（尽管 mock 时间较旧，比较不会崩溃）
        elapsed = (now - updated_at).total_seconds()
        assert elapsed > 0  # 至少是正数，不会 TypeError

    def test_close_date_is_none(self, mock_db_trade):
        """close_date 为 None 时不应报错"""
        close_date = ensure_utc(mock_db_trade.close_date)
        assert close_date is None

    def test_naive_aware_list_sort(self):
        """
        模拟协调器中需要对多个 Trade 按时间排序。
        """
        trades = [
            (datetime(2026, 7, 13, 9, 0, 0), "trade_a"),          # naive
            (datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc), "trade_b"),  # aware
            (datetime(2026, 7, 13, 8, 0, 0), "trade_c"),          # naive
        ]
        # 统一转换后排序
        sorted_trades = sorted(
            [(ensure_utc(dt), name) for dt, name in trades],
            key=lambda x: x[0],
        )
        assert sorted_trades[0][1] == "trade_c"  # 最早
        assert sorted_trades[1][1] == "trade_a"
        assert sorted_trades[2][1] == "trade_b"  # 最晚

    def test_reconciler_stale_check_simulation(self):
        """
        模拟 reconciler._is_entry_order_stale 的时间比较逻辑。
        """
        now = datetime.now(timezone.utc)

        # 模拟 DB 读取的 order.expire_at（naive，已过期）
        expire_at = ensure_utc(datetime(2026, 7, 10, 12, 0, 0))
        assert now > expire_at  # 不会崩溃

        # 模拟 DB 读取的 order.order_date + 72h（naive，已过期）
        order_date = ensure_utc(datetime(2026, 7, 10, 12, 0, 0))
        expire_time = order_date + timedelta(hours=72)
        assert now > expire_time  # 不会崩溃

        # 模拟未过期
        recent_date = ensure_utc(datetime.now(timezone.utc) - timedelta(hours=1))
        expire_time2 = recent_date + timedelta(hours=72)
        assert now < expire_time2  # 不会崩溃

    def test_ensure_utc_idempotent(self):
        """ensure_utc 对 UTC aware datetime 是幂等的"""
        dt = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
        result1 = ensure_utc(dt)
        result2 = ensure_utc(result1)
        assert result1 == result2
        assert result2.tzinfo is timezone.utc


class TestRealWorldScenarios:
    """真实场景测试"""

    def test_sqlite_roundtrip_comparison(self):
        """
        SQLite 读写模拟：写入 aware datetime，读取 naive，
        用 ensure_utc 转换后再比较。
        """
        # 写入时（aware）
        stored_value = datetime(2026, 7, 13, 12, 30, 0, tzinfo=timezone.utc)

        # SQLite 存储后再读取（naive，不带 tzinfo）
        read_value = stored_value.replace(tzinfo=None)

        # 此时直接比较会 TypeError：
        # datetime.now(timezone.utc) > read_value  => TypeError

        # 正确做法：
        normalized = ensure_utc(read_value)
        now = datetime.now(timezone.utc)

        # 不会 TypeError
        assert now > normalized
        assert normalized.tzinfo is timezone.utc
        # 时间点一致
        assert normalized == stored_value

    def test_mixed_timezone_in_list(self):
        """
        列表中混合 naive 和 aware datetimes 的处理。
        例如从多个来源收集时间戳（DB、API、WS）。
        """
        timestamps = [
            datetime(2026, 7, 13, 10, 0, 0),                     # DB (naive)
            datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc), # API (aware UTC)
            None,
            datetime(2026, 7, 13, 18, 0, 0, tzinfo=timezone(timedelta(hours=8))),  # CST
        ]

        # 统一转换，过滤 None
        normalized = [ensure_utc(t) for t in timestamps if t is not None]

        # 所有都应该有 UTC tzinfo
        for dt in normalized:
            assert dt.tzinfo is timezone.utc, f"{dt} 不是 UTC"

        # CST 18:00 = UTC 10:00，全部统一后应该 10:00 × 2
        utc_hours = [dt.hour for dt in normalized]
        assert utc_hours.count(10) == 3  # 三个时间都是 UTC 10:00
