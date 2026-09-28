"""
Protection Creator v5 — SINGLE entry point for ALL SL/TP creation.

OKX IS THE ONLY SOURCE OF TRUTH.

Every creation is PRE-FLIGHT verified via OKX REST API:
  1. REST fetch_pending_algo_orders(symbol) → check if SL/TP already exists
  2. If exists + correct → SKIP (no API call)
  3. If exists + wrong → NOT handled here (caller must cancel first)
  4. If missing → create with deterministic clientOrderId

Database NEVER stores "actual state" (has_sl, has_tp, protection_installed).
Database ONLY stores desired state (target prices) and reference algo_ids.

Idempotency:
  - clientOrderId = deterministic(trade_id, algo_type, version)
  - OKX deduplicates by clOrdId within a time window
  - Same trade_id + algo_type → same clOrdId → OKX returns existing order

This is the ONLY module allowed to create SL/TP algo orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade, Order
from core.exchange_runtime import runtime

L = logger.bind(module="protection_creator")


# ———— Result Types ————

@dataclass
class CreateResult:
    """Result of creating a protection order."""
    success: bool
    algo_id: str = ""
    algo_key: str = ""      # "sl" | "tp1"
    error: str = ""


# ———— Helpers ————

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_symbol(s: str) -> str:
    """Normalize to OKX instId: BTC-USDT-SWAP"""
    s = (s or "").upper().replace("/", "").replace(":USDT", "")
    if not s.endswith("USDT"):
        return s
    return f"{s[:-4]}-USDT-SWAP"


def _normalize_amount(symbol: str, raw_amount: float, exchange: str = "okx") -> float | None:
    from exchange_engine.exchange import normalize_order_amount
    return normalize_order_amount(symbol, raw_amount, exchange)


def _client_order_id(trade_id: int, order_type: str, version: int = 0) -> str:
    """Deterministic, idempotent clientOrderId. Max 32 chars for OKX."""
    return f"bot{order_type}{trade_id}v{version}"[:32]


# ============================================================================
# REST Pre-Flight Verification
# ============================================================================

async def _rest_fetch_algo_orders_for_symbol(inst_id: str) -> list[dict]:
    """
    Fetch pending algo orders from OKX REST. RAISES on API failure.
    Uses fetch_pending_algo_orders (now fixed to raise instead of return []).
    """
    from exchange_engine.exchange import fetch_pending_algo_orders
    # Normalize to short form: "BTC-USDT-SWAP" → "BTCUSDT"
    short = inst_id.upper().replace("/", "").replace(":USDT", "").replace("-USDT-SWAP", "USDT")
    return await fetch_pending_algo_orders(short, exchange="okx")


async def _rest_fetch_algo_by_id(algo_id: str, symbol: str) -> dict | None:
    """Fetch a specific algo order by algoId from OKX REST."""
    from exchange_engine.exchange import fetch_algo_order_by_id
    try:
        return await fetch_algo_order_by_id(algo_id, symbol, exchange="okx")
    except Exception:
        return None


def _parse_clordid_type(cl_ord_id: str) -> str | None:
    """
    Parse clientOrderId to determine order type.
    Format: bot{tp|sl|tpN}{trade_id}v{version}
    Examples: bottp1173v0 → takeprofit, botsl1173v1 → stoploss
    Returns "stoploss", "takeprofit", or None.
    """
    if not cl_ord_id or not cl_ord_id.startswith("bot"):
        return None
    import re
    rest = cl_ord_id[3:]
    m = re.match(r'^(sl|tp\d*)\d+v\d+', rest)
    if m:
        t = m.group(1)
        if t == "sl":
            return "stoploss"
        if t.startswith("tp"):
            return "takeprofit"
    return None


def _classify_algo_type(raw: dict) -> str | None:
    """
    Classify an OKX algo order as 'sl', 'tp', 'trailing', or None.
    Uses trigger price fields (NOT ordType) — OKX returns ordType="conditional"
    for both SL and TP; the actual type is determined by slTriggerPx / tpTriggerPx.

    v7: Use clOrdId as fallback when slTriggerPx/tpTriggerPx are both absent.
    OKX batch API may omit these fields; clOrdId format is bot{tp|sl}{id}v{ver}.

    Mirrors the classification logic in core/reconciler.py:_classify_algo_order().
    """
    state = raw.get("state", "")
    if state != "live":
        return None
    ord_type = (raw.get("ordType") or "").lower()
    sl_trigger = raw.get("slTriggerPx") or raw.get("slTriggerPx", "")
    tp_trigger = raw.get("tpTriggerPx") or raw.get("tpTriggerPx", "")
    callback_spread = raw.get("callbackSpread") or raw.get("callbackSpread", "")

    if ord_type in ("move_order_stop", "trailing_stop") or callback_spread:
        return "trailing"
    if sl_trigger or ord_type in ("stoploss", "stop"):
        return "sl"
    if tp_trigger or ord_type == "takeprofit":
        return "tp"
    # v7: check clOrdId before falling back to SL (conservative)
    if ord_type == "conditional":
        cl_ord_id = raw.get("clOrdId", "") or raw.get("clientOrderId", "")
        parsed = _parse_clordid_type(cl_ord_id)
        if parsed == "takeprofit":
            return "tp"
        if parsed == "stoploss":
            return "sl"
        return "sl"  # Fallback: conservative
    return None


async def _okx_has_live_sl(inst_id: str, expected_price: float = 0.0) -> tuple[bool, str]:
    """
    REST check: does OKX have a live SL for this symbol at the expected price?

    Returns (price_ok, algo_id):
      - (True, algo_id):  SL exists AND price matches → skip creation
      - (False, ""):      No matching SL → safe to create (old SLs at different
                           prices are intentionally left alone — Reconciler dedup
                           will clean them up every 10 minutes)

    RAISES on API failure — caller MUST skip creation.
    """
    orders = await _rest_fetch_algo_orders_for_symbol(inst_id)

    for o in orders:
        if _classify_algo_type(o) == "sl":
            algo_id = o.get("algoId", "")
            trigger = float(o.get("triggerPx", 0))
            if expected_price > 0 and trigger > 0 and abs(trigger - expected_price) > 0.5:
                # Price doesn't match → skip this SL, keep looking for exact match
                # Old SL intentionally left alone (Reconciler dedup handles cleanup)
                continue
            return True, algo_id
    return False, ""


async def _okx_has_live_tp(inst_id: str, expected_price: float = 0.0) -> tuple[bool, str]:
    """
    REST check: does OKX have a live TP for this symbol at the expected price?

    Uses tpTriggerPx field to distinguish TP from SL (OKX uses ordType="conditional" for both).
    When expected_price > 0, matches by trigger price (±0.5% tolerance) to distinguish
    TP1 (2%) from TP2 (4%) orders.

    Returns (exists, algo_id).

    RAISES on API failure — caller MUST skip creation.
    """
    orders = await _rest_fetch_algo_orders_for_symbol(inst_id)

    for o in orders:
        if _classify_algo_type(o) == "tp":
            tp_trigger = float(o.get("tpTriggerPx", 0) or 0)
            if expected_price > 0 and tp_trigger > 0:
                # Match by trigger price with 0.5% tolerance
                if abs(tp_trigger - expected_price) / max(tp_trigger, 0.001) > 0.005:
                    continue  # Price doesn't match — this is a different TP (TP1 vs TP2)
            return True, o.get("algoId", "")
    return False, ""


# ============================================================================
# ProtectionCreator
# ============================================================================

class ProtectionCreator:
    """
    THE ONLY module allowed to create SL/TP algo orders.

    Every create is PRE-FLIGHT verified via OKX REST.
    Uses deterministic clientOrderId for idempotency.
    """

    # In-memory cooldown (rate limiting, NOT state tracking)
    _last_create_attempt: dict[int, dict[str, float]] = {}  # trade_id → {algo_key → timestamp}

    # clOrdId version tracking (for replacement orders)
    _algo_versions: dict[int, dict[str, int]] = {}  # trade_id → {algo_key → version}

    @classmethod
    def _get_clordid_version(cls, trade_id: int, algo_key: str) -> int:
        return cls._algo_versions.get(trade_id, {}).get(algo_key, 0)

    @classmethod
    def bump_clordid_version(cls, trade_id: int, algo_key: str) -> int:
        """Increment version for a deliberate replacement order."""
        if trade_id not in cls._algo_versions:
            cls._algo_versions[trade_id] = {}
        new_v = cls._algo_versions[trade_id].get(algo_key, 0) + 1
        cls._algo_versions[trade_id][algo_key] = new_v
        return new_v

    @classmethod
    def _check_cooldown(cls, trade_id: int, algo_key: str, cooldown_s: float = 60.0) -> bool:
        last = cls._last_create_attempt.get(trade_id, {}).get(algo_key, 0.0)
        return (_utc_now().timestamp() - last) >= cooldown_s

    @classmethod
    def _set_cooldown(cls, trade_id: int, algo_key: str) -> None:
        if trade_id not in cls._last_create_attempt:
            cls._last_create_attempt[trade_id] = {}
        cls._last_create_attempt[trade_id][algo_key] = _utc_now().timestamp()

    # ————————————————————————————————————————————————
    # create_sl
    # ————————————————————————————————————————————————

    @classmethod
    async def create_sl(cls, trade: Trade, session: Session) -> CreateResult:
        """
        Create a Stop Loss algo order on OKX.

        PRE-FLIGHT: REST check that no live SL exists for this symbol.
        Uses deterministic clientOrderId for idempotency.

        On success: saves algo_id to trade.sl_algo_id (reference only, not "truth").
        """
        algo_key = "sl"
        trade_ex = trade.exchange or "okx"
        inst_id = _norm_symbol(trade.pair)

        # ———— Guard 1: Trade must be open ————
        if not trade.is_open or trade.amount <= 0:
            return CreateResult(success=False, algo_key=algo_key,
                              error="Trade closed or no position")

        # ———— Guard 2: Must have stop_loss price ————
        if trade.stop_loss <= 0:
            return CreateResult(success=False, algo_key=algo_key,
                              error="No stop_loss price set")

        # ———— Guard 3: In-memory cooldown (rate limiting) ————
        if not cls._check_cooldown(trade.id, algo_key):
            return CreateResult(success=False, algo_key=algo_key,
                              error="Cooldown active (60s)")

        # ———— Guard 4: Real position on OKX (REST verified) ————
        from exchange_engine.exchange import fetch_positions
        try:
            positions = await fetch_positions(exchange=trade_ex)
        except Exception:
            return CreateResult(success=False, algo_key=algo_key,
                              error="OKX API failure — cannot verify position")

        real_contracts = 0.0
        for p in positions:
            if _norm_symbol(p.get("symbol", "")) == inst_id:
                real_contracts = float(p.get("contracts", 0))
                break

        if real_contracts <= 0:
            return CreateResult(success=False, algo_key=algo_key,
                              error="No real position on OKX")

        # ———— Guard 5: PRE-FLIGHT REST check — SL already exists? ————
        try:
            price_ok, existing_algo_id = await _okx_has_live_sl(inst_id, trade.stop_loss)
        except Exception:
            # API failure → skip, don't assume missing
            return CreateResult(success=False, algo_key=algo_key,
                              error="OKX API failure during pre-flight check")

        if price_ok:
            L.info(f"[CreateSL] Trade={trade.id} {trade.pair} SL already exists on OKX "
                   f"(algoId={existing_algo_id}), SKIPPING create")
            # Update reference in DB (for tracking, not as "truth")
            if existing_algo_id and existing_algo_id != trade.sl_algo_id:
                trade.sl_algo_id = existing_algo_id
                session.flush()
            return CreateResult(success=True, algo_key=algo_key,
                              algo_id=existing_algo_id)

        # v6: SL at different price → intentionally NOT cancelled here.
        # Old SLs serve as safety net. Reconciler Phase D dedup (every 10 min)
        # is the ONLY path that cleans up extra SLs.
        # Callers that need atomic replace (e.g. _replace_exchange_sl for trailing)
        # cancel old SLs themselves BEFORE calling create_sl().

        # ———— Guard 6: Amount validation ————
        sl_amt = _normalize_amount(trade.pair, real_contracts, trade_ex)
        if sl_amt is None or sl_amt <= 0:
            L.info(f"[CreateSL] Trade={trade.id} position too small for SL ({real_contracts})")
            return CreateResult(success=False, algo_key=algo_key,
                              algo_id="skipped_too_small",
                              error="Position too small for SL")

        # ———— Record attempt ————
        cls._set_cooldown(trade.id, algo_key)

        # ———— CREATE ————
        version = cls._get_clordid_version(trade.id, algo_key)
        cl_ord_id = _client_order_id(trade.id, "sl", version)

        try:
            sl_order = await runtime.create_algo_order(
                "stoploss", trade.pair, trade.exit_side, sl_amt, trade.stop_loss,
                client_order_id=cl_ord_id,
            )
        except Exception as e:
            err_str = str(e)
            L.error(f"[CreateSL] Trade={trade.id} {trade.pair} create failed: {err_str[:120]}")
            return CreateResult(success=False, algo_key=algo_key, error=err_str[:120])

        # ———— Save reference ————
        algo_id = sl_order.get("id", "")
        if not algo_id:
            return CreateResult(success=False, algo_key=algo_key,
                              error="No algoId in OKX response")

        trade.sl_algo_id = algo_id

        # Record order in DB
        order_obj = Order.parse_from_ccxt(sl_order, trade.pair, "stoploss")
        order_obj.ft_order_role = "stoploss"
        order_obj.ft_order_tag = "stoploss"
        trade.orders.append(order_obj)

        session.flush()

        L.success(f"[CreateSL] Trade={trade.id} {trade.pair} algoId={algo_id} "
                 f"stop={trade.stop_loss} amount={sl_amt} clOrdId={cl_ord_id}")

        return CreateResult(success=True, algo_key=algo_key, algo_id=algo_id)

    # ————————————————————————————————————————————————
    # create_tp
    # ————————————————————————————————————————————————

    @classmethod
    async def create_tp(cls, trade: Trade, session: Session,
                        tp_index: int = 1) -> CreateResult:
        """
        Create a Take Profit algo order on OKX.

        TP1: 30% of position at +2% profit
        TP2: 35% of position at +4% profit (50% of remaining 70%)

        PRE-FLIGHT: REST check that no matching TP exists for this price.
        Uses deterministic clientOrderId for idempotency.

        On success: saves algo_id to trade.tp{tp_index}_algo_id (reference only).
        """
        algo_key = f"tp{tp_index}" if tp_index > 1 else "tp1"
        from core.protection_targets import tp_price_for, single_teacher_tp
        if tp_price_for(trade, tp_index) is None:
            return CreateResult(success=False, algo_key=algo_key,
                              error="Teacher did not provide this TP level")
        trade_ex = trade.exchange or "okx"
        inst_id = _norm_symbol(trade.pair)

        # ———— Guard 1: Trade must be open ————
        if not trade.is_open or trade.amount <= 0:
            return CreateResult(success=False, algo_key=algo_key,
                              error="Trade closed or no position")

        # ———— Guard 1.5: partial_tp 状态下不创建任何 TP ————
        # TP1_FILLED 状态允许创建 TP2（TP1 已完成，TP2 待挂）
        if getattr(trade, 'position_state', None) == "partial_tp":
            return CreateResult(success=False, algo_key=algo_key,
                              error="Position in partial_tp state, TP not needed")
        if getattr(trade, 'position_state', None) == "tp1_filled" and tp_index == 1:
            return CreateResult(success=False, algo_key=algo_key,
                              error="TP1 already filled")

        # ———— Guard 2: In-memory cooldown ————
        if not cls._check_cooldown(trade.id, algo_key):
            return CreateResult(success=False, algo_key=algo_key,
                              error="Cooldown active (60s)")

        # ———— Guard 3: Real position on OKX (REST) ————
        from exchange_engine.exchange import fetch_positions
        try:
            positions = await fetch_positions(exchange=trade_ex)
        except Exception:
            return CreateResult(success=False, algo_key=algo_key,
                              error="OKX API failure")

        real_contracts = 0.0
        for p in positions:
            if _norm_symbol(p.get("symbol", "")) == inst_id:
                real_contracts = float(p.get("contracts", 0))
                break

        if real_contracts <= 0:
            return CreateResult(success=False, algo_key=algo_key,
                              error="No real position on OKX")

        # ———— Calculate TP price ————
        # 止盈比例以 config.json 为准（当前 TP1=3%、TP2=6%）；缺失时退回一致默认值
        from core.config_loader import load_config as _load_cfg
        _tp_cfg = _load_cfg().get("risk", {}).get(f"tp{tp_index}", {})
        tp_profit_pct = float(_tp_cfg.get("profit_pct", 0.03 if tp_index == 1 else 0.06))

        tp_price = tp_price_for(trade, tp_index) or 0.0
        if tp_price <= 0 and trade.open_rate > 0:
            tp_price = trade.open_rate * (1 - tp_profit_pct) if trade.is_short else trade.open_rate * (1 + tp_profit_pct)
            setattr(trade, f"tp{tp_index}_price", tp_price)

        if tp_price <= 0:
            ticker = await runtime.fetch_ticker(trade.pair)
            base = ticker.get("last", 0)
            if base > 0:
                tp_price = base * (1 - tp_profit_pct) if trade.is_short else base * (1 + tp_profit_pct)
                setattr(trade, f"tp{tp_index}_price", tp_price)

        if tp_price <= 0:
            return CreateResult(success=False, algo_key=algo_key,
                              error="Cannot determine TP price")

        # ———— Guard 4: PRE-FLIGHT REST check — TP at this price already exists? ————
        try:
            has_tp, existing_algo_id = await _okx_has_live_tp(inst_id, tp_price)
        except Exception:
            return CreateResult(success=False, algo_key=algo_key,
                              error="OKX API failure during pre-flight check")

        if has_tp:
            L.info(f"[CreateTP] Trade={trade.id} {trade.pair} TP{tp_index} already exists on OKX "
                   f"(algoId={existing_algo_id}), SKIPPING create")
            algo_id_field = f"tp{tp_index}_algo_id" if tp_index > 1 else "tp1_algo_id"
            existing = getattr(trade, algo_id_field, None)
            if existing_algo_id and existing_algo_id != existing:
                setattr(trade, algo_id_field, existing_algo_id)
                session.flush()
            return CreateResult(success=True, algo_key=algo_key,
                              algo_id=existing_algo_id)

        # ———— Calculate TP amount ————
        # TP1: 30% of current position（open 初始挂单 = 全仓）
        # TP2: 35% of current position（open 初始挂单时全仓 ×35% = 剩余70%的一半）
        #      ⚠️ 补挂发生在 tp1_filled 状态（TP1 已平 30%，仓位 ≈ 剩余 70%）
        #      → 数量应为当前仓位的 50%，不是 35%
        #      （TRIA #586: 曾一律按 35% → 修复时 96 张只挂 33 张）
        if single_teacher_tp(trade):
            tp_ratio = 1.0
        elif tp_index == 1:
            tp_ratio = 0.30
        elif getattr(trade, 'position_state', None) == "tp1_filled":
            tp_ratio = 0.50  # TP1 已完成 → 当前仓位即剩余仓，平其一半（平后剩 50%×70%=原仓 35% 归 Trailing）
        else:
            tp_ratio = 0.35
        tp_qty = _normalize_amount(trade.pair, real_contracts * tp_ratio, trade_ex)
        L.info(f"[CreateTP] Trade={trade.id} {trade.pair} TP{tp_index} qty计算: contracts={real_contracts} "
               f"ratio={tp_ratio} state={getattr(trade, 'position_state', None)} → qty={tp_qty}")
        if tp_qty is None or tp_qty <= 0:
            L.info(f"[CreateTP] Trade={trade.id} position too small to split for TP{tp_index}")
            return CreateResult(success=False, algo_key=algo_key,
                              algo_id="skipped_too_small",
                              error="Position too small for TP")

        # ———— Record attempt ————
        cls._set_cooldown(trade.id, algo_key)

        # ———— CREATE ————
        version = cls._get_clordid_version(trade.id, algo_key)
        cl_ord_id = _client_order_id(trade.id, f"tp{tp_index}", version)

        try:
            tp_order = await runtime.create_algo_order(
                "takeprofit", trade.pair, trade.exit_side, tp_qty, tp_price,
                client_order_id=cl_ord_id,
            )
        except Exception as e:
            err_str = str(e)
            L.error(f"[CreateTP] Trade={trade.id} {trade.pair} TP{tp_index} create failed: {err_str[:120]}")
            return CreateResult(success=False, algo_key=algo_key, error=err_str[:120])

        # ———— Save reference ————
        algo_id = tp_order.get("id", "")
        if not algo_id:
            return CreateResult(success=False, algo_key=algo_key,
                              error="No algoId in OKX response")

        if tp_index == 1:
            trade.tp1_algo_id = algo_id
        elif tp_index == 2:
            trade.tp2_algo_id = algo_id

        order_obj = Order.parse_from_ccxt(tp_order, trade.pair, trade.exit_side)
        order_obj.ft_order_role = "tp"
        order_obj.ft_order_tag = f"tp_{tp_index}"
        trade.orders.append(order_obj)

        session.flush()

        L.success(f"[CreateTP] Trade={trade.id} {trade.pair} TP{tp_index} algoId={algo_id} "
                 f"price={tp_price} amount={tp_qty} clOrdId={cl_ord_id}")

        return CreateResult(success=True, algo_key=algo_key, algo_id=algo_id)


# Global singleton
protection_creator = ProtectionCreator()
