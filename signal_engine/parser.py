"""
信号解析器 — DeepSeek API + 消息类型分发 + 标准化。

数据流：
  DeepSeek JSON（含 message_type）
      ↓
  standardize() 按 message_type 分发
      ↓
  统一 dict（含 signal_type 语义，兼容下游）
"""
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from openai import AsyncOpenAI
from loguru import logger

from .prompts import SYSTEM_PROMPT, build_user_prompt
from .normalization import normalize_signal
from core.protection_targets import positive_price, normalize_tp_prices


# ———————— 数据规范化工具（防御 AI 返回字符串/None 等非预期类型）————————


def normalize_dict(value, field_name="unknown"):
    """确保字段是 dict 类型。安全遍历前必须调用此函数。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        logger.info(f"AI 返回文本（期望 dict）：{field_name}={value[:200]}")
    elif value is not None and value != [] and value != {}:
        logger.info(f"AI 返回非预期类型（期望 dict）：{field_name}={type(value).__name__}")
    return {}


def normalize_list(value, field_name="unknown"):
    """确保字段是 list 类型。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        logger.info(f"AI 返回文本（期望 list）：{field_name}={value[:200]}")
    elif value is not None:
        logger.info(f"AI 返回非预期类型（期望 list）：{field_name}={type(value).__name__}")
    return []


def _normalize_pair(pair: str) -> str:
    """统一 pair 格式: AAVE/USDT:USDT → AAVEUSDT"""
    return (pair or "").upper().replace("/", "").replace(":USDT", "").strip()


# ———————— 新增：消息类型优先级常量 ————————

# DeepSeek 只允许输出以下 message_type
VALID_MESSAGE_TYPES = {
    "MARKET_ORDER",
    "LIMIT_ORDER",
    "UPDATE_SLTP",
    "CLOSE_POSITION",
    "CANCEL_ORDER",
    "BREAKOUT_ORDER",
    "PULLBACK_ORDER",
    "IGNORE",
}


class SignalParser:
    """DeepSeek 信号解析器 — 消息类型驱动架构."""

    def __init__(
        self, api_key: str, base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-v4-pro", temperature: float = 0.1, max_tokens: int = 2048,
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    @staticmethod
    def _parse_entry_range(raw: str) -> tuple[float | None, float | None]:
        """
        解析入场价格范围，支持多种分隔符格式。

        支持格式：
        - "61588-60888" → (60888, 61588) 自动交换
        - "60888-61588" → (60888, 61588)
        - "61588~60888" → (60888, 61588) 自动交换
        - "61588 至 60888" → (60888, 61588) 自动交换
        - "61588到60888" → (60888, 61588) 自动交换
        - "61588,60888" → (60888, 61588) 自动交换
        - "61588 60888" → (60888, 61588) 自动交换
        - "61588" → (61588, None) 单价格

        Args:
            raw: 原始价格字符串

        Returns:
            (entry_low, entry_high) - 较低价格和较高价格，自动确保 entry_low <= entry_high
        """
        if not raw or not isinstance(raw, str):
            return None, None

        # 支持的分隔符：- ~ 至 到 , 空格（多个空格）
        pattern = r'\s*[-~至到,]\s*|\s+'
        parts = re.split(pattern, raw.strip())

        # 过滤空字符串
        parts = [p.strip() for p in parts if p.strip()]

        if not parts:
            return None, None

        try:
            if len(parts) == 1:
                price = float(parts[0])
                return price, None
            elif len(parts) >= 2:
                p1 = float(parts[0])
                p2 = float(parts[1])
                if p1 > p2:
                    return p2, p1
                else:
                    return p1, p2
        except (ValueError, TypeError) as e:
            logger.warning(f"[解析] 入场价格解析失败: {raw}, error={e}")
            return None, None

    @staticmethod
    def clean_symbol(raw: str) -> str:
        s = raw.upper().replace("/", "").replace("-", "").replace(" ", "").replace(":", "")
        if s.endswith("USDT") and len(s) > 4:
            return s
        for suffix in ("USD", "BUSD", "USDC"):
            if s.endswith(suffix):
                s = s[:-len(suffix)]
                break
        if not s.endswith("USDT"):
            s += "USDT"
        return s

    @staticmethod
    def _get_default_leverage(symbol: str) -> int:
        """根据币种返回默认杠杆。"""
        sym = symbol.upper()
        if sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"):
            return 20
        return 10

    @staticmethod
    def _parse_close_pct(raw) -> float:
        """解析平仓/减仓比例，容忍 '50%' / '50％' / '50' / 50.0 / null。

        - 缺失 / 非法 / <=0 → 100.0（全平）
        - 有效值裁剪到 100.0
        """
        if raw is None or raw == "":
            return 100.0
        try:
            if isinstance(raw, str):
                cleaned = raw.strip().replace("%", "").replace("％", "")
                pct = float(cleaned)
            else:
                pct = float(raw)
        except (ValueError, TypeError):
            logger.warning(f"[解析] close_pct 非法: {raw!r}，按全平处理")
            return 100.0
        if pct <= 0:
            return 100.0
        return min(pct, 100.0)

    async def parse(self, text: str, sender: str = "", group: str = "", original_signal: str = "", retries: int = 2) -> Optional[dict]:
        """调用 DeepSeek API 解析一条消息。空响应/JSON解析失败会自动重试。

        Args:
            text: 当前消息文本
            sender: 发送者名称
            group: 群组名称
            original_signal: 原始交易信号（从回复链中提取）
            retries: 重试次数
        """
        user_prompt = build_user_prompt(text, sender=sender, group=group, original_signal=original_signal)
        last_error = None

        for attempt in range(1 + retries):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self.temperature, max_tokens=self.max_tokens,
                )
                raw = resp.choices[0].message.content
                content = (raw or "").strip()

                # ———— 空响应检测 ————
                if not content:
                    last_error = f"API 返回空内容 (attempt {attempt+1}/{1+retries})"
                    logger.warning(f"[解析] {last_error}，消息: {text[:80]}")
                    if attempt < retries:
                        continue
                    return {
                        "is_trading_signal": False,
                        "error": last_error,
                        "message_type": "IGNORE",
                        "parse_debug": {
                            "raw_signal": text[:200],
                            "recognized": {},
                            "inferred": {},
                            "warnings": ["DeepSeek 返回空内容，疑似输入无法识别或API瞬时故障"],
                            "summary": f"API返回空内容，已重试{retries}次仍失败",
                        },
                    }

                # ———— 清洗 markdown 包裹 ————
                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?\s*", "", content)
                    content = re.sub(r"\s*```$", "", content)

                # ———— JSON 解析 ————
                result = json.loads(content)
                return result

            except json.JSONDecodeError as e:
                last_error = str(e)
                logger.warning(
                    f"[解析] JSON失败 (attempt {attempt+1}/{1+retries}): {e}，"
                    f"消息: {text[:80]}"
                )
                if attempt < retries:
                    continue
                return {"is_trading_signal": False, "message_type": "IGNORE", "error": str(e)}

            except Exception as e:
                logger.error(f"[解析] API失败 (attempt {attempt+1}): {e}")
                if attempt < retries:
                    continue
                return None

        return {"is_trading_signal": False, "message_type": "IGNORE", "error": last_error or "未知错误"}

    def standardize(self, result: dict, text: str, sender: str = "", group: str = "") -> Optional[dict]:
        """
        将 LLM 输出按 message_type 标准化为可执行信号。

        == 消息类型 → 执行语义 ==
        MARKET_ORDER    → signal_type=new,  entry_strategy=market,  无 Entry Price
        LIMIT_ORDER     → signal_type=new,  entry_strategy=limit_*, 有 Entry Price
        UPDATE_SLTP     → signal_type=update
        CLOSE_POSITION  → signal_type=close
        CANCEL_ORDER    → signal_type=cancel
        BREAKOUT_ORDER  → signal_type=new,  entry_strategy=limit_trigger, 不执行
        PULLBACK_ORDER  → signal_type=new,  entry_strategy=limit_single,  不执行
        IGNORE          → None

        **不再根据"有没有 entry/price/sl/tp"猜测交易类型。**
        **只有 message_type 决定程序行为。**
        """
        if result is None:
            return None

        # ———— 提取 message_type ————
        message_type = (result.get("message_type") or "").strip().upper()
        if message_type not in VALID_MESSAGE_TYPES:
            logger.info(f"[标准化] 未知 message_type={message_type!r}，视为 IGNORE")
            return None

        # ———— IGNORE / 非交易信号 ————
        if message_type == "IGNORE":
            return None

        # ———— 通用字段提取 ————
        symbol = self.clean_symbol(result.get("symbol") or "")
        direction = (result.get("direction") or "").lower().strip()

        # 来源信息
        common = {
            "source_group": group,
            "source_sender": sender,
            "raw_text": text,
            "message_type": message_type,  # 保留原始 message_type 用于日志/审计
        }

        # 开仓消息可在顶层或 parse_debug.recognized 携带老师价位。
        debug = normalize_dict(result.get("parse_debug"), "parse_debug")
        recognized = normalize_dict(debug.get("recognized"), "parse_debug.recognized")
        raw_sl = result.get("sl", result.get("stop_loss"))
        raw_tp = result.get("tp", result.get("take_profit"))
        entry_sl = positive_price(raw_sl) or positive_price(recognized.get("sl"))
        entry_tp = normalize_tp_prices(raw_tp) or normalize_tp_prices(recognized.get("tp"))

        # ================================================================
        #  MARKET_ORDER — 市价进场，不需要 Entry Price
        # ================================================================
        if message_type == "MARKET_ORDER":
            if direction not in ("long", "short") or len(symbol) < 5:
                logger.warning(f"[标准化] MARKET_ORDER 缺 direction/symbol: dir={direction}, sym={symbol}")
                return None

            leverage = self._get_default_leverage(symbol)

            return {
                "signal_type": "new",
                "position_action": "open",
                "symbol": symbol,
                "direction": direction,
                "entry_low": None,
                "entry_high": None,
                "entry_type": "market",
                "entry_strategy": "market",
                "trigger_price": None,
                "stop_loss": entry_sl,
                "sl_type": "fixed",
                "take_profit": entry_tp,
                "leverage": leverage,
                **common,
            }

        # ================================================================
        #  LIMIT_ORDER — 限价挂单，有 Entry Price
        # ================================================================
        if message_type == "LIMIT_ORDER":
            if direction not in ("long", "short") or len(symbol) < 5:
                logger.warning(f"[标准化] LIMIT_ORDER 缺 direction/symbol: dir={direction}, sym={symbol}")
                return None

            # 从 DeepSeek 输出提取 entry_low / entry_high
            entry_low = result.get("entry_low")
            entry_high = result.get("entry_high")
            entry_raw = result.get("entry_raw", "")

            # 兼容旧字段（仅在此处兼容）
            if entry_low is None:
                entry_low = result.get("price") or result.get("min_price")
            if entry_high is None:
                entry_high = result.get("price2") or result.get("max_price")

            # 调用标准化函数进行校验
            temp_signal = {
                "entry_low": entry_low,
                "entry_high": entry_high,
                "entry_strategy": "limit_single" if entry_high is None else "limit_range",
                "entry_raw": entry_raw,
            }
            normalized = normalize_signal(temp_signal, symbol=symbol)
            if normalized is None:
                logger.warning(f"[标准化] {symbol} LIMIT_ORDER 入场价格校验失败，拒绝信号")
                return None

            entry_low_final = normalized.get("entry_low")
            entry_high_final = normalized.get("entry_high")
            has_range = entry_high_final is not None
            entry_strategy = "limit_range" if has_range else "limit_single"

            leverage = self._get_default_leverage(symbol)

            return {
                "signal_type": "new",
                "position_action": "open",
                "symbol": symbol,
                "direction": direction,
                "entry_low": entry_low_final,
                "entry_high": entry_high_final,
                "entry_type": "limit",
                "entry_strategy": entry_strategy,
                "trigger_price": None,
                "stop_loss": entry_sl,
                "sl_type": "fixed",
                "take_profit": entry_tp,
                "leverage": leverage,
                **common,
            }

        # ================================================================
        #  UPDATE_SLTP — 修改止盈止损
        # ================================================================
        if message_type == "UPDATE_SLTP":
            new_sl = positive_price(result.get("new_stop_loss"))
            new_tp = normalize_tp_prices(result.get("new_take_profit"))
            update_type = (result.get("update_type") or "").strip() or "sl_and_tp"

            return {
                "signal_type": "update",
                "position_action": "modify",
                "symbol": symbol,
                "direction": direction,
                "update_type": update_type,
                "new_stop_loss": new_sl,
                "new_take_profit": new_tp,
                **common,
            }

        # ================================================================
        #  CLOSE_POSITION — 平仓
        # ================================================================
        if message_type == "CLOSE_POSITION":
            close_pct = self._parse_close_pct(result.get("close_pct"))
            return {
                "signal_type": "close",
                "position_action": "close",
                "symbol": symbol,
                "direction": direction,
                "close_pct": close_pct,
                **common,
            }

        # ================================================================
        #  CANCEL_ORDER — 取消挂单
        # ================================================================
        if message_type == "CANCEL_ORDER":
            if len(symbol) < 5:
                logger.warning(f"[标准化] CANCEL_ORDER 缺 symbol: {symbol}")
                return None
            return {
                "signal_type": "cancel",
                "position_action": "cancel",
                "symbol": symbol,
                "direction": "",
                **common,
            }

        # ================================================================
        #  BREAKOUT_ORDER — 突破单（当前不执行）
        # ================================================================
        if message_type == "BREAKOUT_ORDER":
            trigger_price = result.get("trigger_price")
            if trigger_price is not None:
                trigger_price = float(trigger_price)

            leverage = self._get_default_leverage(symbol)
            return {
                "signal_type": "breakout",
                "position_action": "open",
                "symbol": symbol,
                "direction": direction,
                "entry_low": None,
                "entry_high": None,
                "entry_type": "limit",
                "entry_strategy": "limit_trigger",
                "trigger_price": trigger_price,
                "stop_loss": None,
                "sl_type": "fixed",
                "take_profit": [],
                "leverage": leverage,
                "message_type": "BREAKOUT_ORDER",
                **common,
            }

        # ================================================================
        #  PULLBACK_ORDER — 回踩单（当前不执行）
        # ================================================================
        if message_type == "PULLBACK_ORDER":
            entry_low = result.get("entry_low")
            if entry_low is not None:
                entry_low = float(entry_low)

            leverage = self._get_default_leverage(symbol)
            return {
                "signal_type": "pullback",
                "position_action": "open",
                "symbol": symbol,
                "direction": direction,
                "entry_low": entry_low,
                "entry_high": None,
                "entry_type": "limit",
                "entry_strategy": "limit_single",
                "trigger_price": None,
                "stop_loss": None,
                "sl_type": "fixed",
                "take_profit": [],
                "leverage": leverage,
                "message_type": "PULLBACK_ORDER",
                **common,
            }

        # 不应该到达这里
        return None

    async def close(self):
        """释放 AsyncOpenAI 客户端资源（aiohttp session）。"""
        if hasattr(self, 'client') and self.client is not None:
            try:
                await self.client.close()
                logger.debug("[SignalParser] AsyncOpenAI client 已关闭")
            except Exception as e:
                logger.warning(f"[SignalParser] 关闭 client 异常: {e}")
