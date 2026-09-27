"""数据库引擎与会话管理。

设计原则：
1. 机器人 SQLite 库以 **只读** 方式打开，保证 Dashboard 绝不修改交易数据；
2. Dashboard 自有统计表存放在独立的 dashboard.db 中；
3. 机器人库不存在时不报错，回退到内存空库，API 返回空数据。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .. import config


def _sqlite_ro_url(url: str) -> str:
    """把 sqlite url 转为只读 url（mode=ro&uri=true）。"""
    if "sqlite:///" not in url:
        return url
    body = url.split("sqlite:///", 1)[1]
    path_part = body.split("?", 1)[0]
    if path_part.startswith("file:"):
        file_path = path_part[len("file:"):]
    else:
        file_path = path_part
    return f"sqlite:///file:{quote(file_path)}?mode=ro&uri=true"


_bot_engine: Engine | None = None
_dash_engine: Engine | None = None
_bot_session_factory: sessionmaker | None = None
_dash_session_factory: sessionmaker | None = None

bot_db_available: bool = True
bot_db_path: str = ""


def _enable_wal(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-16000")
        cursor.close()


def _extract_sqlite_path(url: str) -> str | None:
    if "sqlite:///" not in url:
        return None
    body = url.split("sqlite:///", 1)[1]
    path_part = body.split("?", 1)[0]
    if path_part.startswith("file:"):
        path_part = path_part[len("file:"):]
    return path_part


def init_db() -> None:
    """初始化两个引擎。幂等，可重复调用。"""
    global _bot_engine, _dash_engine, _bot_session_factory, _dash_session_factory
    global bot_db_available, bot_db_path

    if _bot_engine is not None:
        return

    # ---------- 机器人库（只读） ----------
    path = _extract_sqlite_path(config.BOT_DATABASE_URL)
    if path:
        bot_db_path = path
        if Path(path).is_file():
            bot_db_available = True
            ro_url = _sqlite_ro_url(config.BOT_DATABASE_URL)
            logger.info(f"连接机器人数据库(只读): {path}")
            _bot_engine = create_engine(
                ro_url, poolclass=NullPool, connect_args={"timeout": 30}
            )
        else:
            bot_db_available = False
            logger.warning(f"机器人数据库不存在，使用空库: {path}")
            _bot_engine = create_engine("sqlite://", poolclass=NullPool)
    else:
        bot_db_available = True
        logger.info("使用非 SQLite 数据库（按机器人 url 连接）")
        _bot_engine = create_engine(config.BOT_DATABASE_URL, poolclass=NullPool)

    # ---------- Dashboard 自有库（可写） ----------
    dash_path = _extract_sqlite_path(config.DASHBOARD_DATABASE_URL)
    if dash_path:
        Path(dash_path).parent.mkdir(parents=True, exist_ok=True)
    _dash_engine = create_engine(
        config.DASHBOARD_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    _enable_wal(_dash_engine)

    _bot_session_factory = sessionmaker(bind=_bot_engine, expire_on_commit=False)
    _dash_session_factory = sessionmaker(bind=_dash_engine, expire_on_commit=False)


def bot_session() -> Session:
    if _bot_session_factory is None:
        init_db()
    return _bot_session_factory()  # type: ignore[return-value]


def dashboard_session() -> Session:
    if _dash_session_factory is None:
        init_db()
    return _dash_session_factory()  # type: ignore[return-value]


def dashboard_engine() -> Engine:
    if _dash_engine is None:
        init_db()
    return _dash_engine  # type: ignore[return-value]


def close_db() -> None:
    global _bot_engine, _dash_engine, _bot_session_factory, _dash_session_factory
    for engine in (_bot_engine, _dash_engine):
        if engine is not None:
            engine.dispose()
    _bot_engine = _dash_engine = None
    _bot_session_factory = _dash_session_factory = None
