"""Dashboard 登录认证（会话 Cookie + Basic 兼容）。

凭证来自机器人根目录 .env 的 DASHBOARD_USERNAME / DASHBOARD_PASSWORD。
- 登录：POST /api/login 校验后签发 HMAC 签名会话 Cookie（HttpOnly）。
- 鉴权：中间件对 /api/* 校验 Cookie（浏览器自动携带）或 Basic 头。
- WebSocket：浏览器对同源 WS 握手必然携带 Cookie，handler 据此校验。
全程使用标准库，无额外依赖。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from . import config

_SESSION_TTL_SECONDS = 12 * 3600  # 会话有效期 12 小时
_COOKIE_NAME = "dash_auth"


def auth_enabled() -> bool:
    """Only a complete private credential set may authorize dashboard access."""
    return bool(config.DASHBOARD_USERNAME and config.DASHBOARD_PASSWORD
                and len(config.DASHBOARD_AUTH_SECRET) >= 32)


def verify_credentials(username: str, password: str) -> bool:
    """校验账号密码（登录端点使用）。"""
    if not auth_enabled():
        return False
    return _basic_ok(username, password)


def verify_basic_header(auth_header: str | None) -> bool:
    """校验 Authorization: Basic 头（HTTP 与 WebSocket 通用）。"""
    if not auth_enabled():
        return False
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
        user, _, password = decoded.partition(":")
    except Exception:
        return False
    return _basic_ok(user, password)


def _basic_ok(user: str, password: str) -> bool:
    user_ok = secrets.compare_digest(
        user.encode("utf-8"), config.DASHBOARD_USERNAME.encode("utf-8")
    )
    pass_ok = secrets.compare_digest(
        password.encode("utf-8"), config.DASHBOARD_PASSWORD.encode("utf-8")
    )
    return user_ok and pass_ok


# ---------- 签名会话 Cookie ----------

def _sign(payload: str) -> str:
    return hmac.new(
        config.DASHBOARD_AUTH_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_session_token() -> str:
    """签发 12 小时有效的签名令牌。"""
    expires = int(time.time()) + _SESSION_TTL_SECONDS
    sig = _sign(str(expires))
    return f"{expires}.{sig}"


def verify_session_token(token: str | None) -> bool:
    """校验签名令牌（含有效期）。"""
    if not auth_enabled() or not token:
        return False
    try:
        expires, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(expires)):
            return False
        return int(expires) > int(time.time())
    except Exception:
        return False


def parse_cookie_value(cookie_header: str | None, key: str) -> str | None:
    """从 Cookie 头解析指定键的值。"""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == key:
            return value
    return None
