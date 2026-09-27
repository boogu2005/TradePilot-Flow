"""
配置加载器 — 从 config.json 加载，缺失字段用默认值补全。
"""
from __future__ import annotations

import json
import os
from copy import deepcopy

# 项目根目录（config_loader.py 位于 core/ 下，根目录为其父目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG = {
    "risk": {
        "default_stoploss_pct": 0.02,
        "max_position_pct": 5.0,
        "tp1": {
            "enabled": True,
            "profit_pct": 0.03,
            "close_pct": 30,
            "move_sl_to_breakeven": True,
        },
        "tp2": {
            "enabled": True,
            "profit_pct": 0.06,
            "close_pct": 50,
        },
        "trailing": {
            "enabled": True,
            "activate_profit_pct": 0.06,
            "lock_gap_pct": 0.06,
        },
        "roi": {
            "enabled": True,
            "rules": {
                "2880": 0.01,
                "4320": 0.00,
            },
            "pause_when_hybrid": True,
        },
        "max_hold": {
            "enabled": True,
            "hours": 168,
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = deepcopy(val)
    return result


def _override_from_env(config: dict) -> dict:
    env_map = {
        "TP1_ENABLED": ("risk", "tp1", "enabled"),
        "TP1_PROFIT_PCT": ("risk", "tp1", "profit_pct"),
        "TP1_CLOSE_PCT": ("risk", "tp1", "close_pct"),
        "TP1_MOVE_SL_TO_BREAKEVEN": ("risk", "tp1", "move_sl_to_breakeven"),
        "TP2_ENABLED": ("risk", "tp2", "enabled"),
        "TP2_PROFIT_PCT": ("risk", "tp2", "profit_pct"),
        "TP2_CLOSE_PCT": ("risk", "tp2", "close_pct"),
        "TRAILING_ENABLED": ("risk", "trailing", "enabled"),
        "TRAILING_ACTIVATE_PROFIT_PCT": ("risk", "trailing", "activate_profit_pct"),
        "TRAILING_LOCK_GAP_PCT": ("risk", "trailing", "lock_gap_pct"),
        "ROI_ENABLED": ("risk", "roi", "enabled"),
        "MAX_HOLD_ENABLED": ("risk", "max_hold", "enabled"),
        "MAX_HOLD_HOURS": ("risk", "max_hold", "hours"),
        "DEFAULT_STOP_LOSS_PCT": ("risk", "default_stoploss_pct"),
    }
    for env_key, path_parts in env_map.items():
        val = os.getenv(env_key)
        if val is None:
            continue
        parent = config
        for part in path_parts[:-1]:
            parent = parent.setdefault(part, {})
        key = path_parts[-1]
        raw = val.strip()
        if raw.lower() in ("true", "false"):
            parent[key] = raw.lower() == "true"
        elif "." in raw:
            try:
                parent[key] = float(raw)
            except ValueError:
                parent[key] = raw
        else:
            try:
                parent[key] = int(raw, 10)
            except ValueError:
                parent[key] = raw
    return config


def load_config(path: str | None = None) -> dict:
    if path is None:
        path = os.path.join(_PROJECT_ROOT, "config.json")
    config = deepcopy(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user_config = json.load(f)
            config = _deep_merge(config, user_config)
        except Exception as e:
            import logging
            logging.getLogger("config").warning(f"加载 {path} 失败: {e}，使用默认配置")
    return _override_from_env(config)
