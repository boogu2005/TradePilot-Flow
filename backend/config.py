"""Dashboard 配置。

读取交易机器人根目录下的 .env（OKX 凭证、代理、数据库地址等），
并解析出 Dashboard 自身需要的配置项。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# backend/ 的上一级即机器人项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 加载机器人根目录的 .env（已存在的值不覆盖）
_dotenv_path = PROJECT_ROOT / ".env"
if _dotenv_path.exists():
    load_dotenv(_dotenv_path, override=False)
    logger.info(f"加载配置: {_dotenv_path}")
else:
    logger.warning("未找到机器人 .env，Dashboard 将使用环境变量/默认值")


def _resolve_db_path(url: str) -> str:
    """把 sqlite 相对路径解析为绝对路径。"""
    if not url.startswith("sqlite"):
        return url
    # sqlite:///相对路径 或 sqlite:///file:绝对路径?mode=...
    body = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else url
    path_part = body.split("?", 1)[0]
    if path_part.startswith("file:"):
        path_part = path_part[len("file:"):]
    if not Path(path_part).is_absolute():
        resolved = (PROJECT_ROOT / path_part).resolve()
        return url.replace(body.split("?", 1)[0], str(resolved), 1)
    return url


BOT_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///user_data/trading_bot.db")
BOT_DATABASE_URL = _resolve_db_path(BOT_DATABASE_URL)

# Dashboard 自己的数据库（存放 Dashboard 专属统计表，绝不写入机器人库）
DASHBOARD_DATABASE_URL = os.getenv(
    "DASHBOARD_DATABASE_URL",
    f"sqlite:///{(PROJECT_ROOT / 'user_data' / 'dashboard.db').as_posix()}",
)

# OKX 实时数据（可选）。关闭时 Dashboard 纯读数据库。
LIVE_OKX_ENABLED = os.getenv("DASHBOARD_LIVE_OKX", "1").lower() in ("1", "true", "yes", "on")
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
EXCHANGE_PROXY = os.getenv("EXCHANGE_PROXY", "")

# 心跳判定：最近一次系统事件/成交在多少秒内视为机器人在线
BOT_ONLINE_WINDOW_SECONDS = int(os.getenv("DASHBOARD_ONLINE_WINDOW", "300"))

# 资金曲线：基于 OKX 账单重建的历史天数 + 周期刷新间隔（秒）
EQUITY_CURVE_DAYS = int(os.getenv("DASHBOARD_EQUITY_DAYS", "30"))
EQUITY_REFRESH_SECONDS = int(os.getenv("DASHBOARD_EQUITY_REFRESH", "1800"))

# 后台快照任务：每隔多少秒记录一次账户权益
SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("DASHBOARD_SNAPSHOT_INTERVAL", "60"))

# 排行榜默认返回条数
RANKING_DEFAULT_LIMIT = int(os.getenv("DASHBOARD_RANKING_LIMIT", "50"))

# CORS 允许来源（开发环境 Vite 默认端口 5173）
CORS_ORIGINS = os.getenv(
    "DASHBOARD_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
).split(",")

APP_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

# Dashboard 登录认证（Basic Auth）
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
# 会话 Cookie 签名密钥（HMAC），用于 WebSocket 实时推送鉴权
DASHBOARD_AUTH_SECRET = os.getenv("DASHBOARD_AUTH_SECRET", "")


def validate_auth_config() -> None:
    """Never serve private account data with the dashboard login disabled."""
    if not DASHBOARD_USERNAME or not DASHBOARD_PASSWORD:
        raise RuntimeError("Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD in the private .env")
    if len(DASHBOARD_AUTH_SECRET) < 32:
        raise RuntimeError("Set DASHBOARD_AUTH_SECRET to a random value of at least 32 characters")

# 机器人状态页（只读来源）：systemd 服务名 + 日志文件路径
BOT_SERVICE_NAME = os.getenv("DASHBOARD_BOT_UNIT", "trading-bot.service")
BOT_LOG_PATH = os.getenv(
    "DASHBOARD_BOT_LOG",
    str(PROJECT_ROOT / "user_data" / "logs" / "systemd.log"),
)
