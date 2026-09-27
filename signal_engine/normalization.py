"""
信号标准化模块 — 统一处理入场价格、止损、止盈等字段。

职责：
1. 自动交换：确保 entry_low <= entry_high
2. 兼容旧字段：price/price2/min_price/max_price → entry_low/entry_high
3. 完整校验：None/NaN/Infinity/<=0 检查
4. 区间宽度检查：BTC > 15% 报警，山寨 > 30% 报警
5. 统一日志格式

使用方式：
    from signal_engine.normalization import normalize_signal

    normalized = normalize_signal(signal, symbol="BTCUSDT")
    if not normalized:
        # 校验失败，拒绝下单
        return
"""
import math
from loguru import logger


class NormalizationError(Exception):
    """标准化错误"""
    pass


def normalize_signal(signal: dict, symbol: str = "") -> dict | None:
    """
    统一标准化信号中的入场价格。

    职责：
    1. 自动交换：entry_low > entry_high 时交换
    2. 兼容旧字段：price/price2/min_price/max_price → entry_low/entry_high
    3. 完整校验：None/NaN/Infinity/<=0
    4. 区间宽度检查：BTC > 15% 报警，山寨 > 30% 报警

    Args:
        signal: 原始信号字典，包含 entry_low/entry_high 或旧字段
        symbol: 币种（用于区间宽度检查）

    Returns:
        标准化后的信号字典，包含 entry_low/entry_high
        校验失败返回 None

    示例：
        >>> signal = {"entry_low": 61588, "entry_high": 60888}
        >>> normalized = normalize_signal(signal, symbol="BTCUSDT")
        >>> normalized["entry_low"], normalized["entry_high"]
        (60888, 61588)
    """
    if not signal:
        logger.warning(f"[标准化] 信号为空")
        return None

    # 复制信号，避免修改原对象
    normalized = signal.copy()

    # 提取入场价格（兼容新旧字段）
    entry_low = _extract_entry_low(signal)
    entry_high = _extract_entry_high(signal)
    entry_raw = signal.get("entry_raw", "")

    # 记录原始值（用于日志）
    original_low = entry_low
    original_high = entry_high

    # ========== 校验 1: None 检查 ==========
    if entry_low is None and entry_high is None:
        # 市价单允许没有入场价格
        strategy = signal.get("entry_strategy", "market")
        if strategy == "market":
            normalized["entry_low"] = None
            normalized["entry_high"] = None
            return normalized
        else:
            logger.error(f"[标准化] {symbol} 限价单缺少入场价格: entry_low=None, entry_high=None")
            return None

    # ========== 校验 2: NaN 检查 ==========
    if entry_low is not None and (isinstance(entry_low, float) and math.isnan(entry_low)):
        logger.error(f"[标准化] {symbol} entry_low 为 NaN")
        return None
    if entry_high is not None and (isinstance(entry_high, float) and math.isnan(entry_high)):
        logger.error(f"[标准化] {symbol} entry_high 为 NaN")
        return None

    # ========== 校验 3: Infinity 检查 ==========
    if entry_low is not None and (isinstance(entry_low, float) and math.isinf(entry_low)):
        logger.error(f"[标准化] {symbol} entry_low 为 Infinity")
        return None
    if entry_high is not None and (isinstance(entry_high, float) and math.isinf(entry_high)):
        logger.error(f"[标准化] {symbol} entry_high 为 Infinity")
        return None

    # ========== 校验 4: <= 0 检查 ==========
    if entry_low is not None and entry_low <= 0:
        logger.error(f"[标准化] {symbol} entry_low <= 0: {entry_low}")
        return None
    if entry_high is not None and entry_high <= 0:
        logger.error(f"[标准化] {symbol} entry_high <= 0: {entry_high}")
        return None

    # ========== 校验 5: 自动交换 ==========
    swapped = False
    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        logger.warning(
            f"[标准化] {symbol} 入场价格自动交换: "
            f"{entry_low} > {entry_high} → {entry_high} < {entry_low}"
        )
        entry_low, entry_high = entry_high, entry_low
        swapped = True

    # ========== 校验 6: 区间宽度检查 ==========
    if entry_low is not None and entry_high is not None:
        mid = (entry_low + entry_high) / 2
        width = (entry_high - entry_low) / mid
        width_pct = width * 100

        # 判断是否为主流币
        is_major = symbol.upper() in ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "SOLUSDT")
        threshold = 15.0 if is_major else 30.0

        if width_pct > threshold:
            logger.warning(
                f"[标准化] {symbol} 入场区间宽度过大: "
                f"{entry_low}-{entry_high} (宽度={width_pct:.1f}%, 阈值={threshold}%)"
            )

    # ========== 输出标准化日志 ==========
    if swapped or entry_raw:
        logger.info(
            f"\n{'=' * 28}\n"
            f"Normalized Signal\n"
            f"Teacher Entry: {entry_raw}\n"
            f"Normalized: {entry_low}-{entry_high}\n"
            f"entry_low: {entry_low}\n"
            f"entry_high: {entry_high}\n"
            f"Strategy: {signal.get('entry_strategy', 'market')}\n"
            f"{'=' * 28}"
        )

    # 写入标准化后的值
    normalized["entry_low"] = entry_low
    normalized["entry_high"] = entry_high

    return normalized


def _extract_entry_low(signal: dict) -> float | None:
    """
    提取 entry_low，兼容旧字段。

    优先级：
    1. entry_low（新字段）
    2. price（旧字段）
    3. min_price（旧字段）
    4. entry（旧字段）
    """
    # 优先使用新字段
    if "entry_low" in signal and signal["entry_low"] is not None:
        return float(signal["entry_low"])

    # 兼容旧字段
    if "price" in signal and signal["price"] is not None:
        logger.warning(f"[标准化] 使用 deprecated 字段: price={signal['price']}")
        return float(signal["price"])

    if "min_price" in signal and signal["min_price"] is not None:
        logger.warning(f"[标准化] 使用 deprecated 字段: min_price={signal['min_price']}")
        return float(signal["min_price"])

    if "entry" in signal and signal["entry"] is not None:
        logger.warning(f"[标准化] 使用 deprecated 字段: entry={signal['entry']}")
        return float(signal["entry"])

    return None


def _extract_entry_high(signal: dict) -> float | None:
    """
    提取 entry_high，兼容旧字段。

    优先级：
    1. entry_high（新字段）
    2. price2（旧字段）
    3. max_price（旧字段）
    """
    # 优先使用新字段
    if "entry_high" in signal and signal["entry_high"] is not None:
        return float(signal["entry_high"])

    # 兼容旧字段
    if "price2" in signal and signal["price2"] is not None:
        logger.warning(f"[标准化] 使用 deprecated 字段: price2={signal['price2']}")
        return float(signal["price2"])

    if "max_price" in signal and signal["max_price"] is not None:
        logger.warning(f"[标准化] 使用 deprecated 字段: max_price={signal['max_price']}")
        return float(signal["max_price"])

    return None
