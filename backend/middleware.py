"""Dashboard 会话鉴权中间件（纯 ASGI）。

放行策略：
- WebSocket：直接透传（其鉴权在 /api/ws handler 内校验 Cookie）。
- OPTIONS：透传（CORS 预检）。
- /api/login、/api/logout：公开（登录/登出本身无需凭证）。
- 其余 /api/*：需有效会话 Cookie 或 Basic 头，否则返回 JSON 401
  （不返回 WWW-Authenticate: Basic，避免触发浏览器原生登录框）。
- 其余路径（/、/assets/* 等 SPA 静态文件）：公开加载，登录页才可显示。

使用纯 ASGI 实现而非 BaseHTTPMiddleware，避免其不支持 WebSocket 的问题。
"""
from __future__ import annotations

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import (
    auth_enabled,
    make_session_token,
    parse_cookie_value,
    verify_basic_header,
    verify_session_token,
)

_COOKIE_NAME = "dash_auth"
_PUBLIC_API_PATHS = {"/api/login", "/api/logout"}


class DashboardAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # WebSocket 透传，由 handler 自行鉴权
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # 配置缺失时失败关闭，即使 ASGI lifespan 被禁用也不公开账户数据。
        if not auth_enabled():
            await JSONResponse(
                status_code=503,
                content={"detail": "Dashboard authentication is not configured"},
            )(scope, receive, send)
            return
        # CORS 预检直接放行
        if scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        # 仅保护 /api/*（登录/登出端点除外），SPA 静态文件公开加载
        if not (path.startswith("/api/") and path not in _PUBLIC_API_PATHS):
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        authz = headers.get("authorization")
        cookie = headers.get("cookie")

        if verify_basic_header(authz):
            # 凭 Basic 头通过 -> 放行并顺手续签会话 Cookie
            token = make_session_token()

            async def send_with_cookie(message: dict) -> None:
                if message["type"] == "http.response.start":
                    raw_headers = list(message.get("headers", []))
                    raw_headers.append(
                        (
                            b"set-cookie",
                            (
                                f"{_COOKIE_NAME}={token}; Path=/; HttpOnly; "
                                "SameSite=Lax; Max-Age=43200"
                            ).encode(),
                        )
                    )
                    message = {**message, "headers": raw_headers}
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return

        if verify_session_token(parse_cookie_value(cookie, _COOKIE_NAME)):
            # 已持有有效会话 Cookie -> 放行
            await self.app(scope, receive, send)
            return

        # 无有效凭证 -> JSON 401（不带 WWW-Authenticate，前端跳转登录页）
        await JSONResponse(
            status_code=401,
            content={"detail": "未登录或会话已过期"},
        )(scope, receive, send)
