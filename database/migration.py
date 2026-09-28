"""
数据库自动迁移模块。

支持增量添加新表和字段，不影响现有数据。
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from loguru import logger

from .models import Base


def run_migration(engine: Engine) -> None:
    """
    执行数据库迁移。

    1. 检查并创建缺失的表
    2. 检查并添加缺失的字段
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # 获取所有定义的表
    defined_tables = set(Base.metadata.tables.keys())

    # 1. 创建缺失的表
    tables_to_create = defined_tables - existing_tables
    if tables_to_create:
        logger.info(f"发现 {len(tables_to_create)} 个新表需要创建: {tables_to_create}")
        for table_name in tables_to_create:
            table = Base.metadata.tables[table_name]
            table.create(engine)
            logger.success(f"表 {table_name} 创建成功")
    else:
        logger.info("所有表已存在，无需创建新表")

    # 2. 检查并添加缺失的字段
    for table_name in defined_tables & existing_tables:
        table = Base.metadata.tables[table_name]
        existing_columns = set(col["name"] for col in inspector.get_columns(table_name))
        defined_columns = set(col.name for col in table.columns)

        missing_columns = defined_columns - existing_columns
        if missing_columns:
            logger.info(f"表 {table_name} 发现 {len(missing_columns)} 个缺失字段: {missing_columns}")
            for col_name in missing_columns:
                col = table.columns[col_name]
                _add_column(engine, table_name, col)


def _add_column(engine: Engine, table_name: str, column) -> None:
    """为表添加新字段"""
    col_type = column.type.compile(engine.dialect)
    nullable = "NULL" if column.nullable else "NOT NULL"

    # 处理默认值
    default_clause = ""
    if column.default is not None:
        if hasattr(column.default, "arg"):
            default_val = column.default.arg
            if isinstance(default_val, str):
                default_clause = f" DEFAULT '{default_val}'"
            elif isinstance(default_val, (int, float)):
                default_clause = f" DEFAULT {default_val}"
            elif callable(default_val):
                # 对于函数默认值（如 _utc_now），使用数据库函数
                if "datetime" in str(default_val).lower() or "now" in str(default_val).lower():
                    default_clause = " DEFAULT CURRENT_TIMESTAMP"
        elif column.default.is_scalar:
            default_val = column.default.arg
            if isinstance(default_val, str):
                default_clause = f" DEFAULT '{default_val}'"
            elif isinstance(default_val, (int, float)):
                default_clause = f" DEFAULT {default_val}"

    sql = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type} {nullable}{default_clause}"

    try:
        with engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
        logger.success(f"字段 {table_name}.{column.name} 添加成功")
    except Exception as e:
        logger.error(f"添加字段 {table_name}.{column.name} 失败: {e}")


def check_schema_integrity(engine: Engine) -> dict:
    """
    检查数据库schema完整性。

    返回: {
        "missing_tables": [...],
        "missing_columns": {...},
        "status": "ok" | "needs_migration"
    }
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    defined_tables = set(Base.metadata.tables.keys())

    missing_tables = defined_tables - existing_tables
    missing_columns = {}

    for table_name in defined_tables & existing_tables:
        table = Base.metadata.tables[table_name]
        existing_columns = set(col["name"] for col in inspector.get_columns(table_name))
        defined_columns = set(col.name for col in table.columns)
        missing = defined_columns - existing_columns
        if missing:
            missing_columns[table_name] = missing

    status = "needs_migration" if (missing_tables or missing_columns) else "ok"

    return {
        "missing_tables": list(missing_tables),
        "missing_columns": missing_columns,
        "status": status,
    }
