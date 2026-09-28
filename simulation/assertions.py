"""
Assertions - 断言检查系统
验证交易生命周期的每个阶段是否符合预期
"""
from __future__ import annotations
from typing import Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum


class AssertionResult(str, Enum):
    """断言结果"""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class AssertionCheck:
    """断言检查项"""
    name: str
    description: str
    result: AssertionResult
    message: str = ""
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "name": self.name,
            "description": self.description,
            "result": self.result.value,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


class Assertions:
    """
    断言检查系统

    验证交易生命周期的每个阶段是否符合预期，支持：
    - 仓位状态检查
    - 订单状态检查
    - 余额检查
    - 快照检查
    - 自定义检查
    """

    def __init__(self):
        """初始化断言系统"""
        self._checks: list[AssertionCheck] = []
        self._custom_checks: dict[str, Callable] = {}

    def _add_check(
        self,
        name: str,
        description: str,
        passed: bool,
        message: str = "",
        expected: Any = None,
        actual: Any = None,
    ) -> AssertionCheck:
        """添加检查项"""
        result = AssertionResult.PASS if passed else AssertionResult.FAIL
        check = AssertionCheck(
            name=name,
            description=description,
            result=result,
            message=message,
            expected=expected,
            actual=actual,
        )
        self._checks.append(check)
        return check

    # ========== 仓位检查 ==========

    def assert_position_exists(
        self,
        positions: list[dict],
        symbol: str,
        side: Optional[str] = None,
    ) -> AssertionCheck:
        """
        断言仓位存在

        Args:
            positions: 仓位列表
            symbol: 交易对
            side: 方向（可选）

        Returns:
            检查结果
        """
        found = False
        for pos in positions:
            if pos.get("symbol") == symbol:
                if side is None or pos.get("side") == side:
                    found = True
                    break

        return self._add_check(
            name="position_exists",
            description=f"检查仓位是否存在: {symbol} {side or ''}",
            passed=found,
            message="" if found else f"未找到仓位: {symbol} {side or ''}",
            expected="position exists",
            actual="position not found" if not found else "position found",
        )

    def assert_position_not_exists(
        self,
        positions: list[dict],
        symbol: str,
        side: Optional[str] = None,
    ) -> AssertionCheck:
        """
        断言仓位不存在

        Args:
            positions: 仓位列表
            symbol: 交易对
            side: 方向（可选）

        Returns:
            检查结果
        """
        found = False
        for pos in positions:
            if pos.get("symbol") == symbol:
                if side is None or pos.get("side") == side:
                    found = True
                    break

        return self._add_check(
            name="position_not_exists",
            description=f"检查仓位是否不存在: {symbol} {side or ''}",
            passed=not found,
            message="" if not found else f"仓位仍然存在: {symbol} {side or ''}",
            expected="position not exists",
            actual="position exists" if found else "position not exists",
        )

    def assert_position_size(
        self,
        positions: list[dict],
        symbol: str,
        expected_size: float,
        tolerance: float = 0.001,
    ) -> AssertionCheck:
        """
        断言仓位大小

        Args:
            positions: 仓位列表
            symbol: 交易对
            expected_size: 预期大小
            tolerance: 容差

        Returns:
            检查结果
        """
        actual_size = 0.0
        for pos in positions:
            if pos.get("symbol") == symbol:
                actual_size += float(pos.get("contracts", 0))

        passed = abs(actual_size - expected_size) <= tolerance

        return self._add_check(
            name="position_size",
            description=f"检查仓位大小: {symbol}",
            passed=passed,
            message="" if passed else f"仓位大小不匹配",
            expected=expected_size,
            actual=actual_size,
        )

    # ========== 订单检查 ==========

    def assert_order_exists(
        self,
        orders: list[dict],
        order_type: Optional[str] = None,
        side: Optional[str] = None,
        status: Optional[str] = None,
    ) -> AssertionCheck:
        """
        断言订单存在

        Args:
            orders: 订单列表
            order_type: 订单类型
            side: 方向
            status: 状态

        Returns:
            检查结果
        """
        found = False
        for order in orders:
            match = True
            if order_type and order.get("type") != order_type:
                match = False
            if side and order.get("side") != side:
                match = False
            if status and order.get("status") != status:
                match = False

            if match:
                found = True
                break

        desc_parts = []
        if order_type:
            desc_parts.append(f"type={order_type}")
        if side:
            desc_parts.append(f"side={side}")
        if status:
            desc_parts.append(f"status={status}")

        return self._add_check(
            name="order_exists",
            description=f"检查订单是否存在: {', '.join(desc_parts)}",
            passed=found,
            message="" if found else "未找到匹配的订单",
            expected="order exists",
            actual="order not found" if not found else "order found",
        )

    def assert_order_not_exists(
        self,
        orders: list[dict],
        order_type: Optional[str] = None,
        side: Optional[str] = None,
        status: Optional[str] = None,
    ) -> AssertionCheck:
        """
        断言订单不存在

        Args:
            orders: 订单列表
            order_type: 订单类型
            side: 方向
            status: 状态

        Returns:
            检查结果
        """
        found = False
        for order in orders:
            match = True
            if order_type and order.get("type") != order_type:
                match = False
            if side and order.get("side") != side:
                match = False
            if status and order.get("status") != status:
                match = False

            if match:
                found = True
                break

        desc_parts = []
        if order_type:
            desc_parts.append(f"type={order_type}")
        if side:
            desc_parts.append(f"side={side}")
        if status:
            desc_parts.append(f"status={status}")

        return self._add_check(
            name="order_not_exists",
            description=f"检查订单是否不存在: {', '.join(desc_parts)}",
            passed=not found,
            message="" if not found else "订单仍然存在",
            expected="order not exists",
            actual="order exists" if found else "order not exists",
        )

    # ========== 余额检查 ==========

    def assert_balance_greater_than(
        self,
        balance: dict,
        min_amount: float,
    ) -> AssertionCheck:
        """
        断言余额大于指定值

        Args:
            balance: 余额数据
            min_amount: 最小金额

        Returns:
            检查结果
        """
        actual = float(balance.get("USDT", {}).get("free", 0))
        passed = actual > min_amount

        return self._add_check(
            name="balance_greater_than",
            description=f"检查余额是否大于 {min_amount}",
            passed=passed,
            message="" if passed else f"余额不足",
            expected=f"> {min_amount}",
            actual=actual,
        )

    def assert_balance_less_than(
        self,
        balance: dict,
        max_amount: float,
    ) -> AssertionCheck:
        """
        断言余额小于指定值

        Args:
            balance: 余额数据
            max_amount: 最大金额

        Returns:
            检查结果
        """
        actual = float(balance.get("USDT", {}).get("free", 0))
        passed = actual < max_amount

        return self._add_check(
            name="balance_less_than",
            description=f"检查余额是否小于 {max_amount}",
            passed=passed,
            message="" if passed else f"余额超出预期",
            expected=f"< {max_amount}",
            actual=actual,
        )

    # ========== 通用断言 ==========

    def assert_equal(
        self,
        actual: Any,
        expected: Any,
        description: str = "",
    ) -> AssertionCheck:
        """
        断言相等

        Args:
            actual: 实际值
            expected: 预期值
            description: 描述

        Returns:
            检查结果
        """
        passed = actual == expected

        return self._add_check(
            name="equal",
            description=description or f"检查是否相等",
            passed=passed,
            message="" if passed else f"值不相等",
            expected=expected,
            actual=actual,
        )

    def assert_not_none(
        self,
        value: Any,
        description: str = "",
    ) -> AssertionCheck:
        """
        断言非 None

        Args:
            value: 要检查的值
            description: 描述

        Returns:
            检查结果
        """
        passed = value is not None

        return self._add_check(
            name="not_none",
            description=description or f"检查是否非 None",
            passed=passed,
            message="" if passed else f"值为 None",
            expected="not None",
            actual=value,
        )

    def assert_greater_than(
        self,
        actual: float,
        expected: float,
        description: str = "",
    ) -> AssertionCheck:
        """
        断言大于

        Args:
            actual: 实际值
            expected: 预期值
            description: 描述

        Returns:
            检查结果
        """
        passed = actual > expected

        return self._add_check(
            name="greater_than",
            description=description or f"检查是否大于 {expected}",
            passed=passed,
            message="" if passed else f"值不大于预期",
            expected=f"> {expected}",
            actual=actual,
        )

    def assert_less_than(
        self,
        actual: float,
        expected: float,
        description: str = "",
    ) -> AssertionCheck:
        """
        断言小于

        Args:
            actual: 实际值
            expected: 预期值
            description: 描述

        Returns:
            检查结果
        """
        passed = actual < expected

        return self._add_check(
            name="less_than",
            description=description or f"检查是否小于 {expected}",
            passed=passed,
            message="" if passed else f"值不小于预期",
            expected=f"< {expected}",
            actual=actual,
        )

    def assert_true(
        self,
        value: bool,
        description: str = "",
    ) -> AssertionCheck:
        """
        断言为真

        Args:
            value: 要检查的值
            description: 描述

        Returns:
            检查结果
        """
        passed = value is True

        return self._add_check(
            name="true",
            description=description or f"检查是否为真",
            passed=passed,
            message="" if passed else f"值为假",
            expected=True,
            actual=value,
        )

    def assert_false(
        self,
        value: bool,
        description: str = "",
    ) -> AssertionCheck:
        """
        断言为假

        Args:
            value: 要检查的值
            description: 描述

        Returns:
            检查结果
        """
        passed = value is False

        return self._add_check(
            name="false",
            description=description or f"检查是否为假",
            passed=passed,
            message="" if passed else f"值为真",
            expected=False,
            actual=value,
        )

    # ========== 快照检查 ==========

    def assert_snapshot_healthy(
        self,
        snapshot_age: float,
        max_age: float = 20.0,
    ) -> AssertionCheck:
        """
        断言快照健康

        Args:
            snapshot_age: 快照年龄（秒）
            max_age: 最大允许年龄

        Returns:
            检查结果
        """
        passed = snapshot_age < max_age

        return self._add_check(
            name="snapshot_healthy",
            description=f"检查快照是否健康（年龄 < {max_age}s）",
            passed=passed,
            message="" if passed else f"快照过期",
            expected=f"< {max_age}s",
            actual=f"{snapshot_age:.2f}s",
        )

    # ========== 自定义检查 ==========

    def register_custom_check(
        self,
        name: str,
        check_func: Callable[[], tuple[bool, str]],
    ) -> None:
        """
        注册自定义检查

        Args:
            name: 检查名称
            check_func: 检查函数，返回 (passed, message)
        """
        self._custom_checks[name] = check_func

    def run_custom_check(self, name: str) -> AssertionCheck:
        """
        运行自定义检查

        Args:
            name: 检查名称

        Returns:
            检查结果
        """
        if name not in self._custom_checks:
            return self._add_check(
                name=name,
                description=f"自定义检查: {name}",
                passed=False,
                message=f"未注册的自定义检查: {name}",
            )

        try:
            passed, message = self._custom_checks[name]()
            return self._add_check(
                name=name,
                description=f"自定义检查: {name}",
                passed=passed,
                message=message,
            )
        except Exception as e:
            return self._add_check(
                name=name,
                description=f"自定义检查: {name}",
                passed=False,
                message=f"检查执行失败: {e}",
            )

    # ========== 结果汇总 ==========

    def get_results(self) -> list[AssertionCheck]:
        """获取所有检查结果"""
        return self._checks.copy()

    def get_passed(self) -> list[AssertionCheck]:
        """获取通过的检查"""
        return [c for c in self._checks if c.result == AssertionResult.PASS]

    def get_failed(self) -> list[AssertionCheck]:
        """获取失败的检查"""
        return [c for c in self._checks if c.result == AssertionResult.FAIL]

    def get_summary(self) -> dict:
        """
        获取汇总信息

        Returns:
            汇总字典
        """
        total = len(self._checks)
        passed = len(self.get_passed())
        failed = len(self.get_failed())

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / total if total > 0 else 0,
            "all_passed": failed == 0,
        }

    def print_results(self) -> None:
        """打印所有结果"""
        print("\n" + "=" * 60)
        print("Assertion Results")
        print("=" * 60)

        for check in self._checks:
            status = "✓" if check.result == AssertionResult.PASS else "✗"
            print(f"{status} {check.name:30} {check.description}")

            if check.result == AssertionResult.FAIL:
                print(f"  Expected: {check.expected}")
                print(f"  Actual: {check.actual}")
                if check.message:
                    print(f"  Message: {check.message}")

        summary = self.get_summary()
        print("\n" + "-" * 60)
        print(f"Total: {summary['total']}")
        print(f"Passed: {summary['passed']}")
        print(f"Failed: {summary['failed']}")
        print(f"Pass Rate: {summary['pass_rate']:.1%}")
        print(f"Overall: {'PASS' if summary['all_passed'] else 'FAIL'}")
        print("=" * 60 + "\n")

    def clear(self) -> None:
        """清空所有检查"""
        self._checks.clear()


# 全局单例
_assertions: Optional[Assertions] = None


def get_assertions() -> Optional[Assertions]:
    """获取全局断言实例"""
    return _assertions


def set_assertions(assertions: Assertions) -> None:
    """设置全局断言实例"""
    global _assertions
    _assertions = assertions
