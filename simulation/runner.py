"""
SimulationRunner - 模拟交易运行器
执行测试场景，协调各个组件完成完整的交易生命周期测试
"""
from __future__ import annotations
import asyncio
import time
from typing import Optional, Callable, Any
from pathlib import Path
from loguru import logger

from .fake_exchange import FakeExchange
from .fake_position_manager import FakePositionManager, set_position_manager
from .logger import SimulationLog, EventType, set_logger
from .assertions import Assertions, set_assertions


class ScenarioResult:
    """场景执行结果"""

    def __init__(self, name: str):
        self.name = name
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.success = False
        self.error: Optional[str] = None
        self.assertions_passed = 0
        self.assertions_failed = 0

    def finish(self, success: bool, error: Optional[str] = None):
        """完成场景"""
        self.end_time = time.time()
        self.success = success
        self.error = error

    @property
    def duration(self) -> float:
        """执行时长"""
        if self.end_time:
            return self.end_time - self.start_time
        return time.time() - self.start_time


class SimulationRunner:
    """
    模拟交易运行器

    执行测试场景，协调各个组件完成完整的交易生命周期测试。
    支持：
    - 场景注册和执行
    - 自动化测试流程
    - 结果汇总和报告
    - 日志和断言集成
    """

    def __init__(
        self,
        initial_balance: float = 10000.0,
        log_dir: Optional[Path] = None,
    ):
        """
        初始化运行器

        Args:
            initial_balance: 初始余额
            log_dir: 日志目录
        """
        # 初始化核心组件
        self.exchange = FakeExchange(initial_balance)
        self.position_manager = FakePositionManager(self.exchange)
        self.log = SimulationLog(log_dir)
        self.assertions = Assertions()

        # 设置全局单例
        set_position_manager(self.position_manager)
        set_logger(self.log)
        set_assertions(self.assertions)

        # 场景注册表
        self._scenarios: dict[str, Callable] = {}

        # 执行结果
        self._results: list[ScenarioResult] = []

        # 初始化日志目录
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[SimulationRunner] 初始化完成，初始余额: {initial_balance} USDT")

    def register_scenario(self, name: str, scenario_func: Callable) -> None:
        """
        注册测试场景

        Args:
            name: 场景名称
            scenario_func: 场景函数，签名为 async def scenario(runner: SimulationRunner)
        """
        self._scenarios[name] = scenario_func
        logger.info(f"[SimulationRunner] 注册场景: {name}")

    async def run_scenario(self, name: str) -> ScenarioResult:
        """
        执行单个场景

        Args:
            name: 场景名称

        Returns:
            执行结果
        """
        if name not in self._scenarios:
            raise ValueError(f"未注册的场景: {name}")

        result = ScenarioResult(name)
        self.log.log(EventType.SYSTEM_START, f"开始执行场景: {name}")

        try:
            # 重置状态
            self.exchange.reset()
            self.assertions.clear()

            # 执行场景
            scenario_func = self._scenarios[name]
            await scenario_func(self)

            # 收集断言结果
            summary = self.assertions.get_summary()
            result.assertions_passed = summary["passed"]
            result.assertions_failed = summary["failed"]

            # 判断成功
            if summary["all_passed"]:
                result.finish(success=True)
                self.log.log(EventType.CUSTOM, f"场景执行成功: {name}")
            else:
                # 输出失败的断言详情
                failed_checks = self.assertions.get_failed()
                for check in failed_checks:
                    logger.error(f"  ✗ {check.description}: {check.message}")
                    if check.expected is not None or check.actual is not None:
                        logger.error(f"    期望: {check.expected}, 实际: {check.actual}")

                result.finish(
                    success=False,
                    error=f"断言失败: {summary['failed']}/{summary['total']}"
                )
                self.log.log(EventType.CUSTOM, f"场景执行失败: {name}")

        except Exception as e:
            result.finish(success=False, error=str(e))
            self.log.log(EventType.CUSTOM, f"场景执行异常: {name}, error={e}")
            logger.error(f"[SimulationRunner] 场景执行异常: {name}, error={e}")

        self._results.append(result)
        return result

    async def run_all(self) -> dict:
        """
        执行所有注册的场景

        Returns:
            执行汇总
        """
        self.log.log(EventType.SYSTEM_START, "开始执行所有场景")

        for name in self._scenarios.keys():
            await self.run_scenario(name)

        self.log.log(EventType.SYSTEM_STOP, "所有场景执行完成")

        return self.get_summary()

    def get_summary(self) -> dict:
        """
        获取执行汇总

        Returns:
            汇总字典
        """
        total = len(self._results)
        passed = sum(1 for r in self._results if r.success)
        failed = total - passed

        total_duration = sum(r.duration for r in self._results)
        avg_duration = total_duration / total if total > 0 else 0

        return {
            "total_scenarios": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / total if total > 0 else 0,
            "total_duration": total_duration,
            "avg_duration": avg_duration,
            "results": [
                {
                    "name": r.name,
                    "success": r.success,
                    "duration": r.duration,
                    "error": r.error,
                    "assertions_passed": r.assertions_passed,
                    "assertions_failed": r.assertions_failed,
                }
                for r in self._results
            ],
        }

    def print_summary(self) -> None:
        """打印执行汇总"""
        summary = self.get_summary()

        print("\n" + "=" * 70)
        print("Simulation Test Summary")
        print("=" * 70)
        print(f"Total Scenarios: {summary['total_scenarios']}")
        print(f"Passed: {summary['passed']}")
        print(f"Failed: {summary['failed']}")
        print(f"Pass Rate: {summary['pass_rate']:.1%}")
        print(f"Total Duration: {summary['total_duration']:.2f}s")
        print(f"Avg Duration: {summary['avg_duration']:.2f}s")
        print("\nScenario Details:")

        for result in summary["results"]:
            status = "✓" if result["success"] else "✗"
            print(f"\n{status} {result['name']}")
            print(f"  Duration: {result['duration']:.2f}s")
            print(f"  Assertions: {result['assertions_passed']} passed, {result['assertions_failed']} failed")
            if result["error"]:
                print(f"  Error: {result['error']}")

        print("\n" + "=" * 70)

        # 打印日志汇总
        self.log.print_summary()

    def export_results(self, output_dir: Path) -> None:
        """
        导出执行结果

        Args:
            output_dir: 输出目录
        """
        import json

        output_dir.mkdir(parents=True, exist_ok=True)

        # 导出汇总
        summary = self.get_summary()
        summary_file = output_dir / "summary.json"
        with open(summary_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        # 导出日志
        self.log.export_to_json("simulation_log.json")

        logger.info(f"[SimulationRunner] 结果已导出到: {output_dir}")

    async def close(self) -> None:
        """关闭运行器"""
        await self.position_manager.stop()
        logger.info("[SimulationRunner] 已关闭")


# 便捷函数
async def run_simulation(
    scenarios: dict[str, Callable],
    initial_balance: float = 10000.0,
    log_dir: Optional[Path] = None,
) -> dict:
    """
    快速运行模拟测试

    Args:
        scenarios: 场景字典 {name: scenario_func}
        initial_balance: 初始余额
        log_dir: 日志目录

    Returns:
        执行汇总
    """
    runner = SimulationRunner(initial_balance, log_dir)

    for name, func in scenarios.items():
        runner.register_scenario(name, func)

    summary = await runner.run_all()
    runner.print_summary()

    if log_dir:
        runner.export_results(log_dir)

    await runner.close()

    return summary
