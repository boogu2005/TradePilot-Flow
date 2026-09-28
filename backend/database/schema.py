"""Schema 自动识别 + Dashboard 自有统计表迁移。

功能：
1. 扫描机器人库全部表与字段；
2. 自动识别老师字段（teacher / teacher_name）；
3. 识别交易表、持仓字段、资金快照字段；
4. 在 Dashboard 自有库中按需创建统计表（若机器人库字段不足）。
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    text,
)

from . import db


def detect_tables() -> dict:
    """扫描机器人库的表结构与行数。"""
    session = db.bot_session()
    try:
        inspector = inspect(session.bind)
        tables = inspector.get_table_names()
        result = {}
        for name in sorted(tables):
            columns = [
                {"name": col["name"], "type": str(col["type"]), "nullable": col.get("nullable")}
                for col in inspector.get_columns(name)
            ]
            try:
                count = session.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0
            except Exception:
                count = -1
            result[name] = {"columns": columns, "row_count": count}
        return result
    finally:
        session.close()


def resolve_teacher_column(table_name: str = "trades") -> str | None:
    """自动识别老师字段名：优先 teacher，其次 teacher_name。"""
    session = db.bot_session()
    try:
        inspector = inspect(session.bind)
        if table_name not in inspector.get_table_names():
            return None
        cols = {c["name"].lower() for c in inspector.get_columns(table_name)}
        for candidate in ("teacher", "teacher_name"):
            if candidate in cols:
                return candidate
        return None
    finally:
        session.close()


# ---------- Dashboard 自有统计表 ----------

_meta = MetaData()

dashboard_equity_snapshots = Table(
    "dashboard_equity_snapshots",
    _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("time", DateTime, nullable=False, index=True),
    Column("balance", Float, nullable=False, default=0.0),
    Column("equity", Float, nullable=False, default=0.0),
    Column("available", Float, nullable=False, default=0.0),
    Column("unrealized_pnl", Float, nullable=False, default=0.0),
    Column("realized_pnl", Float, nullable=False, default=0.0),
    Column("open_positions", Integer, nullable=False, default=0),
    Column("source", String(32), nullable=False, default="okx"),
)

okx_equity_points = Table(
    "okx_equity_points",
    _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("day", String(10), nullable=False, unique=True, index=True),
    Column("balance", Float, nullable=False, default=0.0),
    Column("equity", Float, nullable=False, default=0.0),
    Column("updated_at", DateTime, nullable=False),
)

# 记录字段不足时的补偿映射，便于排障
dashboard_meta = Table(
    "dashboard_meta",
    _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key", String(64), nullable=False, unique=True),
    Column("value", String(255), nullable=False),
    Column("updated_at", DateTime, nullable=False, default=datetime.utcnow),
)


def ensure_dashboard_schema() -> None:
    """在 Dashboard 自有库中创建统计表（自动迁移，重复执行安全）。"""
    engine = db.dashboard_engine()
    _meta.create_all(engine)
    logger.info("Dashboard 自有统计表就绪")


def record_meta(key: str, value: str) -> None:
    """记录 schema 识别结果，便于在 /api/meta/schema 中展示。"""
    try:
        session = db.dashboard_session()
        try:
            stmt = dashboard_meta.insert().values(
                key=key, value=value, updated_at=datetime.utcnow()
            )
            session.execute(stmt)
            session.commit()
        except Exception:
            session.rollback()
            session.execute(
                dashboard_meta.update()
                .where(dashboard_meta.c.key == key)
                .values(value=value, updated_at=datetime.utcnow())
            )
            session.commit()
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"record_meta 失败: {exc}")
