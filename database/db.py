"""数据库初始化 & 会话管理。

⚠️ 连接池策略：使用 NullPool（SQLite 推荐）。
SQLite 自身通过 WAL 模式 + 文件锁管理并发，QueuePool 反而容易引发死锁。
每个 Session 独立创建/关闭连接，无池化争用。

🆕 迁移模式：支持增量迁移，自动添加新表和新字段，不影响现有数据。
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool

from loguru import logger

from .models import Base
from .migration import run_migration

_engine = None
_SessionFactory: sessionmaker | None = None


def init_db(database_url: str | None = None):
    global _engine, _SessionFactory
    # 防重复初始化：如果 Engine 已存在则跳过
    if _engine is not None and _SessionFactory is not None:
        logger.debug("数据库已初始化，跳过重复 init_db()")
        return
    import os
    if database_url is None:
        database_url = os.getenv("DATABASE_URL", "sqlite:///user_data/trading_bot.db")
    os.makedirs("user_data", exist_ok=True)

    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args = {"timeout": 30, "check_same_thread": False}
    _engine = create_engine(database_url, echo=False,
                            poolclass=NullPool,
                            connect_args=connect_args)
    if database_url.startswith("sqlite"):
        from sqlalchemy import event as sa_event
        @sa_event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            # P1: SQLite 官方文档推荐的 7×24 长期运行配置
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # P1: 自动 checkpoint，防止 WAL 文件无限增长（SQLite 官方文档）
            cursor.execute("PRAGMA wal_autocheckpoint=100")
            # P1: 增加页面缓存，减少磁盘 IO（SQLite 官方文档推荐）
            cursor.execute("PRAGMA cache_size=-64000")  # 64MB
            # P1: 内存映射文件，减少系统调用（SQLite 官方文档推荐）
            cursor.execute("PRAGMA mmap_size=268435456")  # 256MB
            cursor.close()
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)

    # 执行迁移：创建新表、添加新字段
    run_migration(_engine)
    logger.success("数据库迁移完成")


def get_session() -> Session:
    if _SessionFactory is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _SessionFactory()


def close_db():
    """[sync] 关闭数据库连接。engine.dispose() 是同步操作。"""
    global _engine
    if _engine:
        _engine.dispose()
        _engine = None
