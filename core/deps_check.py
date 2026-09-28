"""
依赖预检查 — 在启动任何异步服务前，验证系统依赖是否齐全。

目前检查：
1. 代理依赖（python-socks）：配置了 EXCHANGE_PROXY 时必需
2. 基础 Python 包：ccxt / telethon / sqlalchemy / loguru / dotenv

设计原则：
- 任何缺失依赖都打印清晰提示 + 安全退出（return False）
- 绝不抛 Traceback
- 退出码 1，让 .bat 脚本能感知
"""
from __future__ import annotations

import os
import sys
import importlib
from urllib.parse import urlparse

# Windows 控制台 GBK 兼容：本模块可能在 setup_logging() 之前被调用
# 必须先保证 stdout 支持 UTF-8，否则打印 emoji/中文 会 UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from loguru import logger


# —————— 代理依赖检查 ——————

def _check_proxy_deps() -> bool:
    """
    检查代理依赖：
    - 配置了 EXCHANGE_PROXY → 需要 python-socks（Telethon 异步代理）
    - 未配置代理 → 跳过
    """
    proxy = os.getenv("EXCHANGE_PROXY", "").strip()
    if not proxy:
        return True

    u = urlparse(proxy)
    scheme = (u.scheme or "http").lower()
    needs_socks = scheme.startswith("socks")

    # Telethon 对任何代理（http/socks）都会尝试用 python-socks
    # HTTP 代理在无 python-socks 时会 fallback 到 aiohttp，但 Telethon 会发警告
    # 为确保稳定，只要有代理就检查 python-socks
    try:
        importlib.import_module("python_socks")
    except ImportError:
        print("\n" + "=" * 60)
        print("❌ 缺少代理依赖：python-socks")
        print(f"   代理配置: {proxy}")
        print(f"   代理类型: {scheme.upper()}")
        print("   Telethon 异步代理需要 python-socks 包")
        print("")
        print("   请执行: pip install python-socks")
        print("=" * 60 + "\n")
        logger.error("缺少 python-socks，无法使用代理。请执行: pip install python-socks")
        return False

    return True


# —————— 基础包检查 ——————

_REQUIRED_PACKAGES = [
    ("ccxt", "ccxt"),
    ("telethon", "telethon"),
    ("sqlalchemy", "sqlalchemy"),
    ("loguru", "loguru"),
    ("dotenv", "python-dotenv"),
    ("openai", "openai"),
]


def _check_core_deps() -> bool:
    """检查核心依赖包是否安装。"""
    missing = []
    for module, package in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)

    if missing:
        print("\n" + "=" * 60)
        print("❌ 缺少核心依赖包:")
        for p in missing:
            print(f"   - {p}")
        print("")
        print("   请执行: pip install -r requirements.txt")
        print("=" * 60 + "\n")
        logger.error(f"缺少核心依赖: {', '.join(missing)}")
        return False

    return True


# —————— 主入口 ——————

def check_all_dependencies() -> bool:
    """
    启动前统一依赖检查。
    返回 True = 全部通过，可以启动
    返回 False = 有缺失，应安全退出
    """
    logger.info("[依赖检查] 开始...")

    if not _check_core_deps():
        return False

    if not _check_proxy_deps():
        return False

    logger.success("[依赖检查] 全部通过")
    return True
