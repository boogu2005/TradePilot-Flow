# 异常诊断系统：运行与审核

## 运行范围

交易主干保留 Telethon → 异步消费 → 信号 JSON 解析/校验 → 风控 → 下单/退出 → WS/REST 对账。单 Agent 只调查异常，没有交易、SQL、Shell 执行工具。原有策略与风险参数未更改。

生产已接入事件存储、规则监控、只读调查及人工批准后的恢复调度。未启动真实交易机器人进行验收；使用模拟接口完成验证。开发依赖安装：`python -m pip install -r requirements-dev.txt`。常规机器人仍使用原 requirements；启动 `python main.py` 会访问真实配置的 Telegram/交易所。

## 最小可中断演示

在仓库根目录运行，使用一个尚不存在的演示数据库路径：

```powershell
python -m diagnostics.sandbox --db user_data/diagnostic-lab.db init
python -m diagnostics.sandbox --db user_data/diagnostic-lab.db investigate
python -m diagnostics.sandbox --db user_data/diagnostic-lab.db status
python -m diagnostics.sandbox --db user_data/diagnostic-lab.db review --decision approve --reviewer demo-operator
python -m diagnostics.sandbox --db user_data/diagnostic-lab.db execute
python -m diagnostics.sandbox --db user_data/diagnostic-lab.db status
```

每条命令都是独立进程。调查读取证据、根据结果选择下一工具、形成计划；只有 review 后 execute 才会写入模拟交易所。再次 execute 返回同一执行记录，不重复动作。status 展示工具参数、证据来源、时间、审批、执行和验证记录。此数据库不能使用生产交易数据库路径。

审批后运行 `change-position` 再 execute，可以看到旧审批被实时指纹拒绝。新建另一个演示库并在 init 后加 `--scenario partial_fill` 可观察调查路径增加成交查询；`--scenario action_timeout` 可复现动作生效但响应丢失，第二次 execute 仅验证结果。支持的 15 个场景见 benchmark.py。

一步演示：`python -m diagnostics.demo`（内存模拟，包含演示用自动批准）。生产后台没有自动批准逻辑。

## 开关与预算

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| DIAGNOSTIC_AGENT_ENABLED | 0 | 启用只读调查；关闭仍保留规则监控与事件 |
| DIAGNOSTIC_EXECUTION_ENABLED | 1 | 消费已有人工审批；0 禁止诊断恢复动作调度 |
| DIAGNOSTIC_AGENT_MODEL | 空 | 空时复用信号解析器模型及客户端 |
| DIAGNOSTIC_MAX_STEPS | 7 | 模型决策轮数 |
| DIAGNOSTIC_MAX_SECONDS | 60 | 累计调查时间 |
| DIAGNOSTIC_MAX_TOOL_CALLS | 10 | 工具调用上限 |
| DIAGNOSTIC_MAX_TOKENS | 8000 | 请求前预算与返回 usage 记账 |
| DIAGNOSTIC_MAX_NO_PROGRESS | 3 | 时间窗口内等价结果上限 |

更改开关后重启机器人。若需要完全停止诊断动作，将两个 ENABLED 均设为 0。模型配置非法时仅停用调查，不阻止交易主流程启动。

工具有输入结构、数量/长度限制、独立超时及输出截断。数据库和日志查询放在线程中。真实模型适配器按 UTF-8 字节加 framing 保守预留输入预算，并限制 completion；供应商返回 usage 时记录 total_tokens，缺失时保留预留上界。离线模型替身只有决策长度估算，不代表实际计费。

## 生产审核

现有本地 CLI 是审核入口，权限依赖主机账号及数据库文件权限；没有增加 Web 审批或多用户 RBAC。reviewer 是声明姓名，另外记录操作系统账号。

```powershell
python -m diagnostics.cli list
python -m diagnostics.cli show INCIDENT_ID
python -m diagnostics.cli review INCIDENT_ID --decision approve --reviewer alice --plan-digest DIGEST_FROM_SHOW --expires-minutes 15
python -m diagnostics.cli review INCIDENT_ID --decision reject --reviewer alice --plan-digest DIGEST_FROM_SHOW --note "证据不足"
python -m diagnostics.cli review INCIDENT_ID --decision request_information --reviewer alice --plan-digest DIGEST_FROM_SHOW --note "需要补充记录"
python -m diagnostics.cli review INCIDENT_ID --decision modify --reviewer alice --plan-digest DIGEST_FROM_SHOW --parameters-json '{"example":"revised"}'
python -m diagnostics.cli reopen INCIDENT_ID --information "经人工核对的补充事实"
```

modify 只生成新计划，需再次审核；生产恢复动作目前只接受空参数，任意新增参数会被硬校验拒绝。过期计划可通过 reopen 重新取证并生成新版本。reopen 会归档旧检查点，保留审批/执行记录；未知执行未核实前拒绝重开。重复告警合并到原事件，终止后不会自行再次启动模型。

后台只派发白名单：restart_telegram_consumer、restart_websocket_worker、restart_exit_manager、reconnect_websocket、refresh_exchange_snapshot、trigger_reconciliation。后者调用既有对账器，可能撤单/补保护单，所以也必须审批。禁止任意动作和参数；重启 worker 还要求本地和交易所无仓位、无挂单。

执行前后使用直接 REST 快照，包含普通单及条件/止盈止损等算法单。快照可能被分页截断时拒绝执行。核查计划版本、摘要、指纹、期限、规则，风险检查结束后再次核查。恢复后检查仓位一致、保护单引用、WS 连接/订阅、消费与退出监控任务及心跳；未知结果最多核查 120 秒，然后转人工。持久化唯一 operation_id 防止重复发送，已发送但响应丢失时只查询。

## 持久化与重启

继续使用项目 SQLAlchemy/SQLite，仅新增 diagnostic_* 表：incidents、approvals、executions、leases、investigations。未操作本机生产数据库。部署时沿用原迁移入口，先按既有运维流程备份 .env 和完整 user_data。

open/investigating 自动恢复累计步数、工具记录和预算。数据库租约阻止重复调查，写入时校验租约所有者与期限。waiting_human 保留审核；executing/pending_confirmation 恢复只验证，不再次发起动作。

入站队列与持久化任务分开；数据库故障时保留 user_data/diagnostic_spool，恢复后重放。有界内存队列在落盘前遭遇强制终止仍可能丢失，队列满会明确告警，不能宣称零丢失。正常关闭会尝试排空队列。检查点是调查记录，不是实时交易所状态。

## 评估与接入

```powershell
python -m pytest -q
python -m diagnostics.evaluation
python -m diagnostics.fault_evaluation
python -m ruff check diagnostics core/order_submission.py
python -m mypy diagnostics --follow-imports skip --ignore-missing-imports --check-untyped-defs
```

详见 [评估说明](diagnostics/EVALUATION.md) 与 [工程审查](ENGINEERING_REVIEW.md)。真实接入沿用现有 Telegram、OKX、模型配置；首先在交易所测试环境验证只读字段、权限及快照，再审核恢复计划。禁止用真实资金作为功能测试。未验证真实模型工具选择效果，未声明生产收益或准确率提升。

## 框架选择与 RAG

评估了 LangGraph 的状态图、工具节点、checkpoint 和 interrupt 能力。本项目已有 asyncio 生命周期与 SQLAlchemy 持久化，第一版分支仅 tool/plan/escalate，自有显式循环更便于复用现有事务和审批版本规则，因此未引入 LangGraph。人工等待落到独立审批记录，重启由事件状态和租约恢复。也未引入 Redis、PostgreSQL 或向量数据库。

RAG 使用版本化 Markdown 的关键词/错误码检索，返回来源、版本、适用条件。实时订单/持仓只来自查询工具；风险阈值仍由原配置与 Workflow 管理。未经人工核实的 Agent 结论不会写入可信手册。
