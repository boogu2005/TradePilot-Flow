"""Trading Bot Web Dashboard - FastAPI 入口。

启动方式：
    cd backend
    ..\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import config
from .api.routes import router, ws_router
from .database import db, schema
from .middleware import DashboardAuthMiddleware
from .models import bot_models
from .services import background, equity, okx


async def _run_background_tasks() -> None:
    await asyncio.gather(
        background.equity_snapshotter(),
        background.ws_broadcaster(),
        equity.equity_curve_builder(),
        return_exceptions=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate_auth_config()
    # 初始化数据库连接
    db.init_db()
    # 自动识别机器人 schema 并映射表
    bot_models.init_bot_models()
    # Dashboard 自有统计表迁移
    schema.ensure_dashboard_schema()
    schema.record_meta(
        "teacher_field", bot_models.teacher_column() or "teacher"
    )
    logger.info(
        f"Dashboard 就绪 | 机器人库可用={bot_models.is_available()} | "
        f"老师字段={bot_models.teacher_column()} | OKX实时={okx.enabled()}"
    )
    task = asyncio.create_task(_run_background_tasks())
    try:
        yield
    finally:
        task.cancel()
        await okx.close()
        db.close_db()


app = FastAPI(
    title="Trading Bot Dashboard",
    version="1.0.0",
    description="加密货币交易机器人监控后台（OKX · 带单老师排行榜）",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Basic Auth 强制 + 会话 Cookie 签发（置于 CORS 外层）
app.add_middleware(DashboardAuthMiddleware)

app.include_router(router, prefix="/api")
app.include_router(ws_router, prefix="/api")


# 生产模式：如果前端已构建（frontend/dist），则托管 SPA。
# 静态资源（/assets）走 StaticFiles；其余非 /api 路径一律回退 index.html，
# 保证 React BrowserRouter 的深链接（/login /trades /status …）刷新/直达不 404。
_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _dist.is_dir():
    _assets_dir = _dist / "assets"
    if _assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_assets_dir)),
            name="assets",
        )
    _index_html = _dist / "index.html"

    @app.get("/", include_in_schema=False)
    async def spa_index():
        return FileResponse(_index_html)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # /api/* 与 /ws 的未知路径保持 API 404 语义，不落入 SPA
        if full_path.startswith("api/") or full_path.startswith("ws/"):
            raise HTTPException(status_code=404)
        candidate = _dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_index_html)

    logger.info(f"已托管前端构建产物(SPA): {_dist}")


@app.get("/")
def root():
    return {"name": "Trading Bot Dashboard", "docs": "/docs", "api": "/api/health"}
