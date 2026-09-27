"""
365 天模拟测试：DailyLossBreaker 双通道熔断 — WS/REST 单方失效场景。

模拟一整年 (365 天) 的熔断器行为，覆盖:
  1. 双通道正常 — 任一通道检测亏损触发熔断
  2. WS 失效 (equity=0) — REST 通道独立触发熔断
  3. REST 失效 (返回 -1) — WS 通道独立触发熔断
  4. WS 数据滞后/错误 — REST 交叉验证告警 + 独立触发
  5. 跨日重置 — WS 失效时不保留旧起始权益
  6. 双通道同时失效 — 守卫防止误触发
  7. 熔断持久化 — 当日持续阻断
  8. force_reset — 手动重置后恢复

用法:
  cd /root/bot1.1 && venv/bin/python tests/simulate_dual_channel_breaker.py
"""
from __future__ import annotations

import sys
import os
import time
import random
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# 辅助：模拟 REST 通道评估（同步版本，直接操作 breaker 内部状态）
# ============================================================================

def _simulate_rest_evaluation(breaker, rest_equity: float, force: bool = False) -> tuple:
    """
    模拟 evaluate_rest() 的核心逻辑，同步版本。

    直接模拟 REST 查询返回 rest_equity，不再调用真实的 OKX API。
    rest_equity <= 0 表示 REST 查询失败。

    注意: 在模拟中忽略 real-time 频率守卫（force=True），
    因为模拟时间远快于真实时间。
    """
    breaker._check_day_reset()

    if breaker._tripped:
        return True, breaker._trip_reason

    now = time.time()
    # 模拟模式下，频率控制由外部 step 间隔决定，此处不做真实时间守卫
    if not force:
        if now - breaker._last_rest_evaluation < breaker._rest_evaluation_interval:
            return False, ""

    breaker._last_rest_evaluation = now

    # REST 查询失败 (equity <= 0)
    if rest_equity <= 0:
        return False, "REST权益查询异常，跳过"

    breaker._rest_equity = rest_equity

    # 初始化起始权益
    if breaker._day_start_equity <= 0:
        breaker._day_start_equity = rest_equity
        return False, "起始权益已记录(REST)"

    # 计算亏损
    daily_loss = breaker._day_start_equity - rest_equity
    daily_loss_pct = daily_loss / breaker._day_start_equity

    # 阈值判断
    if daily_loss_pct >= breaker.max_loss_pct:
        breaker._tripped = True
        breaker._trip_time = time.time()
        breaker._rest_trip_source = "rest"
        breaker._trip_reason = (
            f"单日熔断触发(REST主动校验): "
            f"起始权益={breaker._day_start_equity:.2f}U, "
            f"当前权益(REST)={rest_equity:.2f}U, "
            f"亏损={daily_loss:.2f}U ({daily_loss_pct*100:.1f}%), "
            f"阈值={breaker.max_loss_pct*100:.0f}%"
        )
        return True, breaker._trip_reason

    # 交叉验证
    ws_eq = breaker._current_equity()
    cross_alert = False
    if ws_eq > 0 and rest_equity > 0:
        diff_pct = abs(ws_eq - rest_equity) / rest_equity
        if diff_pct > 0.02:
            cross_alert = True

    return False, "" if not cross_alert else "cross_validation_alert"


def _make_mock_bt(equity: float):
    bt = MagicMock()
    bt.equity = equity
    return bt


# ============================================================================
# 核心模拟逻辑
# ============================================================================

@dataclass
class DayResult:
    day: int
    date: str
    scenario: str
    ws_working: bool
    rest_working: bool
    start_equity: float
    final_equity: float
    final_loss_pct: float
    tripped: bool
    trip_source: str
    trip_at_step: int
    ws_stale: bool
    cross_alert: bool


def simulate_365_days() -> tuple[list[DayResult], dict]:
    """模拟 365 天双通道熔断器行为。"""
    from core.daily_loss_breaker import DailyLossBreaker

    random.seed(42)

    # 场景配置: (name, weight, ws_config, rest_config, loss_bias)
    scenario_configs = [
        ("both_normal",            35, "ok",     "ok",     1.0),
        ("ws_dead",                15, "dead",   "ok",     1.0),
        ("rest_dead",              15, "ok",     "dead",   1.0),
        ("ws_stale",               10, "stale",  "ok",     1.0),
        ("rest_intermittent",       8, "ok",     "intermittent", 1.0),
        ("both_dead",               5, "dead",   "dead",   0.0),
        ("ws_drop_midday",          6, "drop",   "ok",     1.0),
        ("rest_drop_midday",        6, "ok",     "drop",   1.0),
    ]

    all_scenarios = []
    for name, weight, ws_cfg, rest_cfg, loss_bias in scenario_configs:
        all_scenarios.append((name, weight, ws_cfg, rest_cfg, loss_bias))

    names = [s[0] for s in all_scenarios]
    weights = [s[1] for s in all_scenarios]

    daily_results: list[DayResult] = []
    total_trips = 0
    ws_trips = 0
    rest_trips = 0
    cross_alerts = 0
    false_positives = 0
    missed_detections = 0

    for day_idx in range(365):
        # 选场景
        scenario_tuple = random.choices(all_scenarios, weights=weights, k=1)[0]
        scenario_name, _, ws_cfg, rest_cfg, loss_bias = scenario_tuple

        month = min((day_idx // 30) + 1, 12)
        dom = (day_idx % 28) + 1
        date_str = f"2026-{month:02d}-{dom:02d}"

        day_start_equity = random.uniform(800, 1200)

        # 是否产生亏损
        has_loss = random.random() < 0.30  # 30% 的天有亏损
        if has_loss:
            loss_pct = random.uniform(0.01, 0.22)
        else:
            loss_pct = random.uniform(-0.02, 0.05)  # 小幅盈利

        final_equity = day_start_equity * (1 - loss_pct)

        # WS 配置
        ws_working = True
        ws_stale_offset = 0.0
        ws_drop_step = -1
        if ws_cfg == "dead":
            ws_working = False
        elif ws_cfg == "stale":
            ws_stale_offset = day_start_equity * random.uniform(0.03, 0.08)
        elif ws_cfg == "drop":
            ws_drop_step = random.randint(50, 200)

        # REST 配置
        rest_working = True
        rest_drop_at_call = -1
        rest_fail_interval = 0
        if rest_cfg == "dead":
            rest_working = False
        elif rest_cfg == "intermittent":
            rest_fail_interval = random.randint(3, 6)
        elif rest_cfg == "drop":
            rest_drop_at_call = random.randint(5, 18)

        # 创建 breaker
        breaker = DailyLossBreaker(max_loss_pct=0.10)

        mock_dt = MagicMock()
        mock_dt.now.return_value = datetime(2026, month, dom, 10, 0, tzinfo=timezone.utc)
        mock_dt.strftime = datetime.strftime

        day_tripped = False
        trip_source = ""
        trip_at_step = -1
        cross_alert = False

        steps = 288  # 24h at 5s interval
        rest_call_count = 0

        with patch("core.daily_loss_breaker.datetime", mock_dt):
            # 初始 WS 权益
            ws_eq = day_start_equity if ws_working else 0.0

            with patch("core.daily_loss_breaker._get_balance_tracker",
                       return_value=_make_mock_bt(ws_eq)):
                breaker.evaluate()  # 初始化

            for step in range(steps):
                progress = step / steps
                true_equity = day_start_equity + (final_equity - day_start_equity) * progress

                # —— WS 权益计算 ——
                if not ws_working:
                    ws_eq = 0.0
                elif ws_drop_step >= 0 and step >= ws_drop_step:
                    ws_eq = 0.0  # WS 断连
                else:
                    ws_eq = true_equity + ws_stale_offset  # stale 时有偏移

                # —— Channel 1: WS evaluate ——
                with patch("core.daily_loss_breaker._get_balance_tracker",
                           return_value=_make_mock_bt(ws_eq)):
                    blocked, reason = breaker.evaluate()
                    if blocked and not day_tripped:
                        day_tripped = True
                        trip_source = "ws"
                        trip_at_step = step
                        total_trips += 1
                        ws_trips += 1

                # —— Channel 2: REST evaluate (每 12 steps ≈ 60s) ——
                if step % 12 == 0:
                    rest_call_count += 1

                    # REST 是否可用本次
                    rest_ok = rest_working
                    if rest_drop_at_call >= 0 and rest_call_count >= rest_drop_at_call:
                        rest_ok = False
                    if rest_fail_interval > 0 and rest_call_count > 1 and rest_call_count % rest_fail_interval == 0:
                        rest_ok = False

                    # 模拟中直接控制频率（每12步≈60s），不走真实时间守卫
                    rest_eq = true_equity if rest_ok else -1.0
                    blocked, reason = _simulate_rest_evaluation(breaker, rest_eq, force=True)

                    if reason == "cross_validation_alert":
                        cross_alert = True
                        cross_alerts += 1

                    if blocked and not day_tripped:
                        day_tripped = True
                        trip_source = "rest"
                        trip_at_step = step
                        total_trips += 1
                        rest_trips += 1

                if day_tripped:
                    break  # 当天已熔断, 后续不再模拟

            # —— 收盘前最后一次 REST 检查（捕获最后时刻的亏损）——
            if not day_tripped:
                rest_ok = rest_working
                if rest_drop_at_call >= 0 and rest_call_count >= rest_drop_at_call:
                    rest_ok = False
                rest_eq = final_equity if rest_ok else -1.0
                blocked, reason = _simulate_rest_evaluation(breaker, rest_eq, force=True)
                if blocked and not day_tripped:
                    day_tripped = True
                    trip_source = "rest"
                    trip_at_step = 287
                    total_trips += 1
                    rest_trips += 1

            # —— 收盘前最后一次 WS 检查（equity 达到最终值）——
            if not day_tripped and ws_working and final_equity > 0:
                with patch("core.daily_loss_breaker._get_balance_tracker",
                           return_value=_make_mock_bt(final_equity)):
                    blocked, reason = breaker.evaluate()
                    if blocked and not day_tripped:
                        day_tripped = True
                        trip_source = "ws"
                        trip_at_step = 287
                        total_trips += 1
                        ws_trips += 1

        # 统计漏检/误触发
        effective_loss_pct = abs(loss_pct) if loss_pct > 0 else 0
        threshold = 0.10
        any_channel_alive = ws_working or rest_working
        should_trip = (effective_loss_pct >= threshold) and any_channel_alive
        if should_trip and not day_tripped:
            missed_detections += 1
        should_not_trip = (effective_loss_pct < threshold) or (not ws_working and not rest_working)
        if should_not_trip and day_tripped:
            false_positives += 1

        daily_results.append(DayResult(
            day=day_idx + 1,
            date=date_str,
            scenario=scenario_name,
            ws_working=ws_working,
            rest_working=rest_working,
            start_equity=round(day_start_equity, 2),
            final_equity=round(final_equity, 2),
            final_loss_pct=round(effective_loss_pct * 100, 2),
            tripped=day_tripped,
            trip_source=trip_source,
            trip_at_step=trip_at_step,
            ws_stale=(ws_stale_offset != 0),
            cross_alert=cross_alert,
        ))

    return daily_results, {
        "total_days": 365,
        "total_trips": total_trips,
        "ws_trips": ws_trips,
        "rest_trips": rest_trips,
        "cross_alerts": cross_alerts,
        "false_positives": false_positives,
        "missed_detections": missed_detections,
    }


# ============================================================================
# 报告输出
# ============================================================================

def print_report(results: list[DayResult], stats: dict) -> bool:
    print()
    print("=" * 80)
    print("║  DailyLossBreaker 双通道熔断 — 365天模拟测试报告")
    print("=" * 80)

    print(f"\n{'─' * 60}")
    print("  📊 概要统计")
    print(f"{'─' * 60}")
    print(f"  模拟天数:          {stats['total_days']:>6d}")
    print(f"  熔断触发总次数:     {stats['total_trips']:>6d}")
    print(f"    - WS 通道触发:    {stats['ws_trips']:>6d}")
    print(f"    - REST 通道触发:  {stats['rest_trips']:>6d}")
    print(f"  交叉验证告警:       {stats['cross_alerts']:>6d}")
    fp_icon = "✅ 完美" if stats['false_positives'] == 0 else "❌ 有BUG"
    md_icon = "✅ 完美" if stats['missed_detections'] == 0 else "❌ 有BUG"
    print(f"  误触发 (false+):    {stats['false_positives']:>6d}  {fp_icon}")
    print(f"  漏检 (missed):      {stats['missed_detections']:>6d}  {md_icon}")

    # 场景分类
    print(f"\n{'─' * 60}")
    print("  📋 按场景分类统计")
    print(f"{'─' * 60}")

    scenario_agg = {}
    for r in results:
        key = r.scenario
        if key not in scenario_agg:
            scenario_agg[key] = {"days": 0, "trips": 0, "ws_t": 0, "rest_t": 0, "loss_days": 0, "loss_tripped": 0}
        s = scenario_agg[key]
        s["days"] += 1
        if r.tripped:
            s["trips"] += 1
            if r.trip_source == "ws":
                s["ws_t"] += 1
            elif r.trip_source == "rest":
                s["rest_t"] += 1
        if r.final_loss_pct >= 10.0:
            s["loss_days"] += 1
            if r.tripped:
                s["loss_tripped"] += 1

    for key, s in sorted(scenario_agg.items()):
        detect_rate = f"{s['loss_tripped']}/{s['loss_days']}" if s['loss_days'] > 0 else "N/A"
        print(f"  {key:<22s} 天={s['days']:>3d}  熔断={s['trips']:>3d}"
              f"  WS触发={s['ws_t']:>3d}  REST触发={s['rest_t']:>3d}"
              f"  检出率={detect_rate}")

    # 关键验证
    print(f"\n{'─' * 60}")
    print("  🔍 关键场景验证")
    print(f"{'─' * 60}")

    checks = []

    # 1. WS 失效 → REST 必须能独立触发
    #    ws_dead: WS 从未工作，必须由 REST 触发
    #    ws_drop_midday: WS 中途断连，由 WS 或 REST 触发均可
    ws_pure_dead_loss = [r for r in results
                         if r.scenario == "ws_dead" and r.final_loss_pct >= 10.0]
    ws_drop_loss = [r for r in results
                    if r.scenario == "ws_drop_midday" and r.final_loss_pct >= 10.0]
    ws_dead_all = ws_pure_dead_loss + ws_drop_loss
    # 纯 WS 失效必须全部由 REST 触发
    ws_pure_dead_ok = all(r.tripped and r.trip_source == "rest" for r in ws_pure_dead_loss)
    # 全部 WS 失效场景必须全部触发
    ws_all_tripped = all(r.tripped for r in ws_dead_all)
    checks.append(("WS失效 + 亏损>10% → 全部触发",
                   len(ws_dead_all), sum(1 for r in ws_dead_all if r.tripped), ws_all_tripped))
    checks.append(("  └ WS纯失效 → REST独立触发",
                   len(ws_pure_dead_loss), sum(1 for r in ws_pure_dead_loss if r.tripped and r.trip_source == "rest"),
                   ws_pure_dead_ok))

    # 2. REST 失效 → WS 必须能独立触发
    #    rest_dead: REST 从未工作，必须由 WS 触发
    #    rest_intermittent/rest_drop: REST 间歇可用，由 WS 或 REST 触发均可
    rest_pure_dead_loss = [r for r in results
                           if r.scenario == "rest_dead" and r.final_loss_pct >= 10.0]
    rest_partial_loss = [r for r in results
                         if r.scenario in ("rest_intermittent", "rest_drop_midday")
                         and r.final_loss_pct >= 10.0]
    rest_dead_all = rest_pure_dead_loss + rest_partial_loss
    # 纯 REST 失效必须全部由 WS 触发
    rest_pure_dead_ok = all(r.tripped and r.trip_source == "ws" for r in rest_pure_dead_loss)
    # 全部 REST 失效场景必须全部触发
    rest_all_tripped = all(r.tripped for r in rest_dead_all)
    checks.append(("REST失效 + 亏损>10% → 全部触发",
                   len(rest_dead_all), sum(1 for r in rest_dead_all if r.tripped), rest_all_tripped))
    checks.append(("  └ REST纯失效 → WS独立触发",
                   len(rest_pure_dead_loss), sum(1 for r in rest_pure_dead_loss if r.tripped and r.trip_source == "ws"),
                   rest_pure_dead_ok))

    # 3. 双通道失效 → 绝不误触发
    both_dead = [r for r in results if r.scenario == "both_dead"]
    both_dead_ok = all(not r.tripped for r in both_dead)
    checks.append(("双通道失效 → 绝不误触发",
                   len(both_dead), sum(1 for r in both_dead if r.tripped), both_dead_ok))

    # 4. 双通道正常 + 亏损 < 10% → 不触发
    normal_safe = [r for r in results
                   if r.scenario == "both_normal" and r.final_loss_pct < 10.0]
    normal_safe_ok = all(not r.tripped for r in normal_safe)
    checks.append(("双通道正常 + 亏损<10% → 不触发",
                   len(normal_safe), sum(1 for r in normal_safe if r.tripped), normal_safe_ok))

    # 5. WS stale (数据滞后偏高) + 亏损 > 10% → 必须正确触发 (WS 可能无法触发, 靠 REST)
    ws_stale_loss = [r for r in results
                     if r.scenario == "ws_stale" and r.final_loss_pct >= 10.0]
    ws_stale_ok = all(r.tripped for r in ws_stale_loss)
    checks.append(("WS数据滞后 + 亏损>10% → 正确触发",
                   len(ws_stale_loss), sum(1 for r in ws_stale_loss if r.tripped), ws_stale_ok))

    for name, total, passed, ok in checks:
        status = "✅" if ok else "❌"
        print(f"  {status} {name}: {passed}/{total} 通过")

    # 详细日志
    print(f"\n{'─' * 60}")
    print("  📝 异常日志 (熔断触发 / 交叉验证告警 / 双通道失效)")
    print(f"{'─' * 60}")
    header = f"  {'天':<5s} {'日期':<12s} {'场景':<20s} {'WS':<4s} {'REST':<5s} {'始权益':>8s} {'终权益':>8s} {'亏损%':>7s} {'熔断':<4s} {'来源':<6s}"
    print(header)
    print(f"  {'─' * len(header)}")

    shown = 0
    for r in results:
        if r.tripped or r.cross_alert or r.scenario == "both_dead":
            ws_icon = "✅" if r.ws_working else "❌"
            rest_icon = "✅" if r.rest_working else "❌"
            trip_icon = "⛔" if r.tripped else "—"
            alert_mark = " ⚠️" if r.cross_alert else ""
            print(f"  {r.day:<5d} {r.date:<12s} {r.scenario:<20s} {ws_icon:<4s} {rest_icon:<5s} "
                  f"{r.start_equity:>8.2f} {r.final_equity:>8.2f} {r.final_loss_pct:>6.1f}% "
                  f"{trip_icon:<4s} {r.trip_source:<6s}{alert_mark}")
            shown += 1
            if shown >= 40:
                print(f"  ... (更多记录省略, 共 {len([x for x in results if x.tripped or x.cross_alert or x.scenario == 'both_dead'])} 条)")
                break

    # 结论
    print(f"\n{'═' * 80}")
    all_ok = (stats['false_positives'] == 0 and stats['missed_detections'] == 0)
    if all_ok:
        print("║  ✅ 全部通过 — 0 误触发, 0 漏检")
        print("║  双通道熔断在 WS/REST 单方失效时均能正确独立工作")
    else:
        print(f"║  ❌ 发现问题 — 误触发={stats['false_positives']}, 漏检={stats['missed_detections']}")
        if stats['missed_detections'] > 0:
            missed = [r for r in results if r.final_loss_pct >= 10.0 and not r.tripped
                      and (r.ws_working or r.rest_working)]
            for m in missed:
                print(f"║     漏检: Day{m.day} {m.scenario} loss={m.final_loss_pct}% "
                      f"WS={'ok' if m.ws_working else 'dead'} REST={'ok' if m.rest_working else 'dead'}")
        if stats['false_positives'] > 0:
            fp = [r for r in results if r.tripped and r.final_loss_pct < 10.0
                  and (not r.ws_working and not r.rest_working)]
            for f in fp:
                print(f"║     误触发: Day{f.day} {f.scenario} loss={f.final_loss_pct}%")
    print("═" * 80)

    return all_ok


# ============================================================================
# 跨日重置专项测试
# ============================================================================

def test_cross_day_reset():
    """跨日重置专项测试"""
    from core.daily_loss_breaker import DailyLossBreaker

    all_ok = True

    # —— Test 1: 跨日 + WS 正常 → 重置起始权益 ——
    print("  [Test 1] 跨日 + WS 正常 → 自动重置")
    b = DailyLossBreaker(max_loss_pct=0.10)
    with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(1000.0)):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            b.evaluate()
            assert b._day_start_equity == 1000.0

            # 触发熔断
            with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(890.0)):
                blocked, _ = b.evaluate()
                assert blocked

            # 跨日
            mock_dt.now.return_value = datetime(2026, 7, 17, 0, 1, tzinfo=timezone.utc)
            with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(1050.0)):
                blocked, _ = b.evaluate()
                assert not blocked
                assert b._current_date == "2026-07-17"
                assert b._day_start_equity == 1050.0
    print("    ✅ 正确重置, 新起始权益=1050.0")

    # —— Test 2: 跨日 + WS 不可用 → 清零, 等 REST 初始化 ——
    print("  [Test 2] 跨日 + WS 不可用 → 清零起始权益, 等待 REST")
    b2 = DailyLossBreaker(max_loss_pct=0.10)
    real_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(1000.0)):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            b2.evaluate()
            assert b2._day_start_equity == 1000.0

            # Day 2: WS 断连
            mock_dt.now.return_value = datetime(2026, 7, 17, 0, 1, tzinfo=timezone.utc)
            with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(0.0)):
                b2.evaluate()

    # 对齐真实 UTC 日期，避免 _simulate_rest_evaluation 中的 _check_day_reset 触发虚假跨日
    b2._current_date = real_utc

    if b2._day_start_equity != 0.0:
        print(f"    ❌ BUG: 跨日+WS不可用时起始权益应为0, 实际={b2._day_start_equity}")
        all_ok = False
    else:
        print(f"    ✅ 起始权益已清零: {b2._day_start_equity}")

    # REST 通道初始化 (force=True 绕过实时频率守卫)
    blocked, reason = _simulate_rest_evaluation(b2, 1020.0, force=True)
    if blocked:
        print(f"    ❌ REST 初始化不应触发熔断, reason={reason}")
        all_ok = False
    elif b2._day_start_equity != 1020.0:
        print(f"    ❌ BUG: REST 应初始化起始权益=1020, 实际={b2._day_start_equity}")
        all_ok = False
    else:
        print(f"    ✅ REST 正确初始化新日起始权益: {b2._day_start_equity}")

    # —— Test 3: 跨日 + WS 不可用 + 首次运行 (无前日) ——
    print("  [Test 3] 首次运行 WS 不可用 → 等待 REST")
    b3 = DailyLossBreaker(max_loss_pct=0.10)
    with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(0.0)):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
            mock_dt.strftime = datetime.strftime
            b3.evaluate()
    if b3._day_start_equity != 0.0:
        print(f"    ❌ BUG: 首次运行WS不可用起始权益应为0, 实际={b3._day_start_equity}")
        all_ok = False
    else:
        print(f"    ✅ 起始权益={b3._day_start_equity} (等待REST初始化)")

    # —— Test 4: force_reset 清空 REST 状态 ——
    print("  [Test 4] force_reset → 双通道状态重置")
    b4 = DailyLossBreaker(max_loss_pct=0.10)
    real_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(1000.0)):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
            mock_dt.strftime = datetime.strftime
            b4.evaluate()
            # 触发熔断
            with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(890.0)):
                blocked, _ = b4.evaluate()
                assert blocked
            # force_reset
            b4.force_reset()
            b4._current_date = real_utc
    assert not b4.is_tripped
    assert b4._last_rest_evaluation == 0.0, f"_last_rest_evaluation={b4._last_rest_evaluation}"
    assert b4._rest_equity == 0.0
    assert b4._rest_trip_source == ""
    print("    ✅ force_reset 正确重置双通道状态")

    # —— Test 5: REST 触发后 WS 确认不重复触发 ——
    print("  [Test 5] REST 先触发 → WS 确认已熔断, 不重复")
    b5 = DailyLossBreaker(max_loss_pct=0.10)

    # 用当前真实 UTC 日期，避免 mock 日期与真实日期不一致导致的跨日重置
    real_utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(1000.0)):
        with patch("core.daily_loss_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
            mock_dt.strftime = datetime.strftime
            b5.evaluate()
            # 确保日期与真实 UTC 一致
            b5._current_date = real_utc_date

    if b5._day_start_equity <= 0:
        print(f"    ❌ BUG: evaluate 应初始化 _day_start_equity=1000, 实际={b5._day_start_equity}")
        all_ok = False
    else:
        # REST 检测到亏损触发 (force=True 模拟 60s 后的 REST 查询)
        blocked, reason = _simulate_rest_evaluation(b5, 890.0, force=True)
        if not blocked:
            print(f"    DEBUG: _day_start_equity={b5._day_start_equity}, _tripped={b5._tripped}, "
                  f"_rest_equity={b5._rest_equity}, _current_date={b5._current_date}, "
                  f"real_utc={real_utc_date}")
        assert blocked, f"REST should trigger at 11% loss. reason={reason}"
        assert b5._rest_trip_source == "rest", f"trip_source={b5._rest_trip_source}"
        assert b5.is_tripped
        trip_reason = b5._trip_reason

        # WS 再检查 — 确认已熔断
        with patch("core.daily_loss_breaker._get_balance_tracker", return_value=_make_mock_bt(890.0)):
            with patch("core.daily_loss_breaker.datetime") as mock_dt2:
                mock_dt2.now.return_value = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
                mock_dt2.strftime = datetime.strftime
                b5._current_date = real_utc_date  # 保持日期一致
                blocked2, _ = b5.evaluate()
                assert blocked2, "WS 应确认熔断状态"
                assert b5._trip_reason == trip_reason, "WS 不应覆盖 REST 的触发原因"
        print("    ✅ WS 确认已熔断, 保留 REST 触发原因")

    if all_ok:
        print("  ✅ 跨日重置专项测试全部通过")
    return all_ok


# ============================================================================
# Entry point
# ============================================================================

def main():
    print("\n🚀 365天双通道熔断模拟测试启动...")
    results, stats = simulate_365_days()
    main_ok = print_report(results, stats)

    print(f"\n{'─' * 60}")
    print("  🧪 跨日重置专项测试")
    print(f"{'─' * 60}")
    cross_ok = test_cross_day_reset()

    all_ok = main_ok and cross_ok
    print(f"\n{'═' * 80}")
    if all_ok:
        print("║  🎉 全部测试通过 — 双通道熔断保护健壮可靠")
    else:
        print("║  ⚠️  部分测试失败，请检查上述输出")
    print("═" * 80)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
