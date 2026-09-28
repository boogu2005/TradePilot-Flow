"""Teacher protection targets must survive entry, updates, and repair."""

from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.protection_targets import (
    initial_sl_price,
    normalize_tp_prices,
    teacher_tp_prices,
    tp_price_for,
    single_teacher_tp,
    teacher_tp_stage_confirmed,
)
from signal_engine.parser import SignalParser
from exit.tp1 import TP1Checker
from exit.tp2 import TP2Checker
from core.reconciler import _recompute_initial_stop_loss, _tp_rehang_blocked, read_desired_state
from core.reconciler import ensure_protection
from core.reconciler import reconcile
from exchange_engine.signal_updater import apply_update_signal, _place_sl
from core.protection_creator import ProtectionCreator


def trade(**kwargs):
    fields = dict(pair="BTC/USDT:USDT", exchange="okx", open_rate=100.0,
                  is_short=False, stop_loss=98.0,
                  initial_stop_loss=98.0, tp1_price=None, tp2_price=None,
                  signal_meta={})
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def test_market_signal_keeps_teacher_sl_and_tp():
    parser = SignalParser(api_key="test")
    signal = parser.standardize({
        "message_type": "MARKET_ORDER", "symbol": "BTC", "direction": "long",
        "sl": 97, "tp": [105, 110],
    }, "#BTC long SL97 TP105 TP110")
    assert signal["stop_loss"] == 97.0
    assert signal["take_profit"] == [105.0, 110.0]


def test_limit_signal_uses_explicit_prices_and_debug_fallback():
    parser = SignalParser(api_key="test")
    signal = parser.standardize({
        "message_type": "LIMIT_ORDER", "symbol": "BTC", "direction": "long",
        "entry_low": 100, "sl": 96, "tp": [104],
        "parse_debug": {"recognized": {"sl": 95, "tp": [106]}},
    }, "entry 100 SL96 TP104")
    assert signal["stop_loss"] == 96.0
    assert signal["take_profit"] == [104.0]


def test_market_signal_uses_recognized_prices_when_top_level_missing():
    parser = SignalParser(api_key="test")
    signal = parser.standardize({
        "message_type": "MARKET_ORDER", "symbol": "ETH", "direction": "short",
        "parse_debug": {"recognized": {"sl": "104", "tp": [96]}},
    }, "#ETH short SL104 TP96")
    assert signal["stop_loss"] == 104.0
    assert signal["take_profit"] == [96.0]


def test_invalid_teacher_prices_are_not_accepted():
    assert normalize_tp_prices(["105", 0, float("nan"), -1, "bad", 110]) == [105.0, 110.0]


def test_teacher_sl_replaces_initial_default_and_single_tp_disables_tp2():
    position = trade(signal_meta={"_teacher_sl_price": 96, "_teacher_tp_prices": [108]})
    assert initial_sl_price(position) == 96.0
    assert teacher_tp_prices(position) == [108.0]
    assert tp_price_for(position, 1) == 108.0
    assert tp_price_for(position, 2) is None
    assert single_teacher_tp(position)


def test_two_teacher_tps_never_mix_with_default_target():
    position = trade(signal_meta={"_teacher_tp_prices": [108, 112]},
                     tp1_price=103, tp2_price=106)
    assert tp_price_for(position, 1) == 108.0
    assert tp_price_for(position, 2) == 112.0


def test_single_teacher_tp_waits_for_teacher_price_then_closes_all():
    position = trade(pair="BTC/USDT:USDT", position_state="open",
                     trailing_activated=False, signal_meta={"_teacher_tp_prices": [108]})
    checker = TP1Checker({"tp1": {"enabled": True, "profit_pct": 0.03, "close_pct": 30}})
    assert asyncio.run(checker.check(position, "okx", None, current_price=103)) is None
    result = asyncio.run(checker.check(position, "okx", None, current_price=108))
    assert result.close_pct == 100


def test_single_teacher_tp_never_triggers_default_tp2():
    position = trade(pair="BTC/USDT:USDT", position_state="tp1_filled",
                     trailing_activated=False, signal_meta={"_teacher_tp_prices": [108]})
    checker = TP2Checker({"tp2": {"enabled": True, "profit_pct": 0.06, "close_pct": 50}})
    assert asyncio.run(checker.check(position, "okx", None, current_price=130)) is None


def test_limit_fill_uses_teacher_sl_even_when_wider_than_fixed_stop():
    position = trade(stop_loss=0, initial_stop_loss=0,
                     signal_meta={"stop_loss": 94})
    _recompute_initial_stop_loss(position)
    assert position.stop_loss == 94
    assert position.initial_stop_loss == 94


def test_rehang_guard_uses_teacher_tp_instead_of_default():
    position = trade(tp1_price=103, signal_meta={"_teacher_tp_prices": [108]},
                     amount_requested=100, pair="BTC/USDT:USDT")
    assert asyncio.run(_tp_rehang_blocked(position, 1, 100, 105)) is None
    assert "补挂会立即成交" in asyncio.run(_tp_rehang_blocked(position, 1, 100, 108))
    assert "无法确定TP触发价" in asyncio.run(_tp_rehang_blocked(position, 2, 100, 105))


def test_modify_tp_replaces_default_targets_with_one_full_teacher_tp():
    position = trade(id=1, pair="BTC/USDT:USDT", exchange="okx",
                     amount=10, position_state="open", has_open_orders=False,
                     tp1_algo_id="default1", tp2_algo_id="default2",
                     sl_algo_id="sl", orders=[])
    session = MagicMock()
    signal = {"symbol": "BTCUSDT", "direction": "long", "update_type": "modify_tp",
              "new_take_profit": [108], "new_stop_loss": None}
    from core.protection_creator import CreateResult
    result = CreateResult(success=True, algo_key="tp1", algo_id="teacher1")
    with patch("exchange_engine.signal_updater.resolve_update_direction", new=AsyncMock(return_value=("long", "signal"))), \
         patch("exchange_engine.signal_updater._resolve_trade", new=AsyncMock(return_value=(position, "db"))), \
         patch("exchange_engine.signal_updater.trade_lock_manager.acquire", new=AsyncMock(return_value=asyncio.Lock())), \
         patch("exit.protection.cancel_tp", new=AsyncMock(return_value=True)) as cancel, \
         patch("core.protection_creator.protection_creator.create_tp", new=AsyncMock(return_value=result)) as create:
        ok, _ = asyncio.run(apply_update_signal(session, signal))
    assert ok
    assert cancel.await_count == 2
    assert create.await_count == 1
    assert create.await_args.kwargs["tp_index"] == 1
    assert position.signal_meta["_teacher_tp_prices"] == [108.0]
    assert position.tp1_price == 108.0
    assert position.tp2_price is None


def test_initial_setup_places_teacher_tp_only_without_default_tp2():
    position = trade(id=2, is_open=True, amount=10, position_state="open",
                     signal_meta={"stop_loss": 96, "take_profit": [108]},
                     sl_algo_id=None, tp1_algo_id=None, tp2_algo_id=None)
    session = MagicMock()
    from core.protection_creator import CreateResult
    sl_result = CreateResult(success=True, algo_key="sl", algo_id="sl1")
    tp_result = CreateResult(success=True, algo_key="tp1", algo_id="tp1")
    with patch("core.reconciler._fetch_okx_algo_orders", new=AsyncMock(return_value=[])), \
         patch("core.protection_creator.protection_creator.create_sl", new=AsyncMock(return_value=sl_result)) as create_sl, \
         patch("core.protection_creator.protection_creator.create_tp", new=AsyncMock(return_value=tp_result)) as create_tp, \
         patch("sqlalchemy.orm.attributes.flag_modified"):
        outcome = asyncio.run(ensure_protection(position, session))
    assert outcome["sl"] == "created"
    assert outcome["tp"] == "created"
    assert outcome["tp2"] == "skipped"
    assert create_sl.await_count == 1
    assert create_tp.await_count == 1
    assert position.tp1_price == 108
    assert position.tp2_price is None


def test_single_teacher_tp_order_uses_entire_position():
    position = trade(id=3, is_open=True, amount=10, position_state="open",
                     exit_side="sell", signal_meta={"_teacher_tp_prices": [108]},
                     orders=[], tp1_algo_id=None)
    session = MagicMock()
    order = SimpleNamespace(ft_order_role=None, ft_order_tag=None)
    with patch("core.protection_creator.ProtectionCreator._check_cooldown", return_value=True), \
         patch("exchange_engine.exchange.fetch_positions", new=AsyncMock(return_value=[
             {"symbol": "BTC/USDT:USDT", "contracts": 10}])), \
         patch("core.protection_creator._okx_has_live_tp", new=AsyncMock(return_value=(False, None))), \
         patch("core.protection_creator._normalize_amount", side_effect=lambda pair, qty, ex: qty), \
         patch("core.protection_creator.runtime.create_algo_order", new=AsyncMock(return_value={"id": "tp-teacher"})) as create, \
         patch("core.protection_creator.Order.parse_from_ccxt", return_value=order):
        result = asyncio.run(ProtectionCreator.create_tp(position, session, tp_index=1))
    assert result.success
    assert create.await_args.args[3] == 10
    assert create.await_args.args[4] == 108


def test_late_single_teacher_tp_uses_tp2_slot_and_full_remaining_position():
    position = trade(id=4, is_open=True, amount=7, position_state="tp1_filled",
                     exit_side="sell", signal_meta={"_teacher_tp_prices": [112],
                                                   "_teacher_tp_start_index": 2},
                     orders=[], tp2_algo_id=None)
    session = MagicMock()
    order = SimpleNamespace(ft_order_role=None, ft_order_tag=None)
    with patch("core.protection_creator.ProtectionCreator._check_cooldown", return_value=True), \
         patch("exchange_engine.exchange.fetch_positions", new=AsyncMock(return_value=[
             {"symbol": "BTC/USDT:USDT", "contracts": 7}])), \
         patch("core.protection_creator._okx_has_live_tp", new=AsyncMock(return_value=(False, None))), \
         patch("core.protection_creator._normalize_amount", side_effect=lambda pair, qty, ex: qty), \
         patch("core.protection_creator.runtime.create_algo_order", new=AsyncMock(return_value={"id": "tp-teacher"})) as create, \
         patch("core.protection_creator.Order.parse_from_ccxt", return_value=order):
        result = asyncio.run(ProtectionCreator.create_tp(position, session, tp_index=2))
    assert result.success
    assert create.await_args.args[3] == 7
    assert create.await_args.args[4] == 112


def test_desired_state_has_no_default_tp2_when_teacher_only_gave_one():
    position = trade(id=5, trailing_activated=False,
                     signal_meta={"_teacher_sl_price": 96, "_teacher_tp_prices": [108]},
                     tp1_price=103, tp2_price=106)
    desired = read_desired_state(position)
    assert desired.target_sl == 96
    assert desired.target_tp1_price == 108
    assert desired.target_tp1_pct == 100
    assert desired.target_tp2_price == 0


def test_ensure_protection_replaces_default_sl_before_marking_setup_done():
    from core.protection_creator import CreateResult

    position = trade(id=20, is_open=True, amount=10, position_state="open",
                     stop_loss=94, initial_stop_loss=94,
                     sl_algo_id="default-sl", signal_meta={"_teacher_sl_price": 94,
                                                           "_teacher_tp_prices": [108]})
    old_sl = {"instId": "BTC-USDT-SWAP", "algoId": "default-sl",
              "slTriggerPx": "98", "triggerPx": "98", "state": "live"}
    events = []

    async def create_sl(*args):
        events.append("create-teacher-sl")
        position.sl_algo_id = "teacher-sl"
        return CreateResult(success=True, algo_key="sl", algo_id="teacher-sl")

    async def cancel_sl(*args):
        events.append("cancel-default-sl")

    with patch("core.reconciler._fetch_okx_algo_orders", new=AsyncMock(return_value=[old_sl])), \
         patch("core.protection_creator.protection_creator.create_sl", new=create_sl), \
         patch("core.protection_creator.protection_creator.create_tp", new=AsyncMock(
             return_value=CreateResult(success=True, algo_key="tp1", algo_id="teacher-tp1"))), \
         patch("core.reconciler.runtime.cancel_algo_order", new=cancel_sl), \
         patch("sqlalchemy.orm.attributes.flag_modified"):
        result = asyncio.run(ensure_protection(position, MagicMock()))
    assert result["sl"] == "created"
    assert events == ["create-teacher-sl", "cancel-default-sl"]


def test_teacher_sl_replacement_creates_new_order_before_cancelling_old():
    position = trade(id=6, exchange="okx", sl_algo_id="default-sl", orders=[],
                     exit_side="sell")
    session = MagicMock()
    from core.protection_creator import CreateResult
    events = []

    async def create_sl(trade_arg, session_arg):
        events.append("create")
        assert trade_arg.stop_loss == 95
        trade_arg.sl_algo_id = "teacher-sl"
        return CreateResult(success=True, algo_key="sl", algo_id="teacher-sl")

    async def cancel_sl(algo_id, pair):
        events.append("cancel")
        assert algo_id == "default-sl"

    with patch("core.protection_creator.protection_creator.create_sl", new=create_sl), \
         patch("core.protection_creator.protection_creator.bump_clordid_version"), \
         patch("core.exchange_runtime.runtime.cancel_algo_order", new=cancel_sl):
        assert asyncio.run(_place_sl(position, 95, session))
    assert events == ["create", "cancel"]
    assert position.initial_stop_loss == 95


def test_teacher_tp_stage_requires_actual_position_reduction():
    position = trade(amount_requested=100, signal_meta={"_teacher_tp_prices": [108, 112]})
    assert not teacher_tp_stage_confirmed(position, 1, 100)
    assert teacher_tp_stage_confirmed(position, 1, 70)
    assert not teacher_tp_stage_confirmed(position, 2, 70)
    assert teacher_tp_stage_confirmed(position, 2, 35)
    position.signal_meta = {"_teacher_tp_prices": [108]}
    assert not teacher_tp_stage_confirmed(position, 1, 70)


def test_late_teacher_tp_update_targets_remaining_position():
    position = trade(id=7, amount=7, position_state="tp1_filled",
                     has_open_orders=False, tp1_algo_id=None, tp2_algo_id="default2",
                     orders=[])
    session = MagicMock()
    signal = {"symbol": "BTCUSDT", "direction": "long", "update_type": "move_tp",
              "new_take_profit": [112]}
    from core.protection_creator import CreateResult
    result = CreateResult(success=True, algo_key="tp2", algo_id="teacher2")
    with patch("exchange_engine.signal_updater.resolve_update_direction", new=AsyncMock(return_value=("long", "signal"))), \
         patch("exchange_engine.signal_updater._resolve_trade", new=AsyncMock(return_value=(position, "db"))), \
         patch("exchange_engine.signal_updater.trade_lock_manager.acquire", new=AsyncMock(return_value=asyncio.Lock())), \
         patch("exit.protection.cancel_tp", new=AsyncMock(return_value=True)), \
         patch("core.protection_creator.protection_creator.create_tp", new=AsyncMock(return_value=result)) as create:
        ok, _ = asyncio.run(apply_update_signal(session, signal))
    assert ok
    assert position.signal_meta["_teacher_tp_start_index"] == 2
    assert position.tp1_price is None
    assert position.tp2_price == 112
    assert create.await_args.kwargs["tp_index"] == 2
    checker = TP2Checker({"tp2": {"enabled": True, "profit_pct": 0.06, "close_pct": 50}})
    assert asyncio.run(checker.check(position, "okx", None, current_price=112)).close_pct == 100


def test_phase_d_repairs_teacher_prices_without_default_targets():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from database.models import Base, Trade
    from core.protection_creator import CreateResult

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    position = Trade(pair="BTC/USDT:USDT", base_currency="BTC", stake_currency="USDT",
                     exchange="okx", is_open=True, is_short=False, open_rate=100,
                     amount=10, amount_requested=10, stop_loss=0, initial_stop_loss=0,
                     position_state="open", trading_mode="futures", strategy="test",
                     signal_id="teacher", stake_amount=100, leverage=10,
                     signal_meta={"_teacher_sl_price": 94, "_teacher_tp_prices": [108]})
    session.add(position)
    session.commit()
    calls = []

    async def create_sl(trade_arg, session_arg):
        calls.append(("sl", trade_arg.stop_loss))
        trade_arg.sl_algo_id = "teacher-sl"
        return CreateResult(success=True, algo_key="sl", algo_id="teacher-sl")

    async def create_tp(trade_arg, session_arg, tp_index=1):
        calls.append((f"tp{tp_index}", tp_price_for(trade_arg, tp_index)))
        setattr(trade_arg, f"tp{tp_index}_algo_id", f"teacher-tp{tp_index}")
        return CreateResult(success=True, algo_key=f"tp{tp_index}", algo_id=f"teacher-tp{tp_index}")

    try:
        with patch("core.reconciler._fetch_okx_positions", new=AsyncMock(return_value=[{
                 "symbol": "BTC-USDT-SWAP", "side": "long", "contracts": 10,
                 "entry_price": 100}])), \
             patch("core.reconciler._fetch_all_algo_orders", new=AsyncMock(return_value=[])), \
             patch("core.reconciler._cleanup_residual_algos", new=AsyncMock(return_value=0)), \
             patch("core.reconciler.cleanup_orphans", new=AsyncMock(return_value=0)), \
             patch("exchange_engine.exchange.fetch_open_orders", new=AsyncMock(return_value=[])), \
             patch("core.reconciler.runtime.fetch_ticker", new=AsyncMock(return_value={"last": 105})), \
             patch("core.reconciler._can_split_position", return_value=True), \
             patch("core.protection_creator.protection_creator.create_sl", new=create_sl), \
             patch("core.protection_creator.protection_creator.create_tp", new=create_tp):
            asyncio.run(reconcile(session))
        assert ("sl", 94) in calls
        assert ("tp1", 108) in calls
        assert not any(name == "tp2" for name, _ in calls)
        session.refresh(position)
        assert position.initial_stop_loss == 94
        assert position.tp2_price is None
    finally:
        session.close()
        engine.dispose()


def test_phase_d_replaces_existing_default_orders_with_teacher_orders():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from database.models import Base, Trade
    from core.protection_creator import CreateResult

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    position = Trade(pair="BTC/USDT:USDT", base_currency="BTC", stake_currency="USDT",
                     exchange="okx", is_open=True, is_short=False, open_rate=100,
                     amount=10, amount_requested=10, stop_loss=98, initial_stop_loss=98,
                     tp1_price=103, tp2_price=106, sl_algo_id="default-sl",
                     tp1_algo_id="default-tp1", tp2_algo_id="default-tp2",
                     position_state="open", trading_mode="futures", strategy="test",
                     signal_id="teacher", stake_amount=100, leverage=10,
                     signal_meta={"_teacher_sl_price": 94, "_teacher_tp_prices": [108]})
    session.add(position)
    session.commit()
    default_orders = [
        {"algoId": "default-sl", "instId": "BTC-USDT-SWAP", "posSide": "long",
         "state": "live", "ordType": "stoploss", "triggerPx": "98", "slTriggerPx": "98"},
        {"algoId": "default-tp1", "instId": "BTC-USDT-SWAP", "posSide": "long",
         "state": "live", "ordType": "takeprofit", "triggerPx": "103",
         "tpTriggerPx": "103", "clOrdId": "bottp-old1"},
        {"algoId": "default-tp2", "instId": "BTC-USDT-SWAP", "posSide": "long",
         "state": "live", "ordType": "takeprofit", "triggerPx": "106",
         "tpTriggerPx": "106", "clOrdId": "bottp-old2"},
    ]
    calls = []
    cancelled = []

    async def create_sl(trade_arg, session_arg):
        calls.append(("sl", trade_arg.stop_loss))
        trade_arg.sl_algo_id = "teacher-sl"
        return CreateResult(success=True, algo_key="sl", algo_id="teacher-sl")

    async def create_tp(trade_arg, session_arg, tp_index=1):
        calls.append((f"tp{tp_index}", tp_price_for(trade_arg, tp_index)))
        setattr(trade_arg, f"tp{tp_index}_algo_id", f"teacher-tp{tp_index}")
        return CreateResult(success=True, algo_key=f"tp{tp_index}", algo_id=f"teacher-tp{tp_index}")

    async def cancel(algo_id, instrument):
        cancelled.append(algo_id)

    try:
        with patch("core.reconciler._fetch_okx_positions", new=AsyncMock(return_value=[{
                 "symbol": "BTC-USDT-SWAP", "side": "long", "contracts": 10,
                 "entry_price": 100}])), \
             patch("core.reconciler._fetch_all_algo_orders", new=AsyncMock(return_value=default_orders)), \
             patch("core.reconciler._cleanup_residual_algos", new=AsyncMock(return_value=0)), \
             patch("core.reconciler.cleanup_orphans", new=AsyncMock(return_value=0)), \
             patch("exchange_engine.exchange.fetch_open_orders", new=AsyncMock(return_value=[])), \
             patch("core.reconciler.runtime.fetch_ticker", new=AsyncMock(return_value={"last": 105})), \
             patch("core.reconciler.runtime.cancel_algo_order", new=cancel), \
             patch("core.reconciler._can_split_position", return_value=True), \
             patch("core.protection_creator.protection_creator.create_sl", new=create_sl), \
             patch("core.protection_creator.protection_creator.create_tp", new=create_tp):
            asyncio.run(reconcile(session))
        assert ("sl", 94) in calls
        assert ("tp1", 108) in calls
        assert not any(name == "tp2" for name, _ in calls)
        assert {"default-sl", "default-tp1", "default-tp2"} <= set(cancelled)
    finally:
        session.close()
        engine.dispose()


def test_same_update_replaces_teacher_sl_and_tp_together():
    position = trade(id=8, amount=10, position_state="open", has_open_orders=False,
                     tp1_algo_id="old-tp", tp2_algo_id=None, sl_algo_id="old-sl",
                     orders=[])
    session = MagicMock()
    signal = {"symbol": "BTCUSDT", "direction": "long", "update_type": "modify_both",
              "new_stop_loss": 95, "new_take_profit": [108]}
    from core.protection_creator import CreateResult
    result = CreateResult(success=True, algo_key="tp1", algo_id="new-tp")
    with patch("exchange_engine.signal_updater.resolve_update_direction", new=AsyncMock(return_value=("long", "signal"))), \
         patch("exchange_engine.signal_updater._resolve_trade", new=AsyncMock(return_value=(position, "db"))), \
         patch("exchange_engine.signal_updater.trade_lock_manager.acquire", new=AsyncMock(return_value=asyncio.Lock())), \
         patch("exchange_engine.signal_updater._place_sl", new=AsyncMock(return_value=True)) as place_sl, \
         patch("exit.protection.cancel_tp", new=AsyncMock(return_value=True)), \
         patch("core.protection_creator.protection_creator.create_tp", new=AsyncMock(return_value=result)) as create_tp:
        ok, _ = asyncio.run(apply_update_signal(session, signal))
    assert ok
    assert place_sl.await_args.args[1] == 95
    assert create_tp.await_count == 1
    assert position.signal_meta["_teacher_sl_price"] == 95
    assert position.signal_meta["_teacher_tp_prices"] == [108]
