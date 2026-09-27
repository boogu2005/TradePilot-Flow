#!/bin/bash
# ============================================================
# 修复缺失平仓数据 — 定时任务入口
# 被 crontab 每天 00:00 调用
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$PROJECT_DIR/repair_tools/repair_close_data.py"
LOG_DIR="$PROJECT_DIR/repair_tools/logs"

# 确保日志目录存在
mkdir -p "$LOG_DIR"

# 切换到项目目录（确保相对路径 .env / user_data/ 能正确解析）
cd "$PROJECT_DIR"

# 使用项目虚拟环境的 Python（如果有）
if [ -f "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/venv/bin/python3"
elif [ -f "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
else
    PYTHON="python3"
fi

# 运行修复脚本
exec "$PYTHON" "$SCRIPT" >> "$LOG_DIR/cron_daily.log" 2>&1
