"""
入场价格解析单元测试
测试 _parse_entry_range 方法的各种分隔符和方向组合
"""
import pytest
from signal_engine.parser import SignalParser


class TestParseEntryRange:
    """测试入场价格范围解析"""

    def setup_method(self):
        """初始化测试"""
        self.parser = SignalParser(api_key="test_key")

    # ========== 基础分隔符测试 ==========

    def test_dash_separator_normal_order(self):
        """测试短横线分隔符（正常顺序）"""
        low, high = self.parser._parse_entry_range("60888-61588")
        assert low == 60888.0
        assert high == 61588.0

    def test_dash_separator_reversed_order(self):
        """测试短横线分隔符（反向顺序，应自动交换）"""
        low, high = self.parser._parse_entry_range("61588-60888")
        assert low == 60888.0
        assert high == 61588.0

    def test_tilde_separator(self):
        """测试波浪号分隔符"""
        low, high = self.parser._parse_entry_range("61588~60888")
        assert low == 60888.0
        assert high == 61588.0

    def test_chinese_zhi_separator(self):
        """测试中文"至"分隔符"""
        low, high = self.parser._parse_entry_range("61588 至 60888")
        assert low == 60888.0
        assert high == 61588.0

    def test_chinese_dao_separator(self):
        """测试中文"到"分隔符"""
        low, high = self.parser._parse_entry_range("61588到60888")
        assert low == 60888.0
        assert high == 61588.0

    def test_comma_separator(self):
        """测试逗号分隔符"""
        low, high = self.parser._parse_entry_range("61588,60888")
        assert low == 60888.0
        assert high == 61588.0

    def test_space_separator(self):
        """测试空格分隔符"""
        low, high = self.parser._parse_entry_range("61588 60888")
        assert low == 60888.0
        assert high == 61588.0

    def test_multiple_spaces(self):
        """测试多个空格分隔符"""
        low, high = self.parser._parse_entry_range("61588   60888")
        assert low == 60888.0
        assert high == 61588.0

    # ========== 单价格测试 ==========

    def test_single_price(self):
        """测试单价格"""
        low, high = self.parser._parse_entry_range("61588")
        assert low == 61588.0
        assert high is None

    def test_single_price_with_spaces(self):
        """测试带空格的单价格"""
        low, high = self.parser._parse_entry_range("  61588  ")
        assert low == 61588.0
        assert high is None

    # ========== 边界情况测试 ==========

    def test_empty_string(self):
        """测试空字符串"""
        low, high = self.parser._parse_entry_range("")
        assert low is None
        assert high is None

    def test_none_input(self):
        """测试 None 输入"""
        low, high = self.parser._parse_entry_range(None)
        assert low is None
        assert high is None

    def test_non_string_input(self):
        """测试非字符串输入"""
        low, high = self.parser._parse_entry_range(123)
        assert low is None
        assert high is None

    def test_invalid_format(self):
        """测试无效格式"""
        low, high = self.parser._parse_entry_range("abc-def")
        assert low is None
        assert high is None

    # ========== 小数价格测试 ==========

    def test_decimal_prices(self):
        """测试小数价格"""
        low, high = self.parser._parse_entry_range("0.1234-0.1230")
        assert low == 0.1230
        assert high == 0.1234

    def test_decimal_prices_reversed(self):
        """测试小数价格（反向）"""
        low, high = self.parser._parse_entry_range("0.1234-0.1230")
        assert low == 0.1230
        assert high == 0.1234

    # ========== 大数值测试 ==========

    def test_large_numbers(self):
        """测试大数值"""
        low, high = self.parser._parse_entry_range("100000-99000")
        assert low == 99000.0
        assert high == 100000.0

    # ========== 混合分隔符测试 ==========

    def test_mixed_separators(self):
        """测试混合分隔符（应取前两个数字）"""
        low, high = self.parser._parse_entry_range("61588-60888~60000")
        assert low == 60888.0
        assert high == 61588.0

    # ========== 实际场景测试 ==========

    def test_teacher_signal_btc_long(self):
        """测试老师信号：BTC LONG EP: 61588-60888"""
        low, high = self.parser._parse_entry_range("61588-60888")
        assert low == 60888.0
        assert high == 61588.0
        assert low <= high, "entry_low 必须 <= entry_high"

    def test_teacher_signal_btc_short(self):
        """测试老师信号：BTC SHORT EP: 60888-61588"""
        low, high = self.parser._parse_entry_range("60888-61588")
        assert low == 60888.0
        assert high == 61588.0
        assert low <= high, "entry_low 必须 <= entry_high"

    def test_teacher_signal_with_spaces(self):
        """测试老师信号带空格"""
        low, high = self.parser._parse_entry_range(" 61588 - 60888 ")
        assert low == 60888.0
        assert high == 61588.0


class TestStandardizeMessageType:
    """测试标准化的消息类型分发逻辑"""

    def setup_method(self):
        """初始化测试"""
        self.parser = SignalParser(api_key="test_key")

    def test_market_order_no_entry(self):
        """测试 MARKET_ORDER 无 Entry Price"""
        result = {
            "message_type": "MARKET_ORDER",
            "symbol": "BTCUSDT",
            "direction": "long",
            "parse_debug": {
                "recognized": {"coin": "BTC", "direction": "long"},
                "inferred": {"symbol": "BTCUSDT"},
                "warnings": [],
                "summary": "MARKET_ORDER #BTC 多 → BTCUSDT long",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 多")

        assert standardized is not None
        assert standardized["signal_type"] == "new"
        assert standardized["entry_low"] is None
        assert standardized["entry_high"] is None
        assert standardized["entry_strategy"] == "market"
        assert standardized["symbol"] == "BTCUSDT"
        assert standardized["direction"] == "long"

    def test_limit_order_with_reversed_entry(self):
        """测试 LIMIT_ORDER 自动交换反向价格"""
        result = {
            "message_type": "LIMIT_ORDER",
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_low": 61588.0,   # 反向
            "entry_high": 60888.0,  # 反向
            "parse_debug": {
                "recognized": {"coin": "BTC", "direction": "long", "entry": "61588-60888"},
                "inferred": {"symbol": "BTCUSDT", "strategy": "limit_range"},
                "warnings": [],
                "summary": "LIMIT_ORDER #BTC 61588-60888 → BTCUSDT long",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 61588-60888 long")

        assert standardized is not None
        assert standardized["signal_type"] == "new"
        assert standardized["entry_low"] == 60888.0
        assert standardized["entry_high"] == 61588.0
        assert standardized["entry_low"] <= standardized["entry_high"]
        assert standardized["entry_strategy"] == "limit_range"

    def test_limit_order_normal_entry(self):
        """测试 LIMIT_ORDER 正常价格不交换"""
        result = {
            "message_type": "LIMIT_ORDER",
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_low": 60888.0,
            "entry_high": 61588.0,
            "parse_debug": {
                "recognized": {"coin": "BTC", "direction": "long", "entry": "60888-61588"},
                "inferred": {"symbol": "BTCUSDT", "strategy": "limit_range"},
                "warnings": [],
                "summary": "LIMIT_ORDER #BTC 60888-61588 → BTCUSDT long",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 60888-61588 long")

        assert standardized is not None
        assert standardized["entry_low"] == 60888.0
        assert standardized["entry_high"] == 61588.0
        assert standardized["entry_strategy"] == "limit_range"

    def test_limit_single_price(self):
        """测试 LIMIT_ORDER 单价格"""
        result = {
            "message_type": "LIMIT_ORDER",
            "symbol": "ETHUSDT",
            "direction": "long",
            "entry_low": 3100.0,
            "entry_high": None,
            "parse_debug": {
                "recognized": {"coin": "ETH", "direction": "long", "entry": "3100"},
                "inferred": {"symbol": "ETHUSDT", "strategy": "limit_single"},
                "warnings": [],
                "summary": "LIMIT_ORDER #ETH 回踩3100 → ETHUSDT long",
            },
        }

        standardized = self.parser.standardize(result, "#ETH 回踩 3100 做多")

        assert standardized is not None
        assert standardized["signal_type"] == "new"
        assert standardized["entry_low"] == 3100.0
        assert standardized["entry_high"] is None
        assert standardized["entry_strategy"] == "limit_single"

    def test_update_sltp(self):
        """测试 UPDATE_SLTP 信号"""
        result = {
            "message_type": "UPDATE_SLTP",
            "symbol": "BTCUSDT",
            "new_stop_loss": 63000,
            "new_take_profit": None,
            "update_type": "modify_sl",
            "parse_debug": {
                "recognized": {"coin": "BTC", "entry": "63000"},
                "inferred": {"symbol": "BTCUSDT"},
                "warnings": [],
                "summary": "UPDATE_SLTP #BTC 止损提到63000",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 止损提到 63000")

        assert standardized is not None
        assert standardized["signal_type"] == "update"
        assert standardized["update_type"] == "modify_sl"
        assert standardized["new_stop_loss"] == 63000.0
        assert standardized["new_take_profit"] == []

    def test_close_position(self):
        """测试 CLOSE_POSITION 信号"""
        result = {
            "message_type": "CLOSE_POSITION",
            "symbol": "BTCUSDT",
            "close_pct": 100,
            "parse_debug": {
                "recognized": {"coin": "BTC"},
                "inferred": {"symbol": "BTCUSDT"},
                "warnings": [],
                "summary": "CLOSE_POSITION #BTC 平仓",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 平仓")

        assert standardized is not None
        assert standardized["signal_type"] == "close"
        assert standardized["close_pct"] == 100.0

    def test_cancel_order(self):
        """测试 CANCEL_ORDER 信号"""
        result = {
            "message_type": "CANCEL_ORDER",
            "symbol": "BTCUSDT",
            "parse_debug": {
                "recognized": {"coin": "BTC"},
                "inferred": {"symbol": "BTCUSDT"},
                "warnings": [],
                "summary": "CANCEL_ORDER #BTC 取消挂单",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 取消挂单")

        assert standardized is not None
        assert standardized["signal_type"] == "cancel"
        assert standardized["symbol"] == "BTCUSDT"

    def test_breakout_order(self):
        """测试 BREAKOUT_ORDER 信号（当前不执行）"""
        result = {
            "message_type": "BREAKOUT_ORDER",
            "symbol": "BTCUSDT",
            "direction": "long",
            "trigger_price": 65000,
            "parse_debug": {
                "recognized": {"coin": "BTC", "direction": "long", "entry": "65000"},
                "inferred": {"symbol": "BTCUSDT", "strategy": "breakout"},
                "warnings": [],
                "summary": "BREAKOUT_ORDER #BTC 突破65000追多",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 突破 65000 追多")

        assert standardized is not None
        assert standardized["signal_type"] == "breakout"
        assert standardized["message_type"] == "BREAKOUT_ORDER"
        assert standardized["trigger_price"] == 65000.0

    def test_pullback_order(self):
        """测试 PULLBACK_ORDER 信号（当前不执行）"""
        result = {
            "message_type": "PULLBACK_ORDER",
            "symbol": "SOLUSDT",
            "direction": "long",
            "entry_low": 145.0,
            "parse_debug": {
                "recognized": {"coin": "SOL", "direction": "long", "entry": "145"},
                "inferred": {"symbol": "SOLUSDT", "strategy": "pullback"},
                "warnings": [],
                "summary": "PULLBACK_ORDER #SOL 回踩145做多",
            },
        }

        standardized = self.parser.standardize(result, "#SOL 回踩 145 做多")

        assert standardized is not None
        assert standardized["signal_type"] == "pullback"
        assert standardized["message_type"] == "PULLBACK_ORDER"
        assert standardized["entry_low"] == 145.0

    def test_ignore_signal(self):
        """测试 IGNORE 信号"""
        result = {
            "message_type": "IGNORE",
            "parse_debug": {
                "raw_signal": "感觉要涨了",
                "summary": "非交易内容，已忽略",
            },
        }

        standardized = self.parser.standardize(result, "感觉要涨了")

        assert standardized is None

    def test_market_order_no_warning(self):
        """验证 MARKET_ORDER 不会输出任何'未检测到入场价'警告"""
        result = {
            "message_type": "MARKET_ORDER",
            "symbol": "BTCUSDT",
            "direction": "long",
            "parse_debug": {
                "recognized": {"coin": "BTC", "direction": "long"},
                "inferred": {"symbol": "BTCUSDT"},
                "warnings": [],  # 必须为空数组，不能有"未检测到入场价"
                "summary": "MARKET_ORDER #BTC 多 → BTCUSDT long",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 多")

        assert standardized is not None
        # 验证没有 warnings 相关逻辑
        assert standardized["entry_low"] is None  # MARKET_ORDER 正常无 Entry
        assert standardized["entry_high"] is None

    def test_backward_compatibility_old_fields(self):
        """测试向后兼容旧字段（price/price2 在 result 顶层）"""
        # 模拟旧格式 AI 输出（entry_low/entry_high 不存在，使用 price/price2 回退）
        result = {
            "message_type": "LIMIT_ORDER",
            "symbol": "BTCUSDT",
            "direction": "long",
            "price": 61588.0,    # 旧字段（反向）
            "price2": 60888.0,   # 旧字段（反向）
            "parse_debug": {
                "recognized": {"coin": "BTC", "direction": "long", "entry": "61588-60888"},
                "inferred": {"symbol": "BTCUSDT", "strategy": "limit_range"},
                "warnings": [],
                "summary": "LIMIT_ORDER #BTC 61588-60888 → BTCUSDT long",
            },
        }

        standardized = self.parser.standardize(result, "#BTC 61588-60888 long")

        assert standardized is not None
        # 应该自动交换
        assert standardized["entry_low"] == 60888.0
        assert standardized["entry_high"] == 61588.0
        assert standardized["entry_low"] <= standardized["entry_high"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
