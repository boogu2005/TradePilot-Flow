# 安全政策

本项目会连接交易所和 Telegram。请使用最小权限的独立测试凭据，并将 `.env`、会话文件和运行数据库保留在仓库之外。部署 Dashboard 时设置强密码与随机 `DASHBOARD_AUTH_SECRET`，通过 HTTPS 和访问控制保护公开入口。

如果发现漏洞，请通过 GitHub 仓库的 **Security → Advisories → Report a vulnerability** 私下报告。请勿在公开 issue、PR 或截图中披露凭据、仓位和可直接复现攻击的敏感信息。

若凭据意外公开，应立即在相应服务商处撤销或轮换；仅删除 Git 文件不能使已暴露的密钥失效。
