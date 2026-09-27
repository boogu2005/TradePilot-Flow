"""
自动交易机器人 — 主入口。

流水线：
  Telegram 监听 (Telethon)
      ↓
  DeepSeek 解析 → 标准化信号
      ↓
  风控检查 → 仓位计算
      ↓
  CCXT 执行入场 (OKX) → 挂止损 → 挂分批止盈
      ↓
  订单管理循环：追踪入场成交、止损更新、止盈检查
"""
from __future__ import annotations

import asyncio
import signal as unix_signal
import os
import time
from datetime import datetime, timezone, timedelta
from utils.time import ensure_utc, dt_seconds_ago

from dotenv import load_dotenv
from loguru import logger

from database.db import init_db, get_session, close_db
from database.models import Trade, Order, SignalLog
from telegram_engine.listener import TelegramListener
from signal_engine.parser import SignalParser
from signal_engine.groups import SIGNAL_GROUPS, get_group_full_names
from exchange_engine import exchange as ex
from exchange_engine.trade_executor import execute_entry, risk_check, calculate_position_size, execute_trade_exit
from exchange_engine.order_manager import check_entry_order, post_entry_setup
from exchange_engine.signal_updater import apply_update_signal, apply_close_signal, apply_cancel_signal
from exit.manager import ExitManager

# ———————— 核心模块（生命周期 / 状态 / 任务管理）————————
from core.states import (
    ServiceRegistry, ModuleState,
    MOD_EXCHANGE, MOD_TELEGRAM,
    MOD_ORDER_MONITOR, MOD_SIGNAL_CONSUMER,
)
from core.task_manager import TaskManager
from core.exchange_supervisor import ExchangeSupervisor
from core.lifecycle import AppLifecycle
from core.deps_check import check_all_dependencies
from core.trade_lock import trade_lock_manager
from core.transaction import atomic_transaction

load_dotenv(override=True)  # 强制使用 .env 文件覆盖系统环境变量

# ———————— 代理设置（国内访问交易所必需）————
_proxy = os.getenv("EXCHANGE_PROXY", "").strip()
if _proxy:
    os.environ["HTTP_PROXY"] = _proxy
    os.environ["HTTPS_PROXY"] = _proxy


# ———————— 协调器节奏控制 ————————
# v5: Single 600s cycle. No more dual quick/heavy cycles.
# Reconcile is a health check, NOT a continuous repair loop.
_last_reconcile_time: float = 0.0        # 600s 低频健康检查
_last_reconcile_log_time: float = 0.0     # 3600s 日志

# ———————— 看门狗心跳状态 ————————
_HEARTBEAT: dict = {
    "consumer_last_tick": 0.0,
    "consumer_last_signal": 0.0,
    "monitor_last_tick": 0.0,
}
_last_error_log: dict = {"time": 0.0, "msg": ""}  # 错误日志去重：同一消息 30s 内不重复打印
_cleaned_symbols: set[str] = set()  # 已清理残余订单的币种列表，避免重复请求


# ———————— 日志 ————————
def setup_logging():
    os.makedirs("user_data/logs", exist_ok=True)
    logger.remove()
    logger.add("user_data/logs/bot_{time:YYYY-MM-DD}.log", level="INFO",
               rotation="50 MB", retention="14 days", compression="zip",
               encoding="utf-8", enqueue=True, backtrace=True, diagnose=False)
    # 控制台输出 — Windows 下强制 UTF-8
    import sys, logging as std_logging
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logger.add(lambda msg: print(msg), level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <level>{message}</level>")
    # 屏蔽 SQLAlchemy / 数据库相关日志噪音
    for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool", "sqlalchemy.orm"):
        std_logging.getLogger(name).setLevel(std_logging.ERROR)


# ———————— 启动自检 ————————
def _startup_self_check() -> bool:
    """
    P0-1: 启动前自检 — 验证当前 main.py 及其他核心模块可被 Python 正常编译。

    防止：磁盘源码更新后运维忘记重启服务，进程仍执行旧代码。
    返回 True 表示编译通过，False 表示存在语法错误（拒绝启动）。
    """
    import py_compile
    import sys as _sys

    _core_files = [
        "main.py",
        "database/models.py",
        "core/reconciler.py",
        "core/protection_creator.py",
        "exchange_engine/exchange.py",
        "exchange_engine/trade_executor.py",
        "exit/manager.py",
        "exit/protection.py",
    ]

    _base = os.path.dirname(__file__)
    for _fname in _core_files:
        _path = os.path.join(_base, _fname)
        if not os.path.exists(_path):
            logger.warning(f"[启动自检] 跳过不存在的文件: {_fname}")
            continue
        try:
            py_compile.compile(_path, doraise=True)
        except py_compile.PyCompileError as e:
            logger.critical(f"[启动自检] {_fname} 语法错误，拒绝启动: {e}")
            return False

    logger.success(f"[启动自检] {len(_core_files)} 个核心文件编译通过")
    return True


# ———————— 订单监控循环 ————————
async def order_monitor(shutdown_event: asyncio.Event, exit_manager: ExitManager):
    """
    订单监控循环 — 使用 ExitManager 统一退出决策。
    每 5 秒检查一次所有 open trade（OKX）：
    1. 入场单是否已成交
    2. 入场首次成交 → 挂止损止盈
    3. ExitManager 链式检查 → 按优先级只有一个退出模块激活

    状态门控：交易所未连接时自动暂停，恢复后自动继续。
    """
    from core.exchange_runtime import runtime

    logger.info("订单监控已启动 (5s 间隔, ExitManager 模式)")
    ServiceRegistry.set_state(MOD_ORDER_MONITOR, ModuleState.RUNNING)

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        if shutdown_event.is_set():
            break

        # ———— 状态门控：交易所未连接时暂停监控 ————
        if not ServiceRegistry.can_trade():
            if ServiceRegistry.get_state(MOD_ORDER_MONITOR) != ModuleState.PAUSED:
                logger.warning("[订单监控] 交易所未连接，暂停监控（等待重连...）")
                ServiceRegistry.set_state(MOD_ORDER_MONITOR, ModuleState.PAUSED)
            continue

        # ———— 后台熔断监控：双通道权益校验 ————
        # Channel 1 (WS):  每 5s  同步评估 BalanceTracker 权益
        # Channel 2 (REST): 每 60s 主动调用 OKX REST 查询权益（绕过缓存）
        # 任一通道触发 → 设 _tripped flag → 开仓路径拦截
        # 开仓路径只读 is_tripped，不在此处重复计算
        try:
            from core.daily_loss_breaker import daily_loss_breaker
            # Channel 1: WS 通道 — 每 5s 轻量同步评估
            daily_loss_breaker.evaluate()
            # Channel 2: REST 通道 — 每 60s 主动 REST 查询，双重保障
            if daily_loss_breaker.needs_rest_evaluation():
                await daily_loss_breaker.evaluate_rest()
        except Exception:
            pass

        # ———— BugFix: Runtime READY gate ————
        # Coordinator/Repair/Protection must NOT run before Runtime is fully initialized
        if not runtime.is_ready:
            if ServiceRegistry.get_state(MOD_ORDER_MONITOR) != ModuleState.PAUSED:
                logger.warning("[订单监控] ExchangeRuntime 尚未 READY，等待初始化完成...")
                ServiceRegistry.set_state(MOD_ORDER_MONITOR, ModuleState.PAUSED)
            continue

        # 交易所已恢复 → 标记运行
        if ServiceRegistry.get_state(MOD_ORDER_MONITOR) == ModuleState.PAUSED:
            logger.success("[订单监控] 交易所已恢复，恢复监控")
            ServiceRegistry.set_state(MOD_ORDER_MONITOR, ModuleState.RUNNING)

        session = None
        try:
            session = get_session()
            active_trades = Trade.get_active_trades(session)

            # --- Step 0a: position sync + state reconciliation (v5: 600s health check)
            try:
                from core.reconciler import reconcile
                global _last_reconcile_time, _last_reconcile_log_time
                now_t = time.time()
                # v5: Single 600s low-frequency health check
                if now_t - _last_reconcile_time >= 600:
                    stats = await reconcile(session)
                    _last_reconcile_time = now_t
                else:
                    stats = {}
                # 日志输出：有修复立即打，无修复每小时打一次
                total = sum(v for v in stats.values() if isinstance(v, int))
                if total > 0:
                    detail = " | ".join(f"{k}={v}" for k, v in stats.items() if v)
                    logger.info(f"协调器健康检查 {total} 项: {detail}")
                elif now_t - _last_reconcile_log_time >= 3600:
                    logger.info(f"协调器健康检查完成，无异常")
                    _last_reconcile_log_time = now_t
            except Exception as e:
                logger.warning(f"[监控-协调] 状态协调异常: {e}")

            # 同步后重新查询活跃 trade（部分可能已被同步标记为关闭）
            active_trades = Trade.get_active_trades(session)

            # Batch REST position fetch once per order_monitor cycle (not per-trade)
            _okx_positions_cache = None
            async def _get_okx_positions():
                nonlocal _okx_positions_cache
                if _okx_positions_cache is None:
                    try:
                        from exchange_engine.exchange import fetch_positions
                        _okx_positions_cache = await fetch_positions(exchange="okx")
                    except Exception:
                        _okx_positions_cache = []
                return _okx_positions_cache

            for trade in active_trades:
                trade_ex = trade.exchange or "okx"

                # P1-1: 获取 Trade 锁（细粒度锁，按 trade.id）
                # 整个 trade 处理逻辑都在锁的保护范围内
                trade_lock = await trade_lock_manager.acquire(trade.id)
                async with trade_lock:
                    # --- Step 0: cleanup residual algo orders (fallback)
                    # Primary: cleanup_trade_orders() called from execute_trade_exit().
                    # Fallback: handle positions closed externally (e.g., OKX web).
                    #
                    # ⚠️ REST-first: use OKX REST (source of truth) to check position.
                    # Batch-fetched once per order_monitor cycle, NOT per trade.
                    # GUARD: only cleanup for trades marked closed or position_state=closed.
                    # Open positions should not have their SL/TP orders cancelled.
                    try:
                        if trade.position_state == "closed" or not trade.is_open:
                            if trade.pair in _cleaned_symbols:
                                _pos = runtime.get_position(trade.pair)
                                if _pos and _pos.get("contracts", 0) > 0:
                                    _cleaned_symbols.discard(trade.pair)
                            else:
                                _okx_positions = await _get_okx_positions()
                                _has_position = False
                                for _p in _okx_positions:
                                    if _p.get("symbol", "") == trade.pair and float(_p.get("contracts", 0)) > 0:
                                        _has_position = True
                                        break
                                if not _has_position:
                                    logger.warning(f"[OrderMonitor] Step0 REST: {trade.pair} 仓位不存在 (OKX返回{len(_okx_positions)}笔), 清理残留订单")
                                    from exit.protection import cleanup_trade_orders
                                    await cleanup_trade_orders(trade, session=session, exchange=trade_ex)
                                    _cleaned_symbols.add(trade.pair)
                                else:
                                    logger.debug(f"[OrderMonitor] Step0 REST: {trade.pair} 仓位确认存在, 跳过清理")
                    except Exception as _sce:
                        logger.warning(
                            f"[OrderMonitor] {trade.pair} "
                            f"cleanup exception: {_sce}"
                        )

                    # ——— Step 0.5: 交易所侧平仓实时检测 (≤5s 完整记账) ———
                    # 挂单的 SL/TP 算法单在 OKX 触发成交时本地没有成交事件, 此前只能等
                    # reconcile(10min) 标记关闭且不写 close_rate/盈亏, 价格要等午夜修复回填。
                    # 此处当 tick 检测: WS 无仓位 → REST 复核(真相源) → bookkeep_external_close
                    # 完整记账(close_rate/盈亏/CloseHistory + _pnl_estimated 标记)。
                    try:
                        _s05_state = getattr(trade, 'position_state', None) or "pending_entry"
                        if (trade.is_open and (trade.amount or 0) > 0
                                and _s05_state in ("open", "tp1_filled", "partial_tp")):
                            _ws_pos = runtime.get_position(trade.pair)
                            _ws_contracts = _ws_pos.get("contracts", 0) if _ws_pos else 0
                            if _ws_contracts <= 0:
                                # REST 复核: WS 空缓存/刚开仓事件未推送时不能误关
                                from exchange_engine.trade_executor import _normalize_pair
                                _sym_norm = _normalize_pair(trade.pair)
                                _rest_gone = False
                                try:
                                    _rest_positions = await ex.fetch_positions(exchange=trade_ex)
                                    _rest_gone = not any(
                                        _normalize_pair(str(_p.get("symbol", ""))) == _sym_norm
                                        and float(_p.get("contracts", 0) or 0) > 0
                                        for _p in _rest_positions
                                    )
                                except Exception as _pe:
                                    logger.warning(f"[监控-Step0.5] {trade_ex}:{trade.pair} REST复核失败: {_pe}, 等下轮")
                                if _rest_gone:
                                    # 分批入场保护: 有待成交的限价入场单 → 保持等待(镜像 position_sync)
                                    from core.reconciler import _is_entry_order, _is_entry_order_stale
                                    _has_pending_entry = any(
                                        o.ft_is_open and _is_entry_order(o)
                                        and not _is_entry_order_stale(o)
                                        for o in (trade.orders or [])
                                    )
                                    if _has_pending_entry:
                                        logger.info(
                                            f"[监控-Step0.5] {trade_ex}:{trade.pair} "
                                            f"OKX无仓位但有未过期入场单, 保持等待"
                                        )
                                    else:
                                        from core.reconciler import bookkeep_external_close
                                        if await bookkeep_external_close(session, trade):
                                            logger.warning(
                                                f"[监控-Step0.5] {trade_ex}:{trade.pair} "
                                                f"OKX仓位已归零(交易所侧平仓) → 实时完整记账"
                                            )
                                            continue  # 跳过本 tick 其余处理
                    except Exception as _s05e:
                        logger.warning(f"[监控-Step0.5] {trade_ex}:{trade.pair} 检测异常: {_s05e}")

                    # --- Step 1: entry order fill check ———
                    try:
                        entry_filled = await check_entry_order(trade, session)
                    except Exception as e:
                        logger.error(f"[监控-Step1] {trade_ex}:{trade.pair} {e}")
                        continue

                    # ——— Step 2: 入场首次成交 → 挂止损止盈 (v5: event-driven, once only) ———
                    try:
                        meta = trade.signal_meta or {}
                        setup_done = meta.get("_setup_done", False)
                        if entry_filled and not trade.has_open_orders and not setup_done:
                            from core.reconciler import ensure_protection
                            await ensure_protection(trade, session)
                            # _setup_done 由 ensure_protection 内部按实际结果设置
                            # 只有 SL+TP 都成功创建（或确认已存在）才会置位
                            # 失败时不置位，允许下次轮询或 EntryFillHandler 重试

                            # 记录 TP1 初始创建时的仓位（用于分批挂单 TP1 重算）
                            if meta.get("entry_strategy") == "limit_range":
                                okx_pos = runtime.get_position(trade.pair)
                                okx_contracts = okx_pos["contracts"] if okx_pos else 0
                                if okx_contracts > 0:
                                    meta["_tp1_setup_amount"] = okx_contracts
                                    trade.signal_meta = meta
                    except Exception as e:
                        logger.error(f"[监控-Step2] {trade_ex}:{trade.pair} {e}")

                    # ——— Step 2.5: pending_entry → open (timeout recovery, no repair queue) ———
                    if trade.position_state == "pending_entry" and trade.amount > 0:
                        open_time = trade.open_date or trade.open_date
                        if open_time:
                            elapsed = (datetime.now(timezone.utc) - ensure_utc(open_time)).total_seconds()
                            if elapsed > 60:
                                logger.warning(f"[{trade_ex}] {trade.pair} pending_entry 超时 ({elapsed:.0f}s)，状态转换")
                                pos = runtime.get_position(trade.pair)
                                if pos and pos["contracts"] > 0:
                                    # v5: Event-driven — ensure protection ONCE, no repair queue
                                    async with atomic_transaction(session, "pending_entry → open (超时恢复)"):
                                        trade.position_state = "open"
                                        if not trade.opened_at:
                                            trade.opened_at = datetime.now(timezone.utc)
                                    # Ensure protection immediately
                                    try:
                                        from core.reconciler import ensure_protection
                                        await ensure_protection(trade, session)
                                        logger.info(f"[{trade_ex}] {trade.pair} pending_entry → open + protection ensured")
                                    except Exception as e:
                                        logger.error(f"[监控-恢复] {trade_ex}:{trade.pair} ensure_protection 失败: {e}")
                                else:
                                    # No position — just transition state
                                    async with atomic_transaction(session, "pending_entry → open (无仓位)"):
                                        trade.position_state = "open"
                                        logger.info(f"[{trade_ex}] {trade.pair} pending_entry → open (无仓位)")

                    # ——— Step 2.6: TP1 价格检查（基于开仓价）—————
                    # 仅 OPEN 状态下运行一次。当价格达到开仓价的 +2%（多头）或 -2%（空头）
                    # 时触发：状态 → tp1_filled，SL → 保本价。
                    # ⚠️ 交易所原始 TP1 算法单负责实际平仓 30%，此处仅做状态转换。
                    # ⚠️ 使用 open_rate（开仓价）计算利润，不使用仓位比例。
                    if trade.position_state == "open" and trade.open_rate > 0:
                        try:
                            okx_pos = runtime.get_position(trade.pair)
                            okx_contracts = okx_pos["contracts"] if okx_pos else 0
                            if okx_contracts <= 0:
                                continue
                            # 从 Runtime 获取最新价格
                            ticker = await runtime.fetch_ticker(trade.pair)
                            current_price = ticker.get("last", 0)
                            if current_price <= 0:
                                continue
                            # 以开仓价计算利润
                            if trade.is_short:
                                profit_pct = (trade.open_rate - current_price) / trade.open_rate
                            else:
                                profit_pct = (current_price - trade.open_rate) / trade.open_rate
                            from core.protection_targets import teacher_tp_prices, teacher_tp_stage_confirmed
                            teacher_tps = teacher_tp_prices(trade)
                            if teacher_tps:
                                tp1_reached = teacher_tp_stage_confirmed(trade, 1, okx_contracts)
                                tp1_target = abs(teacher_tps[0] - trade.open_rate) / trade.open_rate
                            else:
                                from core.config_loader import load_config as _ld_cfg1
                                tp1_target = float(_ld_cfg1().get("risk", {}).get("tp1", {}).get("profit_pct", 0.03))
                                tp1_reached = profit_pct >= tp1_target
                            if tp1_reached:
                                logger.info(f"[TP1检查] {trade_ex}:{trade.pair} "
                                    f"开仓价={trade.open_rate:.4f} 当前价={current_price:.4f} "
                                    f"利润={profit_pct*100:.2f}% ≥ {tp1_target*100:.1f}% → TP1触发")
                                async with atomic_transaction(session, "open → tp1_filled (TP1价格检查)"):
                                    trade.position_state = "tp1_filled"
                                    trade.amount = okx_contracts
                                    trade.tp1_filled_at = datetime.now(timezone.utc)
                                    # SL → 保本价（交易所原始SL继续有效）
                                    if trade.open_rate > 0:
                                        trade.stop_loss = trade.open_rate
                                logger.info(f"[TP1检查] {trade_ex}:{trade.pair} → tp1_filled, SL保本={trade.open_rate}")
                        except Exception as e:
                            logger.warning(f"[TP1检查] {trade_ex}:{trade.pair} 检查异常: {e}")

                    # ——— Step 2.7: TP2 价格检查（基于开仓价）—————
                    # 仅 TP1_FILLED 状态下运行一次。当价格达到开仓价的 +4%（多头）或 -4%（空头）
                    # 时触发：状态 → partial_tp，激活 Trailing。
                    # ⚠️ 使用 open_rate（开仓价）计算利润，不使用仓位比例。
                    if trade.position_state == "tp1_filled" and trade.open_rate > 0:
                        try:
                            okx_pos = runtime.get_position(trade.pair)
                            okx_contracts = okx_pos["contracts"] if okx_pos else 0
                            if okx_contracts <= 0:
                                continue
                            # 从 Runtime 获取最新价格
                            ticker = await runtime.fetch_ticker(trade.pair)
                            current_price = ticker.get("last", 0)
                            if current_price <= 0:
                                continue
                            # 以开仓价计算利润
                            if trade.is_short:
                                profit_pct = (trade.open_rate - current_price) / trade.open_rate
                            else:
                                profit_pct = (current_price - trade.open_rate) / trade.open_rate
                            from core.protection_targets import teacher_tp_prices, tp_price_for, teacher_tp_stage_confirmed
                            teacher_tps = teacher_tp_prices(trade)
                            if teacher_tps:
                                tp2_reached = teacher_tp_stage_confirmed(trade, 2, okx_contracts)
                                teacher_tp2 = tp_price_for(trade, 2)
                                tp2_target = abs(teacher_tp2 - trade.open_rate) / trade.open_rate if teacher_tp2 else 0
                            else:
                                from core.config_loader import load_config as _ld_cfg2
                                tp2_target = float(_ld_cfg2().get("risk", {}).get("tp2", {}).get("profit_pct", 0.06))
                                tp2_reached = profit_pct >= tp2_target
                            if tp2_reached:
                                logger.info(f"[TP2检查] {trade_ex}:{trade.pair} "
                                    f"开仓价={trade.open_rate:.4f} 当前价={current_price:.4f} "
                                    f"利润={profit_pct*100:.2f}% ≥ {tp2_target*100:.1f}% → TP2触发")
                                async with atomic_transaction(session, "tp1_filled → partial_tp (TP2价格检查)"):
                                    trade.position_state = "partial_tp"
                                    trade.tp2_filled_at = datetime.now(timezone.utc)
                                logger.success(f"[TP2检查] {trade_ex}:{trade.pair} → partial_tp, 激活Trailing")
                                # 激活 Trailing（仅DB记录，交易所SL由TrailingChecker管理）
                                try:
                                    if current_price > 0 and trade.open_rate > 0:
                                        from core.config_loader import load_config as _ld_cfg
                                        cfg = _ld_cfg().get("trailing", {})
                                        activate_pct = cfg.get("activate_profit_pct", 0.06)
                                        if profit_pct >= activate_pct:
                                            lock_gap = cfg.get("lock_gap_pct", 0.06)
                                            sl_profit_pct = max(0.0, profit_pct - lock_gap)
                                            trailing_sl = (trade.open_rate * (1 - sl_profit_pct)
                                                if trade.is_short
                                                else trade.open_rate * (1 + sl_profit_pct))
                                            trade.trailing_activated = True
                                            trade.trailing_highest_profit_pct = profit_pct
                                            trade.trailing_highest_price = current_price
                                            trade.trailing_current_sl = trailing_sl
                                            trade.trailing_last_sl_update_at = datetime.now(timezone.utc)
                                            logger.info(f"[TP2检查] {trade_ex}:{trade.pair} Trailing激活: "
                                                f"profit={profit_pct*100:.2f}% trailing_sl={trailing_sl:.4f}")
                                except Exception as e:
                                    logger.warning(f"[TP2检查] {trade_ex}:{trade.pair} Trailing激活失败: {e}")
                        except Exception as e:
                            logger.warning(f"[TP2检查] {trade_ex}:{trade.pair} 检查异常: {e}")

                    # ——— Step 3: ExitManager 统一退出决策 ———
                    if not trade.is_open or trade.amount <= 0:
                        continue
                    try:
                        exit_result = await exit_manager.check(trade, trade_ex, session)
                    except Exception as e:
                        logger.error(f"[监控-ExitManager] {trade_ex}:{trade.pair} 检查异常: {e}")
                        continue

                    if exit_result is None or not exit_result.should_exit:
                        continue

                    # 老师 TP 由交易所条件单执行；监控只在真实仓位减少后推进状态。
                    from core.protection_targets import teacher_tp_prices
                    if teacher_tp_prices(trade) and exit_result.exit_type in ("tp1", "tp2"):
                        continue

                    exit_price = exit_result.exit_price or 0

                    # ——— TP1: 状态转换 + 保本损 ———
                    # ⚠️ 平仓 30% 由交易所 TP1 算法单负责，此处不做本地平仓，避免重复成交
                    # ⚠️ OKX 全仓止损单在减仓后依然有效 — 不需要创建新 SL！
                    #    原始的 reduce-only 止损单会在价格触发时清掉所有剩余仓位。
                    # ⚠️ TP1 不激活 Trailing — Trailing 由 TP2 在 4% 时激活
                    if exit_result.exit_type == "tp1":
                        if exit_result.move_sl_to_breakeven and trade.open_rate > 0:
                            # 仅更新 DB 中的 stop_loss 用于追踪显示
                            # 交易所上的原始 SL 单继续有效，不创建新单
                            old_sl = trade.stop_loss
                            trade.stop_loss = trade.open_rate
                            logger.info(
                                f"[{trade_ex}] {trade.pair} TP1 成交，SL 记录更新: "
                                f"{old_sl:.4f} → {trade.open_rate:.4f} (保本，交易所原始SL继续有效)"
                            )
                        # P0-4: 原子事务 — open → tp1_filled (TP1 退出)
                        async with atomic_transaction(session, "open → tp1_filled (TP1 退出)"):
                            trade.position_state = "tp1_filled"
                            trade.tp1_filled_at = datetime.now(timezone.utc)  # 记录 TP1 成交时间
                        logger.success(f"[{trade_ex}] {exit_result.reason} | 保本{trade.open_rate}")
                        # TP1 不激活 Trailing（等待 TP2 在 4% 时激活）

                    # ——— TP2: 状态转换 + 激活 Trailing ———
                    # ⚠️ 平仓剩余 50% 由交易所 TP2 算法单负责，此处不做本地平仓，避免重复成交
                    elif exit_result.exit_type == "tp2":
                        # SL 已在 TP1 时设为保本价，此处确保保本
                        if exit_result.move_sl_to_breakeven and trade.open_rate > 0:
                            trade.stop_loss = trade.open_rate
                        # P0-4: 原子事务 — tp1_filled → partial_tp (TP2 退出)
                        async with atomic_transaction(session, "tp1_filled → partial_tp (TP2 退出)"):
                            trade.position_state = "partial_tp"
                            trade.tp2_filled_at = datetime.now(timezone.utc)  # 记录 TP2 成交时间
                        logger.success(f"[{trade_ex}] {exit_result.reason} | 激活 Trailing")
                        # 激活 Trailing 状态（仅 DB，交易所 SL 由下一轮 order_monitor 更新）
                        try:
                            ticker = await ex.fetch_ticker(trade.pair, trade_ex)
                            current_price = ticker.get("last", 0)
                            if current_price > 0 and trade.open_rate > 0:
                                cfg = exit_manager.config.get("trailing", {})
                                activate_pct = cfg.get("activate_profit_pct", 0.06)
                                if trade.is_short:
                                    profit_pct = (trade.open_rate - current_price) / trade.open_rate
                                else:
                                    profit_pct = (current_price - trade.open_rate) / trade.open_rate
                                if profit_pct >= activate_pct:
                                    # 锁利间隔 4%：每涨 1%，SL 从开仓价上移 1%，回调 4% 才触发
                                    lock_gap = cfg.get("lock_gap_pct", 0.06)
                                    sl_profit_pct = max(0.0, profit_pct - lock_gap)
                                    if trade.is_short:
                                        trailing_sl = trade.open_rate * (1 - sl_profit_pct)
                                    else:
                                        trailing_sl = trade.open_rate * (1 + sl_profit_pct)
                                    # 只设置 trailing 状态字段，不修改 trade.stop_loss
                                    # stop_loss 已在上方设为保本价（trade.open_rate）
                                    # TrailingChecker 下一个 tick 会接管并更新 stop_loss
                                    trade.trailing_activated = True
                                    trade.trailing_highest_profit_pct = profit_pct
                                    trade.trailing_highest_price = current_price
                                    trade.trailing_current_sl = trailing_sl
                                    trade.trailing_last_sl_update_at = datetime.now(timezone.utc)
                                    logger.info(
                                        f"[{trade_ex}] {trade.pair} TP2 后激活 Trailing: "
                                        f"profit={profit_pct*100:.2f}% "
                                        f"trailing_sl={trailing_sl:.4f} "
                                        f"锁利间隔={lock_gap*100:.0f}% "
                                        f"(仅DB记录，stop_loss保持保本价{trade.open_rate}，"
                                        f"TrailingChecker下一tick接管)"
                                    )
                        except Exception as e:
                            logger.warning(f"[{trade_ex}] {trade.pair} TP2 后 Trailing 激活失败: {e}")

                    # ——— 其他退出: 全平 ———
                    else:
                        ok = await execute_trade_exit(
                            trade, session,
                            exit_reason=exit_result.reason,
                            ordertype="market",
                        )
                        if not ok:
                            # ⚠️ 平仓未确认(下单失败/订单未成交): 保留仓位，不标记 closed。
                            # 由下轮监控重试 / 仓位同步对账收尾。
                            # (此前无视返回值无条件假关闭 → TRIA #586 残留 amount=63、
                            #  无 CloseHistory 的脏账根源)
                            logger.warning(
                                f"[{trade_ex}] {trade.pair} 平仓执行未确认 "
                                f"({exit_result.reason})，保留仓位由下轮监控处理"
                            )
                            continue
                        # P0-3: 原子事务 — open/partial_tp → closed (全平)
                        async with atomic_transaction(session, "open/partial_tp → closed (全平)"):
                            trade.is_open = False
                            trade.exit_reason = exit_result.reason
                            trade.close_date = datetime.now(timezone.utc)
                            trade.position_state = "closed"
                        logger.success(f"[{trade_ex}] {exit_result.reason} | 全平")

                    # 写入 exit_type 字段（统计退出方式分布）
                    trade.exit_type = exit_result.exit_type

                    continue

            # 定期报告
            open_positions = [t for t in active_trades if t.is_open and t.amount > 0]
            if open_positions:
                positions_str = ", ".join(
                    f"[{t.exchange}]{t.pair} {'SHORT' if t.is_short else 'LONG'} "
                    f"entry={t.open_rate:.4f} pnl={t.calc_profit_ratio(t.open_rate)*100:+.2f}%"
                    for t in open_positions
                )
                logger.debug(f"持仓监控: {len(open_positions)} 笔 | {positions_str}")

            session.commit()

            _HEARTBEAT["monitor_last_tick"] = time.time()

        except Exception as e:
            err_msg = str(e)
            now_t = time.time()
            if err_msg != _last_error_log["msg"] or now_t - _last_error_log["time"] > 30:
                logger.exception(f"[监控循环] 异常: {e}")
                _last_error_log["time"] = now_t
                _last_error_log["msg"] = err_msg
            # 显式回滚未提交的事务
            if session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass

        finally:
            # 确保 session 在任何情况下都被关闭（异常、正常退出均需释放连接）
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass


# ———————— 信号消费者 ————————
async def signal_consumer(listener: TelegramListener, parser: SignalParser, shutdown_event: asyncio.Event):
    """
    信号消费循环：
    1. 从 Telegram 消息队列获取新消息
    2. DeepSeek 解析
    3. 风控检查
    4. 在 OKX 执行交易

    状态门控：交易所未连接时仍消费消息 + 解析 + 记录，但跳过交易执行。
    """
    logger.info("信号消费者已启动")
    ServiceRegistry.set_state(MOD_SIGNAL_CONSUMER, ModuleState.RUNNING)
    max_positions = int(os.getenv("MAX_CONCURRENT_POSITIONS", "5"))
    max_position_pct = float(os.getenv("MAX_POSITION_PCT", "10"))
    daily_max_loss_pct = float(os.getenv("DAILY_MAX_LOSS_PCT", "10"))
    min_trade = float(os.getenv("MIN_TRADE_AMOUNT_USDT", "20"))

    while not shutdown_event.is_set():
        try:
            payload = await asyncio.wait_for(listener.message_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            _HEARTBEAT["consumer_last_tick"] = time.time()
            continue

        session = None
        try:
            session = get_session()
            text = payload["text"]
            sender = payload.get("tg_sender_name", "")
            group = payload.get("tg_group_title", "")
            tg_msg_id = payload.get("tg_msg_id")
            chat_id = payload.get("tg_group_id", "")
            reply_to_id = payload.get("reply_to_msg_id")
            reply_text = payload.get("reply_text", "")
            original_signal = payload.get("original_signal", "")
            reply_chain = payload.get("reply_chain", [])
            t0 = time.perf_counter()
            logger.info(f"[计时] 收到Telegram")
            logger.info(f"来源: {group} | ChatID: {chat_id} | MsgID: {tg_msg_id}")

            # 回复上下文日志
            if reply_to_id:
                logger.info(f"[Reply Context] 检测到回复消息")
                logger.info(f"  当前消息 ID: {tg_msg_id}")
                logger.info(f"  回复至消息 ID: {reply_to_id}")
                if reply_chain:
                    logger.info(f"  回复链深度: {len(reply_chain)} 层")
                    for i, chain_msg in enumerate(reply_chain[:3]):  # 最多显示前 3 层
                        logger.info(f"    第 {i+1} 层: MsgID={chain_msg.get('msg_id')}, 内容={chain_msg.get('text', '')[:50]}...")
                if original_signal:
                    logger.info(f"  原始交易信号: {original_signal[:100]}...")
                    logger.info(f"\n{'='*50}")
                    logger.info(f"原始交易信号:")
                    logger.info(original_signal)
                    logger.info(f"\n当前回复:")
                    logger.info(text)
                    logger.info(f"{'='*50}\n")

            # Step 1: LLM 解析
            result = await parser.parse(text, sender=sender, group=group, original_signal=original_signal)
            signal = parser.standardize(result, text, sender=sender, group=group)
            logger.info(f"[计时] AI解析完成 +{time.perf_counter()-t0:.2f}s")

            # ====== 原始信号摘要 ======
            _first_line = text.strip().split("\n")[0] if text.strip() else ""
            logger.info(f"[Signal] {_first_line[:120]}")

            # ====== 消息类型驱动日志 ======
            msg_type = (result or {}).get("message_type", "UNKNOWN")
            sig_type = signal.get("signal_type", "?") if signal else "None"

            logger.info(f"消息类型: {msg_type}")
            logger.info(f"执行类型: {sig_type}")

            debug = (result or {}).get("parse_debug", {})
            if isinstance(debug, dict):
                logger.info("=" * 50)

                # ———— 识别字段（recognized）————
                rec = debug.get("recognized", {})
                if isinstance(rec, dict) and rec:
                    logger.info("识别字段:")
                    for k, v in rec.items():
                        logger.info(f"    {k:<14}{v}")
                elif isinstance(rec, str) and rec:
                    logger.info(f"识别字段(原文): {rec[:200]}")

                # ———— 推断字段（inferred）————
                inf = debug.get("inferred", {})
                if isinstance(inf, dict) and inf:
                    logger.info("推断字段:")
                    for k, v in inf.items():
                        logger.info(f"    {k:<14}{v}")
                elif isinstance(inf, str) and inf:
                    logger.info(f"推断字段(原文): {inf[:200]}")

                # ———— 总结 ————
                summary = debug.get("summary", "")
                if summary:
                    logger.info(f"总结: {summary}")
                logger.info("=" * 50)
            elif isinstance(debug, str) and debug:
                logger.info(f"AI 返回文本（期望 dict）：parse_debug={debug[:200]}")

            # ———— 写入 TelegramMessage（所有消息都持久化）————
            try:
                from database.models import TelegramMessage
                tm = TelegramMessage(
                    telegram_message_id=tg_msg_id,
                    reply_to_message_id=reply_to_id,
                    chat_id=chat_id,
                    chat_name=group,
                    sender_id=payload.get("tg_sender_id", ""),
                    sender_name=sender,
                    text=text,
                    raw_text=payload.get("text", text),
                    message_type=(result or {}).get("message_type", "chat") if signal else "chat",
                    receive_time=datetime.now(timezone.utc),
                )
                session.add(tm)
            except Exception:
                pass

            # 记录信号日志（v2: 含 tg_msg_id, reply_to_msg_id, chat_id, teacher, signal_status）
            log_entry = SignalLog(
                tg_group_id=payload.get("tg_group_id", ""),
                tg_group_title=group,
                tg_sender_name=sender,
                raw_text=text[:500],
                direction=signal.get("direction") if signal else None,
                pair=signal["symbol"] if signal else None,
                parsed_json=result,
                is_trading_signal=signal is not None,
                error=result.get("error") if result and "error" in result else None,
                # v2 新增字段
                tg_msg_id=tg_msg_id,
                reply_to_msg_id=reply_to_id,
                chat_id=chat_id,
                teacher=sender,
                signal_status="executed" if signal else "ignored",
            )
            session.add(log_entry)
            session.flush()

            # ———— 写入 TelegramMessage → SignalLog 映射（ReplyMapping）————
            # 如果这条消息是开仓信号，建立 mapping 供后续 Reply 匹配
            if signal and signal.get("signal_type") == "new" and tg_msg_id:
                try:
                    from sqlalchemy.exc import IntegrityError
                    from database.models import ReplyMapping
                    existing = session.query(ReplyMapping).filter(
                        ReplyMapping.telegram_message_id == tg_msg_id,
                        ReplyMapping.chat_id == chat_id,
                    ).first()
                    if existing is not None:
                        # 本群已处理过同 MsgID（重复投递），跳过创建，不影响信号执行
                        logger.info(f"[映射] ReplyMapping 已存在（本群重复消息），跳过创建: MsgID={tg_msg_id}")
                    else:
                        mapping = ReplyMapping(
                            telegram_message_id=tg_msg_id,
                            signal_id=log_entry.signal_id,
                            teacher=sender,
                            symbol=signal.get("symbol", ""),
                            chat_id=chat_id,
                            signal_type="new",
                        )
                        session.add(mapping)
                        session.flush()
                        logger.info(f"[映射] ReplyMapping 创建: MsgID={tg_msg_id} SignalID={log_entry.signal_id}")
                except IntegrityError:
                    # 唯一键冲突（历史遗留：该 MsgID 已在其他群建立过映射）。
                    # 回滚恢复会话并重建已暂存的消息/信号日志，绝不影响后续开仓执行。
                    session.rollback()
                    try:
                        session.add(tm)
                    except Exception:
                        pass
                    session.add(log_entry)
                    session.flush()
                    logger.warning(f"[映射] ReplyMapping 已存在（跨群 MsgID 冲突），跳过创建: MsgID={tg_msg_id}")
                except Exception as e:
                    logger.warning(f"[映射] ReplyMapping 创建失败: {e}")

            if signal is None:
                logger.info("执行结果: 信号被过滤/拒绝，不开仓")
                logger.info("=" * 50)
                continue

            sig_type = signal.get("signal_type", "new")

            # ———— BREAKOUT_ORDER / PULLBACK_ORDER ————
            if sig_type in ("breakout", "pullback"):
                msg_type_label = signal.get("message_type", sig_type.upper())
                logger.info(
                    f"执行结果: {msg_type_label} — "
                    f"当前版本暂未支持 {msg_type_label}，已忽略执行"
                )
                logger.info("=" * 50)
                session.commit()
                _HEARTBEAT["consumer_last_signal"] = time.time()
                continue

            # ———— 状态门控：交易所未连接时仅记录信号，不执行交易 ————
            # 消息已消费 + 已解析 + 已写入 SignalLog，但跳过交易执行
            if not ServiceRegistry.can_trade():
                logger.warning(
                    f"[信号消费者] 交易所未连接，跳过 {sig_type} 信号执行: "
                    f"{signal['symbol']} (来源: {group})"
                )
                log_entry.error = "SKIP: 交易所未连接"
                session.commit()
                _HEARTBEAT["consumer_last_signal"] = time.time()
                continue

            # 注入来源上下文（用于方向解析 + 来源绑定）
            signal["reply_to_msg_id"] = reply_to_id
            signal["reply_text"] = reply_text
            signal["source_chat_id"] = chat_id
            signal["source_group_name"] = group
            signal["source_message_id"] = tg_msg_id

            # ———— cancel 信号：取消挂单 ————
            if sig_type == "cancel":
                active_exchanges = ex.get_active_exchanges()
                ok_count = 0
                for trade_ex in active_exchanges:
                    ok, info = await apply_cancel_signal(session, signal, exchange=trade_ex)
                    if ok:
                        ok_count += 1
                        logger.success(f"[cancel] {trade_ex}:{signal['symbol']} | {info}")
                    else:
                        logger.warning(f"[cancel] {trade_ex}:{signal['symbol']} | {info}")
                session.commit()
                _HEARTBEAT["consumer_last_signal"] = time.time()
                continue

            # ———— update / close 信号：方向解析 → 找对应 trade ————
            if sig_type in ("update", "close"):
                active_exchanges = ex.get_active_exchanges()
                ok_count = 0
                for trade_ex in active_exchanges:
                    if sig_type == "update":
                        ok, info = await apply_update_signal(session, signal, exchange=trade_ex)
                    else:
                        ok, info = await apply_close_signal(session, signal, exchange=trade_ex)
                    if ok:
                        ok_count += 1
                        logger.success(f"[{sig_type}] {trade_ex}:{signal['symbol']} | {info}")
                    else:
                        logger.warning(f"[{sig_type}] {trade_ex}:{signal['symbol']} | {info}")
                if ok_count == 0:
                    log_entry.error = f"FAIL: 未找到 {signal['symbol']} 的活跃 trade"
                session.commit()
                _HEARTBEAT["consumer_last_signal"] = time.time()
                continue

            # ———— new 信号 ————

            # Step A: 获取可用余额（计算全局熔断用合并余额，仓位按单个交易所独立计算）
            # P2: 信号入口层过滤无效 symbol -- 在 balance fetch / risk_check / execute_entry
            # 之前校验。以 OKX markets 缓存为准（零 API），无效 symbol 直接跳过，
            # 不创建 DB trade，避免浪费 balance/risk API 调用。
            _valid_sym = None
            for _ex_cand in ex.get_active_exchanges():
                _ok, _ccxt_sym = ex.validate_symbol(signal["symbol"], exchange=_ex_cand)
                if _ok:
                    _valid_sym = _ccxt_sym
                    break
            if _valid_sym is None:
                logger.warning(f"[信号过滤] 无效 symbol，跳过开仓: {signal['symbol']}")
                log_entry.error = f"SKIP: invalid symbol {signal['symbol']}"
                log_entry.signal_status = "invalid_symbol"
                session.commit()
                _HEARTBEAT["consumer_last_signal"] = time.time()
                continue
            signal["symbol"] = _valid_sym  # 统一为 ccxt 标准符号（如 SOL/USDT:USDT）

            total_balance = 0.0
            exchange_balances = {}
            for trade_ex in ex.get_active_exchanges():
                bal = await ex.fetch_balance(exchange=trade_ex)
                exchange_balances[trade_ex] = bal
                total_balance += bal["total"]

            # ———— Step A.5: 每日熔断检查（双通道后台监控，此处只读 flag）————
            # DailyLossBreaker 在 order_monitor 中双通道评估:
            #   Channel 1 (WS):  每 5s  评估 BalanceTracker 权益
            #   Channel 2 (REST): 每 60s 主动 OKX REST 查询（绕过缓存）
            # 此处只检查 flag，不做重复计算
            from core.daily_loss_breaker import daily_loss_breaker
            daily_loss_breaker.max_loss_pct = daily_max_loss_pct / 100.0
            if daily_loss_breaker.is_tripped:
                trip_reason = daily_loss_breaker._trip_reason
                logger.warning(f"[熔断] {trip_reason}")
                if signal:
                    log_entry.error = f"BREAKER: {trip_reason}"
                    log_entry.signal_status = "blocked_breaker"
                session.commit()
                _HEARTBEAT["consumer_last_signal"] = time.time()
                continue

            # Step B: 风控检查（熔断额 = 总权益 × 百分比）
            # 传入 exchange_balances 复用已获取的余额，避免重复 fetch_balance
            daily_max_loss = total_balance * daily_max_loss_pct / 100.0
            passed, reason = await risk_check(
                session, signal["symbol"], signal.get("direction", ""),
                max_positions, daily_max_loss,
                exchange_balances=exchange_balances,
            )
            if not passed:
                logger.info(f"[风控拒绝] {signal['symbol']}: {reason}")
                continue

            # Step C: 老师月度分层仓位 —— 单笔比例由当月快照锁定
            # （core/position_tier：每月1号快照 90 天收益率榜；榜外/无快照 → 0.01 兜底）
            from core.position_tier import ratio_for as _tier_ratio_for
            teacher_ratio = _tier_ratio_for(sender)
            logger.info(
                f"[PositionSizing] 信号 teacher={sender!r} 当月锁定仓位比例={teacher_ratio:.0%}"
            )
            entry_strategy = signal.get("entry_strategy", "market")
            entry_low = signal.get("entry_low")
            entry_high = signal.get("entry_high")
            trigger_price = signal.get("trigger_price")

            # Step C: 在每个交易所执行入场（使用该交易所自己的余额计算仓位）
            logger.info(f"[计时] 开始下单 +{time.perf_counter()-t0:.2f}s")
            executed_on = []
            for trade_ex in ex.get_active_exchanges():
                ex_balance = exchange_balances.get(trade_ex, {})
                ex_free = ex_balance.get("free", 0.0)
                margin = await calculate_position_size(ex_free, teacher_ratio)

                # 每个交易所独立执行
                trade = await execute_entry(
                    session, signal["symbol"], signal["direction"],
                    margin, signal["leverage"], log_entry.signal_id,
                    exchange=trade_ex,
                    signal_meta={
                        "take_profit": signal.get("take_profit") or [],
                        "stop_loss": signal.get("stop_loss"),
                        "sl_type": signal.get("sl_type", "fixed"),
                        "entry_strategy": entry_strategy,
                        "source_sender": sender, "source_group": group,
                        "tg_msg_id": tg_msg_id,
                    },
                    entry_low=entry_low,
                    entry_high=entry_high,
                    entry_strategy=entry_strategy,
                    trigger_price=trigger_price,
                    source_chat_id=chat_id,
                    source_group_name=group,
                    source_message_id=tg_msg_id,
                )

                if trade:
                    executed_on.append(trade_ex)

                    # Step D: 老师给出有效绝对止损价时直接使用；否则先用固定止损
                    # P0-2: 限价单尚未成交时 open_rate=0，跳过 SL 计算。
                    # ensure_protection() 在成交后会从 OKX 回填真实 open_rate 并重算 SL。
                    if trade.open_rate <= 0:
                        logger.info(f"[SL延迟] {trade_ex}:{trade.pair} 限价单等待成交(open_rate=0)，"
                                    f"SL由 ensure_protection 在成交后从 OKX 回填")
                    else:
                        from core.protection_targets import positive_price
                        sig_sl = positive_price(signal.get("stop_loss"))
                        from core.config_loader import load_config as _load_sl_cfg
                        default_sl_pct = abs(_load_sl_cfg().get("risk", {}).get("default_stoploss_pct", 0.02))

                        if sig_sl is not None:
                            teacher_sl_pct = abs(sig_sl - trade.open_rate) / max(trade.open_rate, 1e-8)
                            trade.stop_loss = sig_sl
                            trade.initial_stop_loss = sig_sl
                            trade.stop_loss_pct = -teacher_sl_pct
                            trade.initial_stop_loss_pct = -teacher_sl_pct
                            if signal.get("sl_type") == "trailing":
                                trade.is_stop_loss_trailing = True
                            logger.info(f"[SL老师] {trade_ex}:{trade.pair} SL={sig_sl:.4f}")
                        else:
                            # 老师未提供 SL，使用默认 2%
                            trade.adjust_stop_loss(trade.open_rate, default_sl_pct, initial=True)
                            logger.info(f"[SL默认] {trade_ex}:{trade.pair} SL={trade.stop_loss:.4f} ({default_sl_pct*100:.1f}%)")

                    # Step E: 将老师目标价写进持仓；补挂以这些目标价为准
                    from core.protection_targets import normalize_tp_prices
                    sig_tp = normalize_tp_prices(signal.get("take_profit"))
                    if sig_tp:
                        trade.tp1_price = sig_tp[0]
                        trade.tp2_price = sig_tp[1] if len(sig_tp) > 1 else None
                        logger.info(f"[TP老师] {trade_ex}:{trade.pair} TP={sig_tp}")
                    tp_levels = []

                    # Step F: 挂 SL + TP
                    if not trade.has_open_orders:
                        # Acquire trade lock to prevent race with monitor loop
                        trade_lock = await trade_lock_manager.acquire(trade.id)
                        async with trade_lock:
                            # Re-check post-lock (protects against TOCTOU race)
                            if not trade.has_open_orders:
                                await post_entry_setup(trade, session, tp_levels)

            logger.info(f"[计时] 下单完成 +{time.perf_counter()-t0:.2f}s")
            # ====== 执行结果摘要 ======
            if executed_on:
                _entry_desc = (
                    "market" if entry_strategy == "market"
                    else f"limit@{entry_low}" if entry_strategy == "limit_single"
                    else f"range({entry_low}~{entry_high})" if entry_strategy == "limit_range"
                    else f"trigger@{trigger_price}->{entry_low}" if entry_strategy == "limit_trigger"
                    else entry_strategy
                )
                _msg_type = signal.get("message_type", "NEW")
                logger.info(
                    f"[Result] {', '.join(executed_on)} "
                    f"[{_msg_type}] "
                    f"{signal.get('direction', '?')} "
                    f"{signal.get('symbol', '?')} "
                    f"strategy={_entry_desc} "
                    f"SL={signal.get('stop_loss') or 'default_2%'} "
                    f"TP={signal.get('take_profit') or 'system_tp'}"
                    f" lever={signal['leverage']}x"
                )
            else:
                logger.warning(f"[Result] 开仓失败 {signal.get('symbol', '?')} OKX执行失败")

            session.commit()
            _HEARTBEAT["consumer_last_signal"] = time.time()

        except Exception as e:
            logger.exception(f"[消费者异常] {e}")
            if session:
                try:
                    session.rollback()
                except Exception:
                    pass
        finally:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            listener.message_queue.task_done()
            _HEARTBEAT["consumer_last_tick"] = time.time()


# ———————— 看门狗 ————————
async def watchdog(shutdown_event: asyncio.Event, listener=None, parser=None):
    """
    看门狗 — 每日 00:00 / 12:00（本地时间）检测外部依赖健康状态。
    检测项：协程健康、Telegram API、DeepSeek API、交易所 API。
    """
    logger.info("看门狗已启动（每日 00:00 / 12:00 全链路健康检测）")

    while not shutdown_event.is_set():
        # 计算到下一个检测时间（00:00 或 12:00 本地时间）
        # NOTE: 使用系统本地时间（naive datetime），仅用于调度定时
        # 不与数据库时间比较，不会引发 naive/aware 类型错误
        now = datetime.now()
        today_12 = now.replace(hour=12, minute=0, second=0, microsecond=0)
        tomorrow_0 = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        candidates = []
        if today_12 > now:
            candidates.append(today_12)
        candidates.append(tomorrow_0)
        next_target = min(candidates)
        wait_seconds = (next_target - now).total_seconds()

        logger.info(f"看门狗下次检测: {next_target.strftime('%m-%d %H:%M')} (等待 {wait_seconds/3600:.1f}h)")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            pass

        if shutdown_event.is_set():
            break

        # ———— 执行健康检测 ————
        await _health_check(listener, parser)


async def _health_check(listener=None, parser=None):
    """执行一次完整的健康检测并输出到终端。"""
    sep = "=" * 54
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"\n{sep}", f"  健康检测报告 — {now_str} UTC", sep]

    # 1. 主程序协程健康
    now = time.time()
    consumer_tick = _HEARTBEAT.get("consumer_last_tick", 0)
    monitor_tick = _HEARTBEAT.get("monitor_last_tick", 0)
    consumer_signal = _HEARTBEAT.get("consumer_last_signal", 0)

    lines.append("  🤖 协程健康:")
    if consumer_tick > 0:
        elapsed = now - consumer_tick
        lines.append(f"    - 信号消费者: {'✅ 正常' if elapsed < 600 else '❌ 异常'} (最后活动 {elapsed:.0f}s 前)")
    else:
        lines.append("    - 信号消费者: ⏸️ 尚未启动")
    if monitor_tick > 0:
        elapsed = now - monitor_tick
        lines.append(f"    - 订单监控器: {'✅ 正常' if elapsed < 600 else '❌ 异常'} (最后活动 {elapsed:.0f}s 前)")
    else:
        lines.append("    - 订单监控器: ⏸️ 尚未启动")
    lines.append(f"    - 信号处理:    {'✅ 有信号' if consumer_signal > 0 else '⏸️ 暂无信号'}")

    # 2. Telegram API
    lines.append("  📡 Telegram API:")
    if listener and listener.client:
        try:
            me = await asyncio.wait_for(listener.client.get_me(), timeout=10)
            name = me.username or me.first_name or "connected"
            lines.append(f"    - 状态: ✅ 正常 ({name})")
        except asyncio.TimeoutError:
            lines.append("    - 状态: ❌ 超时 (10s 无响应)")
        except Exception as e:
            lines.append(f"    - 状态: ❌ 异常 ({str(e)[:60]})")
    else:
        lines.append("    - 状态: ⚠️ 未连接")

    # 3. DeepSeek API
    lines.append("  🧠 DeepSeek API:")
    if parser:
        try:
            models = await asyncio.wait_for(
                parser.client.models.list(), timeout=15,
            )
            count = len(models.data) if hasattr(models, "data") else 0
            lines.append(f"    - 状态: ✅ 正常 ({count} 模型)")
        except asyncio.TimeoutError:
            lines.append("    - 状态: ❌ 超时 (15s 无响应)")
        except Exception as e:
            lines.append(f"    - 状态: ❌ 异常 ({str(e)[:60]})")
    else:
        lines.append("    - 状态: ⚠️ 未配置")

    # 4. 交易所 API
    lines.append("  💱 交易所 API:")
    for exchange_name in ex.get_active_exchanges():
        try:
            bal = await asyncio.wait_for(
                ex.fetch_balance(exchange=exchange_name), timeout=15
            )
            lines.append(f"    - {exchange_name}: ✅ 正常 (权益 {bal.get('total', 0):.0f} USDT)")
        except asyncio.TimeoutError:
            lines.append(f"    - {exchange_name}: ❌ 超时 (15s 无响应)")
        except Exception as e:
            lines.append(f"    - {exchange_name}: ❌ 异常 ({str(e)[:60]})")

    # 5. API 调用统计
    try:
        from exchange_engine.api_cache import get_api_stats
        api_stats = get_api_stats()
        if api_stats and api_stats.get("counts"):
            lines.append("  📊 API 调用统计 (运行至今):")
            for name, count in sorted(api_stats["counts"].items()):
                lines.append(f"    - {name}: {count} 次")
            lines.append(f"    缓存状态:")
            for cname, cstats in api_stats.get("caches", {}).items():
                lines.append(f"    - {cname}: {cstats['alive']}/{cstats['entries']} 活跃")
    except Exception:
        pass

    # 6. 带单群胜率报告
    try:
        from database.db import get_session
        from signal_engine.tracker import get_all_group_stats
        session = get_session()
        try:
            gs = get_all_group_stats(session, days=7)
            if gs:
                for name, g in sorted(gs.items()):
                    wr = f"{g.winning_trades/g.executed_trades*100:.0f}%" if g.executed_trades > 0 else "N/A"
                    lines.append(f"    - {name}: {g.executed_trades}笔 胜率{wr} 盈亏{g.total_pnl:+.0f}U")
            else:
                lines.append("    - 暂无交易数据")
        except Exception as e:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        logger.exception(f"[健康检测] 群组统计查询异常: {e}")
        lines.append(f"    - 获取失败: {e}")

    lines.append(f"{sep}\n")
    print("\n".join(lines))

    # 每日健康检测时自动执行状态协调
    try:
        from database.db import get_session
        from core.reconciler import reconcile
        session = get_session()
        try:
            await reconcile(session)
        except Exception as e:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"[看门狗-协调] {e}")


# ============================================================
# 生命周期管理 — 按顺序初始化 / 启动 / 关闭
# ============================================================

def _load_env_config() -> dict | None:
    """加载并验证 .env 配置。失败返回 None。"""
    tg_api_id = int(os.getenv("TG_API_ID", "0"))
    tg_api_hash = os.getenv("TG_API_HASH", "")
    tg_whitelist = os.getenv("TG_WHITELIST_SENDERS", "")
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    deepseek_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    okx_key = os.getenv("OKX_API_KEY", "")
    okx_secret = os.getenv("OKX_API_SECRET", "")
    okx_passphrase = os.getenv("OKX_PASSPHRASE", "")
    okx_testnet = os.getenv("OKX_MODE", "testnet").strip().split("#")[0].strip().lower() == "testnet"

    # 群组：优先用 .env 的 TG_TARGET_GROUPS；否则用 groups.py 按群名匹配
    env_groups = os.getenv("TG_TARGET_GROUPS", "")
    if env_groups:
        group_titles = [g.strip() for g in env_groups.split(",") if g.strip()]
        logger.info(f"使用 .env 群组列表: {len(group_titles)} 个群")
    else:
        group_titles = get_group_full_names()
        logger.info(f"使用硬编码群组列表，按群名匹配: {len(group_titles)} 个群")
        for g in SIGNAL_GROUPS:
            logger.info(f"  [{g['short']}] → {g['full']}")

    # 验证必要配置
    missing = []
    if not tg_api_id or not tg_api_hash:
        missing.append("TG_API_ID/TG_API_HASH")
    if not deepseek_key:
        missing.append("DEEPSEEK_API_KEY")
    if not group_titles:
        missing.append("TG_TARGET_GROUPS (或 signal_engine/groups.py 中配置)")
    if not okx_key or not okx_secret or not okx_passphrase:
        missing.append("OKX API Key/Secret/Passphrase")

    if missing:
        logger.error(f"缺少必要配置: {', '.join(missing)}")
        logger.error("请在 .env 文件中填写上述配置")
        return None

    return {
        "tg_api_id": tg_api_id,
        "tg_api_hash": tg_api_hash,
        "tg_whitelist": tg_whitelist,
        "deepseek_key": deepseek_key,
        "deepseek_model": deepseek_model,
        "deepseek_url": deepseek_url,
        "okx_key": okx_key,
        "okx_secret": okx_secret,
        "okx_passphrase": okx_passphrase,
        "okx_testnet": okx_testnet,
        "group_titles": group_titles,
    }


def init_database() -> bool:
    """初始化数据库。"""
    try:
        init_db()
        logger.success("[生命周期] 数据库已初始化")
        return True
    except Exception as e:
        logger.exception(f"[生命周期] 数据库初始化失败: {e}")
        return False


def init_exchange_supervisor(config: dict, shutdown_event: asyncio.Event,
                              lifecycle: AppLifecycle) -> ExchangeSupervisor:
    """
    初始化交易所 + 启动监督器。

    流程：
    1. 缓存凭据（即使首次失败，supervisor 也能重连）
    2. 启动 ExchangeSupervisor 后台监控（首次连接由 supervisor 内部完成）
    3. 注册交易所资源到生命周期
    """
    # 缓存凭据，让 supervisor 能重连
    if not hasattr(ex, '_exchange_configs') or "okx" not in ex._exchange_configs:
        ex._exchange_configs["okx"] = {
            "api_key": config["okx_key"],
            "api_secret": config["okx_secret"],
            "passphrase": config["okx_passphrase"],
            "testnet": config["okx_testnet"],
        }

    # 启动监督器（内部调用 init_exchange，不再用 ensure_future）
    supervisor = ExchangeSupervisor(shutdown_event, lifecycle)
    supervisor.start()
    return supervisor


async def init_telegram_and_services(config: dict, lifecycle: AppLifecycle):
    """
    初始化 Telegram 监听器 + 信号解析器 + 退出管理器。

    返回:
        (listener, parser, exit_manager) 或 (None, None, None) 失败时
    """
    # 信号解析器
    parser = SignalParser(
        api_key=config["deepseek_key"],
        base_url=config["deepseek_url"],
        model=config["deepseek_model"],
    )
    # 注册 parser 到生命周期（AsyncOpenAI client 需要 close）
    lifecycle.register("signal_parser", parser.close, timeout=5.0)

    # Telegram 监听器
    whitelist = [u.strip() for u in config["tg_whitelist"].split(",") if u.strip()] if config["tg_whitelist"] else []
    listener = TelegramListener(
        api_id=config["tg_api_id"], api_hash=config["tg_api_hash"],
        target_group_titles=config["group_titles"], whitelist_senders=whitelist,
    )

    # 启动 Telegram（失败 = 致命错误，无法监听信号）
    ok = await listener.start()
    if not ok:
        logger.error("[生命周期] Telegram 启动失败，无法继续")
        return None, None, None

    # 注册 Telegram 到生命周期
    lifecycle.register("telegram", listener.stop, timeout=10.0)

    # 退出管理器
    from core.config_loader import load_config as _load_bot_config
    bot_config = _load_bot_config()
    exit_manager = ExitManager(bot_config)

    return listener, parser, exit_manager


async def start_services(
    tasks: TaskManager,
    listener: TelegramListener,
    parser: SignalParser,
    exit_manager: ExitManager,
    shutdown_event: asyncio.Event,
) -> None:
    """
    启动所有后台服务（通过 TaskManager 统一管理）。

    服务列表：
    - 信号消费者：从 Telegram 队列消费消息 + 解析 + 执行交易
    - 订单监控器：ExitManager 退出决策 + 仓位同步
    - 数据库清理：每日 03:00 自动清理过期数据
    - 看门狗：健康检查 + 状态报告
    """
    # 信号消费者
    await tasks.create(
        "信号消费者",
        lambda: signal_consumer(listener, parser, shutdown_event),
    )

    # 订单监控器
    await tasks.create(
        "订单监控器",
        lambda: order_monitor(shutdown_event, exit_manager),
    )

    # 数据库清理（每日 03:00，独立 session，不影响交易）
    from core.cleanup_scheduler import run_cleanup_scheduler
    await tasks.create(
        "数据库清理",
        lambda: run_cleanup_scheduler(shutdown_event),
        autorestart=False,  # 夜间清理无需自动重启
    )

    # 月度仓位快照（每月 1 号 00:05 拉取 90 天收益率榜 → 当月档位整月锁定）
    from core.position_tier import run_monthly_snapshot_scheduler
    await tasks.create(
        "月度仓位快照",
        lambda: run_monthly_snapshot_scheduler(shutdown_event),
        autorestart=False,
    )

    # Repair Worker — processes repair queue (state machine-driven)
    from core.repair_worker import run_repair_worker
    await tasks.create(
        "修复工作器",
        lambda: run_repair_worker(shutdown_event),
    )

    # 看门狗
    await tasks.create(
        "看门狗",
        lambda: watchdog(shutdown_event, listener, parser),
        autorestart=False,  # 看门狗自身不需要自动重启
    )

    # OKX 收益率回填（pnlRatio 真相源，每 10 分钟）
    from core.okx_ratio_backfill import run_ratio_backfill

    await tasks.create(
        "OKX收益回填",
        lambda: run_ratio_backfill(shutdown_event),
        autorestart=True,
    )

    logger.success("[生命周期] 所有服务已启动")


async def shutdown_services(
    tasks: TaskManager,
    lifecycle: AppLifecycle,
    supervisor: ExchangeSupervisor,
    shutdown_event: asyncio.Event,
) -> None:
    """
    按顺序关闭所有资源（通过 AppLifecycle 统一管理）。

    关闭顺序：
    1. 设置 shutdown_event（通知所有循环退出）
    2. 取消所有 TaskManager 任务（等待结束）
    3. 停止 ExchangeSupervisor
    4. AppLifecycle.shutdown() → 按逆序释放：
       - signal_parser  → AsyncOpenAI client.close()
       - telegram       → listener.stop()
       - exchange:okx   → close_all_exchanges()
    5. 关闭数据库
    6. 通知事件桥
    """
    logger.info("[SHUTDOWN] ═══ 开始关闭 ═══")

    # 1. 通知所有循环退出
    if not shutdown_event.is_set():
        shutdown_event.set()

    # 2. 取消所有任务（等待结束，timeout=10s）
    await tasks.cancel_all(timeout=10.0)

    # 3. 停止交易所监督器
    try:
        await supervisor.stop()
    except Exception as e:
        logger.warning(f"[SHUTDOWN] ExchangeSupervisor 停止异常: {e}")

    # 4. AppLifecycle 释放所有已注册资源
    await lifecycle.shutdown()

    # 5. 关闭数据库
    try:
        close_db()
        logger.success("[SHUTDOWN]  ✔ db          已释放")
    except Exception as e:
        logger.warning(f"[SHUTDOWN]  ✗ db          释放异常: {e}")

    logger.success("[SHUTDOWN] ✅ 所有资源已释放，再见。")


def _register_shutdown_handler(
    shutdown_event: asyncio.Event,
    shutdown_fn,
) -> None:
    """注册 SIGINT / SIGTERM 信号处理（兼容 Windows）。"""
    loop = asyncio.get_running_loop()
    _shutdown_triggered = False

    async def _do_shutdown():
        nonlocal _shutdown_triggered
        if _shutdown_triggered:
            return
        _shutdown_triggered = True
        await shutdown_fn()

    def _signal_handler_sync(sig, frame):
        """Windows 同步信号处理。"""
        logger.info(f"[信号] 收到 signal={sig}")
        asyncio.ensure_future(_do_shutdown())

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(unix_signal, sig_name)
        try:
            # Unix: 异步信号处理
            loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(_do_shutdown()))
        except (NotImplementedError, RuntimeError):
            # Windows: 同步信号处理
            unix_signal.signal(sig, _signal_handler_sync)


# ———————— 主函数 ————————
async def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("自动交易机器人 (Freqtrade架构 + Telegram + DeepSeek)")
    logger.info("=" * 60)

    # ———— P0-1: 启动前源码编译自检 ————
    if not _startup_self_check():
        logger.critical("[启动自检] 核心文件编译未通过，拒绝启动")
        return

    # ———— 0. 创建全局生命周期管理器 ————
    lifecycle = AppLifecycle()

    # ———— 1. 依赖预检查 ————
    if not check_all_dependencies():
        logger.error("依赖检查失败，请按提示安装后重试")
        return

    # ———— 2. 加载配置 ————
    config = _load_env_config()
    if config is None:
        return

    # ———— 3. 初始化数据库 ————
    if not init_database():
        return
    # 注册数据库到生命周期
    lifecycle.register("db", lambda: close_db(), timeout=5.0)

    # ———— 3.5 仓位档位：确保当月快照可用 ————
    # 首次部署当天即生成当月分层；失败不阻塞启动，运行时按兜底策略(0.01)并告警，
    # 可用 `venv/bin/python -m core.position_tier --ensure` 人工补拉。
    try:
        from core.position_tier import ensure_current_month

        await ensure_current_month()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PositionTier] 启动加载当月仓位快照失败（继续启动）: {exc}")

    # ———— 4. 初始化交易所 + 监督器 ————
    shutdown_event = asyncio.Event()
    supervisor = init_exchange_supervisor(config, shutdown_event, lifecycle)

    # ———— 5. 初始化 Telegram + 解析器 + 退出管理器 ————
    listener, parser, exit_manager = await init_telegram_and_services(config, lifecycle)
    if listener is None:
        # Telegram 失败 = 致命错误，清理并退出
        await supervisor.stop()
        await lifecycle.shutdown()
        return

    # ———— 6. 等待交易所连接（非阻塞，60s 超时）————
    logger.info("[生命周期] 等待交易所连接（最多 60s）...")
    exchange_ok = await supervisor.wait_until_connected(timeout=60.0)
    if exchange_ok:
        logger.success("[生命周期] 交易所已就绪")

        # Start PositionManager (WS realtime + Watchdog fallback)
        okx_cfg = ex.get_exchange_config("okx")
        if okx_cfg:
            from exchange_engine.exchange import init_position_manager
            await init_position_manager(
                okx_cfg["api_key"], okx_cfg["api_secret"],
                okx_cfg["passphrase"], "okx",
            )

            # 注册 EventBus 事件驱动 SL/TP 处理器
            # EntryFillHandler 订阅 position_update，在仓位出现的第一时间
            # （毫秒级）触发 ensure_protection，无需等待轮询周期
            from core.entry_fill_handler import entry_fill_handler  # noqa: F401
            logger.success("[生命周期] EntryFillHandler 已注册 (WS 事件驱动 SL/TP)")


        # 仓位同步（以交易所为准）
        session = get_session()
        try:
            from exit.position_sync import sync_all_positions
            # 先刷新 Position Snapshot（一次 fetch_positions() 获取全部仓位）
            # Sync from PositionManager cache (REST init already done)
            synced = await sync_all_positions(session)
            logger.info(f"仓位同步完成: {len(synced)} 笔活跃持仓")
        except Exception as e:
            logger.warning(f"仓位同步异常: {e}")
        finally:
            session.close()

        # 同步未成交挂单（重启恢复用）
        try:
            for trade_ex in ex.get_active_exchanges():
                open_orders = await ex.fetch_open_orders(exchange=trade_ex)
                if open_orders:
                    logger.info(f"[启动恢复] {trade_ex} 未成交订单: {len(open_orders)} 笔")
                    for o in open_orders[:5]:
                        logger.info(f"  {o.get('symbol')} {o.get('side')} {o.get('type')} {o.get('amount')} @ {o.get('price')}")
        except Exception as e:
            logger.warning(f"[启动恢复] 查询未成交订单失败: {e}")

        # ── 启动时全量验证所有 OPEN Trade 的保护单 ──
        # 核心原则: Exchange is the source of truth, SQLite is only a cache
        # BugFix: Wait for WS data streams to fully sync before verifying.
        # Without this delay, verify may see stale WS tracker state and
        # incorrectly transition protection to FAILED, triggering duplicate SL/TP.
        try:
            session = get_session()
            try:
                from exit.protection import startup_verify_all_trades
                # Allow WS trackers (positions, orders, algo orders) to populate
                await asyncio.sleep(5)
                verify_result = await startup_verify_all_trades(session)
                if verify_result["failed"] > 0:
                    logger.warning(
                        f"[启动验证] {verify_result['failed']} 笔 Trade 保护单验证失败，"
                        f"将在监控循环中继续重试"
                    )
                elif verify_result["repaired"] > 0:
                    logger.success(
                        f"[启动验证] 修复了 {verify_result['repaired']} 笔 Trade 的保护单"
                    )
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[启动验证] 全量验证异常: {e}")

        # 打印账户权益
        for trade_ex in ex.get_active_exchanges():
            try:
                bal = await ex.fetch_balance(exchange=trade_ex)
                logger.info(f"[{trade_ex}] 权益={bal['total']:.2f}USDT")
            except Exception as e:
                logger.warning(f"[{trade_ex}] 获取余额失败: {e}")
    else:
        logger.warning("[生命周期] 交易所未就绪，仅运行 Telegram 监听（后台自动重连）")

    # ———— 7. 启动后台服务 ————
    tasks = TaskManager()
    await start_services(tasks, listener, parser, exit_manager, shutdown_event)

    # ———— 8. 注册信号处理 ————
    async def _shutdown():
        await shutdown_services(tasks, lifecycle, supervisor, shutdown_event)

    _register_shutdown_handler(shutdown_event, _shutdown)

    logger.success("=" * 60)
    logger.success("机器人启动完成，开始运行")
    logger.success("=" * 60)

    # ———— 退出系统审计日志 ————
    logger.info("[EXIT SYSTEM CHECK] ═══ 退出系统检查 ═══")
    logger.info("[EXIT SYSTEM CHECK] 状态机:   PENDING_ENTRY → OPEN → TP1_FILLED → PARTIAL_TP → CLOSED")
    logger.info("[EXIT SYSTEM CHECK] OPEN:       StopLoss + TP1 + ROI(48h后) + MaxHold(7天)")
    logger.info("[EXIT SYSTEM CHECK] TP1_FILLED: StopLoss + TP2 + ROI(48h后) + MaxHold(7天)")
    logger.info("[EXIT SYSTEM CHECK] PARTIAL_TP: StopLoss + Trailing + ROI(48h后) + MaxHold(7天)")
    logger.info("[EXIT SYSTEM CHECK] 依赖关系: StopLoss=始终 | TP1=仅OPEN | TP2=仅TP1_FILLED | Trailing=仅PARTIAL_TP")
    logger.info("[EXIT SYSTEM CHECK]            ROI=基于时间不依赖TP1/TP2 | MaxHold=基于时间不依赖TP1/TP2")
    _risk = exit_manager.config.get("risk", {})
    _tp1 = _risk.get("tp1", {})
    _tp2 = _risk.get("tp2", {})
    logger.info(f"[EXIT SYSTEM CHECK] TP1:       enabled (+{_tp1.get('profit_pct', 0.03)*100:.0f}%, {_tp1.get('close_pct', 30):.0f}% close, 保本损)")
    logger.info(f"[EXIT SYSTEM CHECK] TP2:       enabled (+{_tp2.get('profit_pct', 0.06)*100:.0f}%, {_tp2.get('close_pct', 50):.0f}% of remaining close, 激活Trailing)")
    logger.info(f"[EXIT SYSTEM CHECK] SL:        enabled (老师价格优先，缺失用固定{exit_manager.config.get('risk', {}).get('default_stoploss_pct', 0.02)*100:.0f}%)")
    logger.info("[EXIT SYSTEM CHECK] teacher_tp: enabled (单目标全平，双目标分批)")
    logger.info("[EXIT SYSTEM CHECK] ══════════════════════════")

    # ———— 10. 主循环 + try/finally 保证清理 ————
    try:
        await shutdown_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        # ———— 11. 清理退出（无论是否异常都会执行）————
        await _shutdown()
        logger.info("程序已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已退出")
    finally:
        # 兜底：确保所有数据库连接被释放（无论正常/异常退出）
        try:
            from database.db import close_db as _safe_close_db
            _safe_close_db()
        except Exception:
            pass
