"""
SimulationLog - 可视化日志系统
记录交易生命周期的每个关键事件
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any
from dataclasses import dataclass, field
from pathlib import Path


class EventType(str, Enum):
    """事件类型"""
    # 信号事件
    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    SIGNAL_PARSED = "SIGNAL_PARSED"

    # 入场事件
    ENTRY_STARTED = "ENTRY_STARTED"
    ENTRY_FILLED = "ENTRY_FILLED"
    ENTRY_FAILED = "ENTRY_FAILED"

    # 仓位事件
    POSITION_CREATED = "POSITION_CREATED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_CLOSED = "POSITION_CLOSED"

    # 保护单事件
    SL_CREATED = "SL_CREATED"
    SL_TRIGGERED = "SL_TRIGGERED"
    SL_UPDATED = "SL_UPDATED"

    TP_CREATED = "TP_CREATED"
    TP_TRIGGERED = "TP_TRIGGERED"
    TP_PARTIAL = "TP_PARTIAL"

    TRAILING_CREATED = "TRAILING_CREATED"
    TRAILING_ACTIVATED = "TRAILING_ACTIVATED"
    TRAILING_TRIGGERED = "TRAILING_TRIGGERED"

    # 价格事件
    PRICE_UPDATE = "PRICE_UPDATE"
    PRICE_JUMP = "PRICE_JUMP"

    # 协调器事件
    RECONCILE_STARTED = "RECONCILE_STARTED"
    RECONCILE_COMPLETED = "RECONCILE_COMPLETED"
    ORPHAN_DETECTED = "ORPHAN_DETECTED"
    ORPHAN_CLEANED = "ORPHAN_CLEANED"

    # 系统事件
    SYSTEM_START = "SYSTEM_START"
    SYSTEM_STOP = "SYSTEM_STOP"
    SNAPSHOT_REFRESH = "SNAPSHOT_REFRESH"
    WS_CONNECTED = "WS_CONNECTED"
    WS_DISCONNECTED = "WS_DISCONNECTED"

    # 自定义事件
    CUSTOM = "CUSTOM"


@dataclass
class LogEntry:
    """日志条目"""
    timestamp: float
    event_type: EventType
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    symbol: Optional[str] = None
    trade_id: Optional[int] = None

    def format_time(self) -> str:
        """格式化时间"""
        dt = datetime.fromtimestamp(self.timestamp, tz=timezone.utc)
        return dt.strftime("%H:%M:%S")

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "timestamp": self.timestamp,
            "time": self.format_time(),
            "event": self.event_type.value,
            "message": self.message,
            "data": self.data,
            "symbol": self.symbol,
            "trade_id": self.trade_id,
        }


class SimulationLog:
    """
    可视化日志系统

    记录交易生命周期的每个关键事件，支持：
    - 时间线视图
    - 事件过滤
    - 导出日志
    - 统计汇总
    """

    def __init__(self, log_dir: Optional[Path] = None):
        """
        初始化日志系统

        Args:
            log_dir: 日志目录（可选）
        """
        self._entries: list[LogEntry] = []
        self._log_dir = log_dir
        self._start_time = time.time()

        # 统计
        self._event_counts: dict[EventType, int] = {}

        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event_type: EventType,
        message: str,
        data: Optional[dict] = None,
        symbol: Optional[str] = None,
        trade_id: Optional[int] = None,
    ) -> LogEntry:
        """
        记录事件

        Args:
            event_type: 事件类型
            message: 消息
            data: 附加数据
            symbol: 交易对
            trade_id: 交易ID

        Returns:
            日志条目
        """
        entry = LogEntry(
            timestamp=time.time(),
            event_type=event_type,
            message=message,
            data=data or {},
            symbol=symbol,
            trade_id=trade_id,
        )

        self._entries.append(entry)
        self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1

        # 打印到控制台
        self._print_entry(entry)

        return entry

    def _print_entry(self, entry: LogEntry) -> None:
        """打印日志条目"""
        time_str = entry.format_time()
        event_str = entry.event_type.value.ljust(20)
        symbol_str = f"[{entry.symbol}]" if entry.symbol else ""

        print(f"{time_str} | {event_str} | {symbol_str} {entry.message}")

    def get_timeline(self, event_types: Optional[list[EventType]] = None) -> list[LogEntry]:
        """
        获取时间线

        Args:
            event_types: 过滤事件类型（可选）

        Returns:
            日志条目列表
        """
        if event_types is None:
            return self._entries.copy()

        return [e for e in self._entries if e.event_type in event_types]

    def get_by_trade(self, trade_id: int) -> list[LogEntry]:
        """
        获取指定交易的日志

        Args:
            trade_id: 交易ID

        Returns:
            日志条目列表
        """
        return [e for e in self._entries if e.trade_id == trade_id]

    def get_by_symbol(self, symbol: str) -> list[LogEntry]:
        """
        获取指定交易对的日志

        Args:
            symbol: 交易对

        Returns:
            日志条目列表
        """
        return [e for e in self._entries if e.symbol == symbol]

    def get_statistics(self) -> dict:
        """
        获取统计信息

        Returns:
            统计字典
        """
        duration = time.time() - self._start_time

        return {
            "total_events": len(self._entries),
            "duration_seconds": duration,
            "event_counts": {k.value: v for k, v in self._event_counts.items()},
            "events_per_second": len(self._entries) / duration if duration > 0 else 0,
        }

    def export_to_file(self, filename: str) -> None:
        """
        导出日志到文件

        Args:
            filename: 文件名
        """
        if not self._log_dir:
            raise ValueError("未设置日志目录")

        filepath = self._log_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            for entry in self._entries:
                f.write(f"{entry.format_time()} | {entry.event_type.value:20} | ")
                if entry.symbol:
                    f.write(f"[{entry.symbol}] ")
                f.write(f"{entry.message}\n")

                if entry.data:
                    for key, value in entry.data.items():
                        f.write(f"  {key}: {value}\n")

        print(f"日志已导出到: {filepath}")

    def export_to_json(self, filename: str) -> None:
        """
        导出日志到 JSON 文件

        Args:
            filename: 文件名
        """
        import json

        if not self._log_dir:
            raise ValueError("未设置日志目录")

        filepath = self._log_dir / filename

        data = {
            "entries": [e.to_dict() for e in self._entries],
            "statistics": self.get_statistics(),
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"JSON 日志已导出到: {filepath}")

    def print_summary(self) -> None:
        """打印汇总信息"""
        stats = self.get_statistics()

        print("\n" + "=" * 60)
        print("Simulation Log Summary")
        print("=" * 60)
        print(f"Total Events: {stats['total_events']}")
        print(f"Duration: {stats['duration_seconds']:.2f}s")
        print(f"Events/sec: {stats['events_per_second']:.2f}")
        print("\nEvent Breakdown:")

        for event_type, count in sorted(
            stats["event_counts"].items(),
            key=lambda x: x[1],
            reverse=True
        ):
            print(f"  {event_type:30} {count}")

        print("=" * 60 + "\n")

    def clear(self) -> None:
        """清空日志"""
        self._entries.clear()
        self._event_counts.clear()
        self._start_time = time.time()


# 全局单例
_logger: Optional[SimulationLog] = None


def get_logger() -> Optional[SimulationLog]:
    """获取全局日志实例"""
    return _logger


def set_logger(log: SimulationLog) -> None:
    """设置全局日志实例"""
    global _logger
    _logger = log
