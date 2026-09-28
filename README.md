# Telegram Signal Trading Bot

一个用于研究 Telegram 交易信号解析、OKX 合约订单管理与持仓对账的 Python 项目，附带 FastAPI/React 监控界面。项目包含自动下单代码；请先在测试环境阅读配置、运行模拟测试，确认行为后再自行决定是否使用真实账户。

## 功能

- 监听指定 Telegram 群组，解析开仓、平仓及止盈止损更新信号。
- 为已成交仓位挂止损和止盈，并根据交易所状态补挂缺失的保护单。
- 老师提供止盈止损价时使用该价位；未收到止损价时使用本地固定止损。单个老师止盈目标对应全仓。
- Dashboard 展示账户、持仓、历史交易及信号来源统计；可使用演示数据查看界面。

## 代码结构

| 路径 | 内容 |
| --- | --- |
| `main.py`、`telegram_engine/`、`signal_engine/` | 机器人入口、消息监听与信号解析 |
| `exchange_engine/`、`exit/`、`core/` | 下单、退出、保护单与对账 |
| `database/`、`user_data/` | 数据模型与本地运行数据；`user_data/` 不进入仓库 |
| `backend/`、`frontend/` | 监控 API 与网页 |
| `simulation/`、`tests/` | 离线模拟与回归测试 |

## 本地开始

需要 Python 3.10+、Node.js 和 npm。Windows PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r backend/requirements.txt
Copy-Item .env.example .env
Copy-Item config.example.json config.json
```

在私有的 `.env` 中填入自己的测试凭据、Telegram 群组、强 `DASHBOARD_PASSWORD` 和至少 32 字符的随机 `DASHBOARD_AUTH_SECRET`。可以用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成签名密钥。没有 Dashboard 密码和签名密钥时服务会拒绝启动。不要将真实账户的 API 密钥或现有数据库复制进公开仓库。`signal_engine/groups.py` 默认不列出任何私人群组；通过 `TG_TARGET_GROUPS` 配置自己的监听目标。运行交易机器人之前，检查 `config.json` 中的风险参数和 `OKX_MODE=testnet`。

启动 Dashboard 的演示数据模式：

```powershell
.\.venv\Scripts\python.exe backend/scripts/seed_demo.py
$env:DATABASE_URL = 'sqlite:///user_data/demo_trading.db'
$env:DASHBOARD_LIVE_OKX = '0'
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

另开终端启动前端：

```powershell
cd frontend
npm ci
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`。机器人入口为 `python main.py`，它会连接 Telegram 与交易所；请只在完成配置和测试环境检查后运行。Ubuntu Dashboard 部署示例见 [DEPLOY_UBUNTU.md](DEPLOY_UBUNTU.md)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m compileall -q backend core database exchange_engine exit signal_engine telegram_engine
.\.venv\Scripts\python.exe -X utf8 tests/sim_entry_fill.py
.\.venv\Scripts\python.exe -X utf8 tests/sim_tp2_flow.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_dashboard_auth_config.py
.\.venv\Scripts\python.exe tools/check_public_release.py
cd frontend
npm ci
npm run build
```

部分旧版协调器模拟的预期仍需整理，当前不把全部模拟测试通过作为项目声明。模拟测试、测试网与真实交易所环境之间存在差异。

## 安全与隐私

仓库只包含代码和示例配置。`.env`、`config.json`、`user_data/`、SQLite 数据库、Telegram 会话、日志、虚拟环境和构建产物均已排除。公开发布前运行 `git status` 和 `git ls-files` 检查待提交文件；请勿把真实账户数据放进 issue 或 PR。发现安全问题请参阅 [SECURITY.md](SECURITY.md)。

本项目不提供收益保证。自动交易可能产生实际损失，使用者自行决定运行环境与风险限制。

## 许可证

[MIT](LICENSE)。
