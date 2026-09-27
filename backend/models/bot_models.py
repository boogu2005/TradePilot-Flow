"""机器人库表的动态映射（只读）。

通过 autoload 动态加载机器人库真实表结构，
保证与机器人实际 schema 保持一致，字段不足时自动降级。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import MetaData, Table, inspect
from sqlalchemy.engine import Engine


_metadata = MetaData()
_tables: dict[str, Table | None] = {}
_teacher_column: str | None = None
_available: bool = True


def _load(engine: Engine) -> None:
    global _teacher_column, _available
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        for table_name in (
            "trades",
            "close_history",
            "account_snapshots",
            "daily_statistics",
            "teacher_stats",
            "system_events",
            "orders",
            "executions",
        ):
            if table_name in names:
                _tables[table_name] = Table(table_name, _metadata, autoload_with=engine)
            else:
                _tables[table_name] = None
        if "trades" in names:
            cols = {c["name"] for c in inspector.get_columns("trades")}
            _teacher_column = "teacher" if "teacher" in cols else (
                "teacher_name" if "teacher_name" in cols else None
            )
        _available = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"机器人表映射失败: {exc}")
        _available = False


def init_bot_models() -> None:
    from ..database import db as _db

    _load(_db.bot_session().bind)


def get_table(name: str) -> Table | None:
    return _tables.get(name)


def teacher_column() -> str | None:
    return _teacher_column


def is_available() -> bool:
    return _available and bool(_tables)
