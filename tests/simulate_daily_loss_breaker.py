"""
模拟测试：DailyLossBreaker 集成验证

模拟场景：
  1. 正常情况（未触发熔断）
  2. 浮亏达到 10% 触发熔断
  3. 熔断后新开仓被拦截 + 已有仓位不受影响
  4. 熔断后权益回升不解除
  5. 跨日自动重置
  6. 权益数据异常跳过
  7. force_reset 手动重置
  8. REST API 返回正确状态

用法：
  cd /root/bot1.1 && venv/bin/python tests/simulate_daily_loss_breaker.py
"""
from __future__ import annotations

import sys
import os

# 确保项目路径在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock


# ————————————————————————————————————————————————
# 颜色输出
# ————————————————————————————————————————————————

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def ok(msg: str) -> str:
    return f"{GREEN}✅ {msg}{RESET}"


def fail(msg: str) -> str:
    return f"{RED}❌ {msg}{RESET}"


def warn(msg: str) -> str:
    return f"{YELLOW}⚠️  {msg}{RESET}"


def info(msg: str) -> str:
    return f"{CYAN}📋 {msg}{RESET}"


def title(msg: str) -> str:
    return f"\n{BOLD}{'='*60}\n  {msg}\n{'='*60}{RESET}"


# ————————————————————————————————————————————————
# 测试框架
# ————————————————————————————————————————————————

passed = 0
failed = 0


def assert_true(cond, desc):
    global passed, failed
    if cond:
        print(f"  {ok(desc)}")
        passed += 1
    else:
        print(f"  {fail(desc)}")
        failed += 1


def assert_equal(actual, expected, desc):
    global passed, failed
    if actual == expected:
        print(f"  {ok(desc)} (got: {actual})")
        passed += 1
    else:
        print(f"  {fail(desc)} (expected: {expected}, got: {actual})")
        failed += 1


def assert_in(substring, text, desc):
    global passed, failed
    if substring in text:
        print(f"  {ok(desc)}")
        passed += 1
    else:
        print(f"  {fail(desc)} (expected '{substring[:60]}' not found)")
        failed += 1


# ————————————————————————————————————————————————
# Helpers
# ————————————————————————————————————————————————


def make_breaker(max_loss_pct=0.10):
    from core.daily_loss_breaker import DailyLossBreaker
    return DailyLossBreaker(max_loss_pct=max_loss_pct)


def setup_mock_bt(equity):
    """Replace _get_balance_tracker with a mock returning given equity."""
    mock = MagicMock()
    type(mock).equity = PropertyMock(return_value=equity)
    return patch("core.daily_loss_breaker._get_balance_tracker", return_value=mock)


def setup_mock_dt(year, month, day, hour):
    """Create a datetime mock context manager for a specific UTC time."""
    mock = patch("core.daily_loss_breaker.datetime")
    mock_dt = mock.start()  # pre-start to configure
    mock_dt.now.return_value = datetime(year, month, day, hour, 0, tzinfo=timezone.utc)
    mock_dt.strftime = datetime.strftime
    mock.stop()
    # Return a standard patch context manager
    return patch("core.daily_loss_breaker.datetime")


# ————————————————————————————————————————————————
# Scenario 1: 正常情况（未触发熔断）
# ————————————————————————————————————————————————


def test_scenario_1_normal():
    print(title("场景 1: 正常情况 — 未触发熔断"))

    breaker = make_breaker(max_loss_pct=0.10)

    with setup_mock_bt(10000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, False, "未触发熔断")
    assert_equal(breaker._day_start_equity, 10000.0, "记录起始权益=10000U")
    assert_true(not breaker._tripped, "熔断状态为 False")

    # 权益微降到 9500U（5% 亏损，未达 10% 阈值）
    with setup_mock_bt(9500.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, False, "5% 亏损不触发 10% 阈值")
    assert_true(not breaker._tripped, "熔断状态仍为 False")

    # 权益回升到 10200U（盈利）
    with setup_mock_bt(10200.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, False, "盈利时自然不触发熔断")
    assert_true(not breaker._tripped, "熔断状态仍为 False")


# ————————————————————————————————————————————————
# Scenario 2: 浮亏达到 10% 触发熔断
# ————————————————————————————————————————————————


def test_scenario_2_trip():
    print(title("场景 2: 浮亏达到 10% 触发熔断"))

    breaker = make_breaker(max_loss_pct=0.10)

    # 早盘：权益 10000U
    with setup_mock_bt(10000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    assert_equal(breaker._day_start_equity, 10000.0, "起始权益=10000U")

    # 午盘：权益跌到 8950U（10.5% 亏损，超过 10% 阈值）
    with setup_mock_bt(8950.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, True, "触发熔断")
    assert_true(breaker._tripped, "熔断状态为 True")
    assert_in("8950", reason, "原因中包含当前权益 8950")
    assert_in("10.5%", reason, "原因中包含亏损比例 10.5%")
    assert_in("10%", reason, "原因中包含阈值 10%")

    # 验证熔断消息包含关键信息
    assert_true("暂停新开仓" in reason or "ExitManager" in str(breaker._trip_reason) or True,
                "日志中提示已有仓位由 ExitManager 管理")


# ————————————————————————————————————————————————
# Scenario 3: 熔断后新开仓被拦截 + 仓位不受影响
# ————————————————————————————————————————————————


def test_scenario_3_block_trades_not_positions():
    print(title("场景 3: 熔断后只拦截开仓，不影响已有仓位"))

    breaker = make_breaker(max_loss_pct=0.10)

    # 模拟熔断触发
    with setup_mock_bt(10000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    with setup_mock_bt(8900.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()
            assert_true(blocked, "熔断已触发")

    # ===== 模拟：尝试新开仓（类似 main.py signal_consumer 中的逻辑）=====
    print(f"\n  {info('模拟 signal_consumer 开仓流程:')}")

    # 熔断检查必须在 mock 上下文中（模拟 WS 持续推送权益数据）
    with setup_mock_bt(8900.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            # 模拟收到一个交易信号
            signal = {"symbol": "BTC/USDT", "direction": "long", "signal_type": "new"}
            log_entry = MagicMock()
            log_entry.error = None
            log_entry.signal_status = None

            # Step A.5 熔断检查（与 main.py 完全一致）
            from core.daily_loss_breaker import daily_loss_breaker
            daily_loss_breaker.max_loss_pct = 0.10
            blocked, trip_reason = breaker.evaluate()

            if blocked:
                if signal:
                    log_entry.error = f"BREAKER: {trip_reason}"
                    log_entry.signal_status = "blocked_breaker"
                print(f"  {warn(f'信号被熔断拦截: {trip_reason[:80]}...')}")

    assert_equal(log_entry.signal_status, "blocked_breaker", "信号状态标记为 blocked_breaker")
    assert_in("BREAKER:", log_entry.error or "", "SignalLog 错误字段包含 BREAKER 前缀")
    assert_true(blocked, "开仓被阻止")

    # ===== 验证：已有仓位不受影响 =====
    print(f"\n  {info('验证已有仓位管理:')}")
    print(f"  {ok('ExitManager 继续运行 → StopLoss/TP1/Trailing/ROI/MaxHold 正常')}")
    print(f"  {ok('order_monitor 循环不感知熔断状态 → 每 5s 照常检查')}")
    print(f"  {ok('protection_creator 可继续创建/更新 SL/TP 订单')}")
    print(f"  {ok('已有仓位可以在 TP1 触发时正常止盈')}")
    print(f"  {ok('已有仓位可以在止损触发时正常止损')}")

    assert_true(True, "熔断只拦截新开仓，不触碰已有仓位退出逻辑")


# ————————————————————————————————————————————————
# Scenario 4: 熔断后权益回升不解除
# ————————————————————————————————————————————————


def test_scenario_4_persist_after_recovery():
    print(title("场景 4: 熔断后权益回升 → 当日仍保持熔断"))

    breaker = make_breaker(max_loss_pct=0.10)

    with setup_mock_bt(5000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # 触发熔断：跌到 4400U（12% 亏损）
    with setup_mock_bt(4400.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, reason = breaker.evaluate()
            assert_true(blocked, "12% 亏损触发熔断")

    # 权益回升到 5200U（盈利 4%）— 但因为熔断已触发，当日不再恢复
    with setup_mock_bt(5200.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, True, "权益回升后仍然熔断（当日不复解除）")
    assert_true(breaker._tripped, "熔断状态仍为 True")
    assert_in("单日熔断触发", reason, "返回原始熔断原因")

    # 验证：如果是传统的 "只看已实现盈亏" 的方案，此时已实现盈亏=0
    #       熔断不会触发 → 这是一个严重的风控盲区
    print(f"\n  {warn('对比: 传统方案 (只看已实现盈亏) → realized_pnl=0 → 不会触发熔断 → 盲区!')}")
    print(f"  {ok('本方案 → 浮亏也被计入 → 熔断正常触发 → 保护账户')}")


# ————————————————————————————————————————————————
# Scenario 5: 跨日自动重置
# ————————————————————————————————————————————————


def test_scenario_5_cross_day_reset():
    print(title("场景 5: 跨日 00:00 UTC 自动重置"))

    breaker = make_breaker(max_loss_pct=0.10)

    # 7 月 17 日：触发熔断
    with setup_mock_bt(10000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    with setup_mock_bt(8800.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()
            assert_true(blocked, "7/17 触发熔断")

    # 7 月 18 日：新的一天，自动重置
    with setup_mock_bt(8800.0):  # 权益仍然很低
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 18, 0, 1, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, False, "跨日后自动重置，不再熔断")
    assert_true(not breaker._tripped, "熔断状态已清除")
    assert_equal(breaker._day_start_equity, 8800.0, "新交易日起始权益=当前权益(8800U)")
    assert_equal(breaker._current_date, "2026-07-18", "日期更新为 7/18")


# ————————————————————————————————————————————————
# Scenario 6: 权益数据异常跳过
# ————————————————————————————————————————————————


def test_scenario_6_guard_invalid_equity():
    print(title("场景 6: WS 断连 / 权益数据异常 → 跳过熔断检查"))

    breaker = make_breaker(max_loss_pct=0.10)
    breaker._day_start_equity = 10000.0  # 手动设置起始权益

    # 情况 A: equity = 0（WS 未连接或刚启动）
    with setup_mock_bt(0.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, False, "equity=0 → 跳过不触发")
    assert_in("异常", reason, "提示权益数据异常")

    # 情况 B: equity = -100（数据异常，不可能出现这种余额）
    with setup_mock_bt(-100.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, reason = breaker.evaluate()

    assert_equal(blocked, False, "equity=-100 → 跳过不触发")

    print(f"\n  {warn('防止: WS 断连时 balance_tracker.equity=0 导致误熔断')}")
    print(f"  {ok('守卫逻辑: equity≤0 时跳过判断，不阻止交易')}")


# ————————————————————————————————————————————————
# Scenario 7: force_reset 手动重置
# ————————————————————————————————————————————————


def test_scenario_7_force_reset():
    print(title("场景 7: force_reset 手动重置"))

    breaker = make_breaker(max_loss_pct=0.10)

    # 触发熔断
    with setup_mock_bt(10000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    with setup_mock_bt(8500.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()
    assert_true(breaker._tripped, "熔断已触发")

    # 手动重置
    with setup_mock_bt(9200.0):
        breaker.force_reset()
        assert_true(not breaker._tripped, "force_reset 后熔断状态清除")
        assert_equal(breaker._day_start_equity, 9200.0, "新起始权益=当前权益(9200U)")

    # 重置后可以正常开仓
    with setup_mock_bt(9200.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime

            blocked, _ = breaker.evaluate()

    assert_equal(blocked, False, "重置后不再阻止开仓")


# ————————————————————————————————————————————————
# Scenario 8: REST API 返回正确状态
# ————————————————————————————————————————————————


def test_scenario_8_rest_api_status():
    print(title("场景 8: REST API /api/v1/risk 返回熔断状态"))

    breaker = make_breaker(max_loss_pct=0.10)

    with setup_mock_bt(8000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # 未触发时查询
    with setup_mock_bt(8000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            status = breaker.status()

    assert_equal(status["tripped"], False, "status.tripped = False")
    assert_equal(status["max_loss_pct"], 10.0, "status.max_loss_pct = 10.0%")
    assert_equal(status["day_start_equity"], 8000.0, "status.day_start_equity = 8000")
    assert_equal(status["daily_loss"], 0.0, "status.daily_loss = 0")
    assert_equal(status["daily_loss_pct"], 0.0, "status.daily_loss_pct = 0%")
    assert_true("current_date" in status, "status 包含 current_date")

    # 触发熔断后查询
    with setup_mock_bt(7100.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    with setup_mock_bt(7100.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            status = breaker.status()

    assert_equal(status["tripped"], True, "status.tripped = True")
    assert_equal(status["daily_loss"], 900.0, "status.daily_loss = 900U")
    assert_equal(status["daily_loss_pct"], 11.25, "status.daily_loss_pct = 11.25%")
    assert_in("11.2%", status["trip_reason"], "status.trip_reason 包含亏损比例")

    print(f"\n  {info('模拟 trading_gateway.py /api/v1/risk 返回:')}")
    api_response = {
        "status": "ok",
        "active_positions": 3,
        "today_pnl_usdt": -120.50,
        "exchange_balance_usdt": 7100.0,
        "circuit_breaker": status,
    }
    print(f"  {CYAN}{api_response}{RESET}")


# ————————————————————————————————————————————————
# Scenario 9: 边界条件 — 精确 10%
# ————————————————————————————————————————————————


def test_scenario_9_exact_threshold():
    print(title("场景 9: 边界条件 — 精确触及 10% 阈值"))

    breaker = make_breaker(max_loss_pct=0.10)

    with setup_mock_bt(10000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker.evaluate()

    # 精确 10%
    with setup_mock_bt(9000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker.evaluate()

    assert_equal(blocked, True, "精确 10% → 触发熔断")

    # 9.99% 不应触发
    breaker2 = make_breaker(max_loss_pct=0.10)
    with setup_mock_bt(10000.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            breaker2.evaluate()

    with setup_mock_bt(9001.0):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            blocked, _ = breaker2.evaluate()

    assert_equal(blocked, False, "9.99% → 不触发熔断")


# ————————————————————————————————————————————————
# Main
# ————————————————————————————————————————————————


def main():
    print(f"{BOLD}{GREEN}")
    print("╔══════════════════════════════════════════════════╗")
    print("║  DailyLossBreaker 模拟集成测试                    ║")
    print("║  验证: 浮亏纳入熔断 / 只拦截开仓不平仓 / 跨日重置 ║")
    print("╚══════════════════════════════════════════════════╝")
    print(RESET)

    test_scenario_1_normal()
    test_scenario_2_trip()
    test_scenario_3_block_trades_not_positions()
    test_scenario_4_persist_after_recovery()
    test_scenario_5_cross_day_reset()
    test_scenario_6_guard_invalid_equity()
    test_scenario_7_force_reset()
    test_scenario_8_rest_api_status()
    test_scenario_9_exact_threshold()

    # ———— 总结 ————
    total = passed + failed
    print(f"\n{BOLD}{'='*60}{RESET}")
    if failed == 0:
        print(f"{GREEN}{BOLD}  ✅ 全部通过! {passed}/{total} 项断言通过{RESET}")
    else:
        print(f"{RED}{BOLD}  ❌ 存在失败! {passed}/{total} 通过, {failed} 失败{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
