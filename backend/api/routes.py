"""REST API 定义。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect

from .. import config
from ..auth import (
    make_session_token,
    parse_cookie_value,
    verify_basic_header,
    verify_credentials,
    verify_session_token,
)
from ..database import schema as db_schema
from ..models import bot_models
from ..services import account as account_svc
from ..services import okx
from ..services import charts as charts_svc
from ..services import positions as positions_svc
from ..services import ranking as ranking_svc
from ..services import teacher as teacher_svc
from ..services import trades as trades_svc
from ..services import status as status_svc
from ..services import teacher_tier as teacher_tier_svc
from ..services.background import hub

# /api/* 的鉴权统一由中间件（middleware.py）执行：会话 Cookie 或 Basic 头。
# WebSocket 路由单独拆出，由 handler 手动校验。
router = APIRouter()
ws_router = APIRouter()


@router.get("/me")
def get_me():
    """当前登录态探针（能到达此处即已通过中间件鉴权）。"""
    return {"authed": True, "username": config.DASHBOARD_USERNAME}


@router.post("/login")
async def login(request: Request, response: Response):
    """登录：校验账密后签发 HttpOnly 会话 Cookie。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    if not verify_credentials(username, password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response.set_cookie(
        "dash_auth",
        make_session_token(),
        path="/",
        httponly=True,
        samesite="lax",
        max_age=12 * 3600,
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    """登出：清除会话 Cookie。"""
    response.delete_cookie("dash_auth", path="/")
    return {"ok": True}


@router.get("/health")
def health():
    return {
        "status": "ok",
        "bot_db_available": bot_models.is_available(),
        "live_okx_enabled": okx.enabled(),
    }


@router.get("/meta/schema")
def meta_schema():
    """数据库结构自动识别结果。"""
    return {
        "bot_db_available": bot_models.is_available(),
        "tables": db_schema.detect_tables(),
        "teacher_field": bot_models.teacher_column() or "teacher",
        "used_fields": {
            "trades": [
                "id", "pair", "is_open", "is_short", "open_rate", "close_rate",
                "realized_profit", "close_profit_abs", "stake_amount", "amount",
                "quantity", "margin", "leverage", "stop_loss", "open_date",
                "close_date", "exit_reason", "strategy", "tp1_price",
                "tp1_filled_at", "position_state", "teacher",
            ],
            "account_snapshots": [
                "time", "balance", "equity", "available", "unrealized_pnl",
                "realized_pnl", "open_positions",
            ],
        },
    }


@router.get("/account")
async def get_account():
    return await account_svc.get_account_summary()


@router.get("/account/history")
def get_account_history(
    days: int = Query(30, ge=1, le=3650),
    source: str = Query("auto", pattern="^(auto|bot|dashboard)$"),
):
    return {"days": days, "points": account_svc.get_account_history(days=days, source=source)}


@router.get("/positions")
async def get_positions(live: bool = True):
    return {"items": await positions_svc.get_positions(use_live=live)}


@router.get("/trades")
def get_trades(
    teacher: str | None = None,
    pair: str | None = None,
    start: str | None = None,
    end: str | None = None,
    direction: str | None = Query(None, pattern="^(LONG|SHORT|BUY|SELL)$", description="LONG/SHORT"),
    result: str | None = Query(None, pattern="^(win|loss)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    return trades_svc.list_trades(
        teacher=teacher,
        pair=pair,
        start=start,
        end=end,
        direction=direction,
        result=result,
        page=page,
        page_size=page_size,
    )


@router.get("/teachers")
def get_teachers(limit: int = Query(200, ge=1, le=1000)):
    return {"items": ranking_svc.list_teachers(limit=limit)}


@router.get("/teachers/ranking")
def get_teachers_ranking(
    period: int = Query(30, ge=1, le=365, description="统计周期(天)"),
    type: str = Query("profit", pattern="^(profit|profit_factor|win_rate|roi|risk_adjusted)$"),
    limit: int = Query(config.RANKING_DEFAULT_LIMIT, ge=1, le=200),
):
    if type == "risk_adjusted":
        items = ranking_svc.get_risk_adjusted_ranking(period=period, limit=limit)
    else:
        items = ranking_svc.get_teacher_ranking(period=period, sort_by=type, limit=limit)
    # UI 增强：附加当月快照锁定的仓位档位（实时排名 ≠ 交易档位）
    teacher_tier_svc.attach_current_tier(items)
    return {
        "period": period,
        "type": type,
        "items": items,
    }


@router.get("/teachers/{teacher_name}")
def get_teacher_detail(
    teacher_name: str,
    days: int = Query(0, ge=0, le=3650, description="0=全周期"),
):
    return teacher_svc.get_teacher_detail(teacher=teacher_name, days=days)


@router.get("/teachers/{teacher_name}/trades")
def get_teacher_trades(
    teacher_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    return teacher_svc.get_teacher_trades(teacher=teacher_name, page=page, page_size=page_size)


@router.get("/charts/equity")
def get_chart_equity(days: int = Query(30, ge=1, le=3650)):
    return charts_svc.get_equity_curve(days=days)


@router.get("/charts/daily-pnl")
def get_chart_daily_pnl(days: int = Query(30, ge=1, le=3650)):
    return charts_svc.get_daily_pnl(days=days)


@router.get("/charts/pnl-distribution")
def get_chart_pnl_distribution(bins: int = Query(10, ge=3, le=30)):
    return charts_svc.get_pnl_distribution(bins=bins)


@router.get("/bot/status")
async def get_bot_status(lines: int = Query(200, ge=1, le=500)):
    """机器人运行状态（纯只读）：systemd 服务态 + 日志尾部 + 今日概况。"""
    return await asyncio.to_thread(status_svc.get_bot_status, max_lines=lines)


@ws_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """实时推送：账户 + 持仓快照，每 5s 一次。"""
    # WebSocket 手动校验：Authorization Basic 头 或 签名会话 Cookie（浏览器
    # 对同源 WS 握手必然自动携带 Cookie，故 Cookie 是可靠的鉴权通道）
    auth_ok = verify_basic_header(websocket.headers.get("authorization")) or (
        verify_session_token(
            parse_cookie_value(websocket.headers.get("cookie"), "dash_auth")
        )
    )
    if not auth_ok:
        await websocket.close(code=1008)
        return
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
    except Exception:
        await hub.disconnect(websocket)
