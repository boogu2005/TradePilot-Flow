"""Dashboard 自有统计表的 ORM 模型（存放于独立 dashboard.db，不触碰机器人库）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DashboardEquitySnapshot(Base):
    """账户权益快照（Dashboard 自采，用于资金曲线）。"""

    __tablename__ = "dashboard_equity_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    time = Column(DateTime, nullable=False, index=True)
    balance = Column(Float, nullable=False, default=0.0)
    equity = Column(Float, nullable=False, default=0.0)
    available = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    open_positions = Column(Integer, nullable=False, default=0)
    source = Column(String(32), nullable=False, default="okx")


class OkxEquityPoint(Base):
    """每日资金曲线点（基于 OKX 账单重建，USDT 余额）。"""

    __tablename__ = "okx_equity_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    day = Column(String(10), nullable=False, unique=True, index=True)
    balance = Column(Float, nullable=False, default=0.0)
    equity = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, nullable=False)


class Transfer(Base):
    """充值/提现记录（OKX 资产流水）。收益统计一律排除转账（见 services/transfers.py）。"""

    __tablename__ = "transfers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tx_id = Column(String(64), nullable=False, unique=True, index=True)
    kind = Column(String(16), nullable=False)  # deposit / withdrawal
    ccy = Column(String(16), nullable=False, default="USDT")
    amount = Column(Float, nullable=False, default=0.0)
    ts = Column(DateTime, nullable=False, index=True)
    state = Column(String(16), nullable=False, default="completed")
    created_at = Column(DateTime, nullable=False)


class DashboardMeta(Base):
    """Dashboard 元信息（schema 识别结果等）。"""

    __tablename__ = "dashboard_meta"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), nullable=False, unique=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
