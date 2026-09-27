# 部署到 Ubuntu 云服务器

此文档供将 Dashboard 与机器人部署在同一台 Ubuntu 主机时参考。先完成测试环境验证，
再按实际网络和权限设计决定是否提供远程访问。

## 一、架构

```
┌───────────── Ubuntu 云服务器 ─────────────┐
│  交易机器人 (Telegram + OKX)              │
│      └── SQLite: user_data/trading_bot.db │
│                                           │
│  Dashboard (FastAPI + 前端静态页) ─────────┼── 只读连接 SQLite
│      端口 8000，仅绑定 127.0.0.1          │
│      └── nginx 反代 + HTTPS (80/443)     │
└─────────────┬─────────────────────────────┘
              │ 公网 IP / 域名
    ┌─────────┴──────────┐
    │ 电脑浏览器          │ 手机浏览器
```

**安全设计**：Dashboard 以 `mode=ro` 只读打开机器人库，绝不写交易数据；
自己的统计表写入独立的 `user_data/dashboard.db`。

## 二、前置条件（服务器上）

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
# 可选：服务器构建前端需要 nodejs；也可以本地构建后直接上传 dist
sudo apt install -y nodejs npm
```

## 三、上传代码（在本地 Windows 执行）

把本项目的 `backend/`、`frontend/`、`deploy/` 传到服务器机器人根目录
（即服务器上含 `main.py`、`user_data/` 的目录，例如 `/home/ubuntu/bot` 或 `/opt/bot`）：

```powershell
# 在本地项目根目录下
scp -r backend frontend deploy ubuntu@<服务器IP>:/home/ubuntu/bot/
```

> 用 `rsync` 更高效（增量、断点续传）：
> `rsync -avz --exclude node_modules --exclude .venv-dashboard backend frontend deploy ubuntu@<服务器IP>:/home/ubuntu/bot/`

## 四、一键部署（服务器上）

```bash
cd /home/ubuntu/bot            # 机器人根目录
# 先创建私有 .env，填写强密码和随机 DASHBOARD_AUTH_SECRET
bash deploy/ubuntu/deploy.sh   # 自动装依赖、构建前端、注册 systemd 服务
```

脚本会自动：
- 查找 `user_data/trading_bot.db` 并配置为只读数据源
- 创建虚拟环境 `.venv-dashboard` 并安装依赖
- 若无 `frontend/dist` 且服务器有 node，则自动构建前端
- 写入 `/etc/systemd/system/dashboard.service` 并开机自启

若数据库不在默认位置，用环境变量指定：
```bash
DASH_DB_URL='sqlite:////var/lib/bot/trading_bot.db' bash deploy/ubuntu/deploy.sh
```

## 五、手动部署（不想用脚本时）

```bash
cd /home/ubuntu/bot
python3 -m venv .venv-dashboard
source .venv-dashboard/bin/activate
pip install -r backend/requirements.txt

# 前端：本地已构建则直接上传 frontend/dist；否则：
cd frontend && npm ci && npm run build && cd ..

# 前台启动验证
.venv-dashboard/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

## 六、systemd 开机自启（一键脚本已自动完成）

```bash
sudo systemctl status dashboard        # 查看状态
sudo journalctl -u dashboard -f        # 实时日志
sudo systemctl restart dashboard       # 重启
sudo systemctl stop dashboard          # 停止
```

## 七、防火墙与访问

Dashboard 包含账户和仓位信息，不要直接开放 8000 端口。先设置私有 `.env` 中的
`DASHBOARD_USERNAME`、强 `DASHBOARD_PASSWORD` 和至少 32 字符的随机
`DASHBOARD_AUTH_SECRET`。没有这些值时，公开版 Dashboard 会拒绝启动。

使用 nginx 反代并配置 HTTPS：
```bash
sudo apt install -y nginx
sudo cp deploy/ubuntu/nginx-dashboard.conf /etc/nginx/sites-available/dashboard
sudo ln -s /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/
# 编辑配置文件，把 server_name 换成你的域名
sudo nginx -t && sudo systemctl reload nginx
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```
完成 TLS 证书配置后通过 `https://<你的域名>` 访问。

### HTTPS
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

## 八、验证

```bash
curl -u '<用户名>:<密码>' http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/          # 应返回 HTML 页面
```
浏览器打开后检查：账户概览、持仓、历史交易、排行榜三个维度、老师详情、图表。

## 九、常见问题

### 1. 页面能开但数据全是 0 / 显示离线
机器人库当前没有记录（表全空）属于正常。机器人产生交易后 Dashboard 自动显示。
若想先看效果，可本地生成演示数据后上传，或部署后确认 `DATABASE_URL` 指向正确：
```bash
sudo systemctl show dashboard -p Environment
sudo journalctl -u dashboard | grep 数据库
```

### 2. OKX 实时余额不显示
Dashboard 会读机器人根目录 `.env` 的 OKX 凭证做实时查询。若机器人用代理访问 OKX，
确认 `.env` 中 `EXCHANGE_PROXY` 已配置；查询失败会自动降级为读数据库快照，不影响页面。

### 3. 机器人库被占用/锁
Dashboard 用只读 + WAL 兼容模式打开，与正在运行的机器人可同时读。
若遇到锁等待，机器人侧确认 `PRAGMA journal_mode=WAL`（常见于 Freqtrade 风格配置）。

### 4. 端口被占用
改端口：`DASH_PORT=8010 bash deploy/ubuntu/deploy.sh`，或改 systemd 里的 `PORT` 后重启。

### 5. 手机打不开
- 确认防火墙只放行 HTTPS 所需的 443（证书签发可能需要 80）
- 确认云厂商安全组放行了对应端口（阿里云/腾讯云/AWS 控制台）
- 用 `curl -u '<用户名>:<密码>' http://127.0.0.1:8000/api/health` 在服务器上自测

## 十、本地开发（可选）

Windows 本地开发仍用原方式：`backend` 起 FastAPI，`frontend` 用 Vite 开发服务器（5173），
前后端代码一致，仅部署环境不同。
