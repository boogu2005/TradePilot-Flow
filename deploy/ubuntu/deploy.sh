#!/usr/bin/env bash
# ============================================================================
# Trading Bot Dashboard - Ubuntu 云服务器一键部署脚本
#
# 前提：
#   1. 已把 backend/ 和 frontend/ 上传到机器人项目根目录
#      （即与服务器上的 main.py / user_data/ 同级的目录）
#   2. 服务器已安装 python3-venv；如用服务器构建前端则还需 nodejs/npm
#
# 用法：
#   cd <机器人根目录>
#   bash deploy/ubuntu/deploy.sh
#
# 可选环境变量：
#   DASH_PORT     Dashboard 监听端口（默认 8000，仅本机访问）
#   DASH_DB_URL   机器人数据库 URL（默认自动查找 user_data/trading_bot.db）
#   VENV_DIR      虚拟环境目录名（默认 .venv-dashboard）
#   DASH_USER     systemd 运行用户（默认当前用户）
# ============================================================================
set -euo pipefail

PORT="${DASH_PORT:-8000}"
VENV_DIR="${VENV_DIR:-.venv-dashboard}"
SERVICE_USER="${DASH_USER:-$(id -un)}"

# 定位项目根目录：脚本位于 <根>/deploy/ubuntu/ 下
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "!! 缺少私有 .env。请复制 .env.example 并设置 Dashboard 用户名、密码和随机签名密钥。"
  exit 1
fi

echo "==> 项目根目录: $ROOT"
echo "==> Dashboard 端口: $PORT"
echo "==> 虚拟环境: $VENV_DIR"
echo "==> 运行用户: $SERVICE_USER"

# ---------------------------------------------------------------------------
# 1. 检测机器人数据库
# ---------------------------------------------------------------------------
DB_PATH=""
if [ -n "${DASH_DB_URL:-}" ]; then
  echo "==> 使用指定数据库 URL: $DASH_DB_URL"
elif [ -f "user_data/trading_bot.db" ]; then
  DB_PATH="user_data/trading_bot.db"
  echo "==> 发现机器人数据库: $ROOT/$DB_PATH"
else
  echo "!! 未在 $ROOT/user_data/ 找到 trading_bot.db"
  echo "   可通过 DASH_DB_URL 指定真实路径，例如："
  echo "   DASH_DB_URL='sqlite:////var/lib/bot/trading_bot.db' bash deploy/ubuntu/deploy.sh"
fi

# ---------------------------------------------------------------------------
# 2. 安装 Python 依赖
# ---------------------------------------------------------------------------
if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "!! 缺少 python3-venv，请先执行: sudo apt install -y python3-venv"
  exit 1
fi
if [ ! -d "$VENV_DIR" ]; then
  echo "==> 创建虚拟环境 $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r backend/requirements.txt -q
echo "==> 后端依赖安装完成"

# ---------------------------------------------------------------------------
# 3. 前端构建
# ---------------------------------------------------------------------------
if [ -f "frontend/dist/index.html" ]; then
  echo "==> 使用已构建的前端产物 frontend/dist"
elif command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  echo "==> 构建前端 (node $(node -v)) ..."
  (cd frontend && npm ci && npm run build)
else
  echo "!! 缺少 frontend/dist 且服务器无 node/npm。"
  echo "   请在本地 Windows 执行: cd frontend && npm ci && npm run build"
  echo "   然后上传 frontend/dist 目录到服务器 frontend/ 下，再重跑本脚本。"
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. 安装 systemd 服务（开机自启 + 崩溃自动重启）
# ---------------------------------------------------------------------------
SERVICE_FILE="/etc/systemd/system/dashboard.service"

echo "==> 写入 systemd 服务: $SERVICE_FILE"

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Trading Bot Dashboard (FastAPI)
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
User=$SERVICE_USER
Environment=PYTHONUNBUFFERED=1
Environment=PORT=$PORT
EOF
if [ -n "$DB_PATH" ]; then
  sudo tee -a "$SERVICE_FILE" >/dev/null <<EOF
Environment=DATABASE_URL=sqlite:///$ROOT/$DB_PATH
EOF
fi
if [ -n "${DASH_DB_URL:-}" ]; then
  sudo tee -a "$SERVICE_FILE" >/dev/null <<EOF
Environment=DATABASE_URL=$DASH_DB_URL
EOF
fi
sudo tee -a "$SERVICE_FILE" >/dev/null <<EOF
ExecStart=$ROOT/$VENV_DIR/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable dashboard
sudo systemctl restart dashboard

echo ""
echo "======================================================================"
echo "  部署完成！"
echo "  本机访问:   http://127.0.0.1:$PORT"
echo "  远程访问:   请先配置有 HTTPS 的反向代理和访问控制"
echo "  日志查看:   sudo journalctl -u dashboard -f"
echo "  服务状态:   sudo systemctl status dashboard"
echo "======================================================================"
