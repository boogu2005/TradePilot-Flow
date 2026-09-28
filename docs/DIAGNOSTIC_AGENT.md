# 事件驱动交易执行与异常诊断系统

## 现有链路与改造结果

### 已实现并保留

- Telethon 将目标群消息放入有界 `asyncio.Queue`，`signal_consumer` 异步消费并持久化全部消息。
- DeepSeek 解析自然语言为 JSON；`SignalParser.standardize()` 按明确的 `message_type` 做字段标准化，主流程继续负责来源关联、字段校验和风险检查。
- 交易执行、订单跟踪、保护单、退出规则、仓位同步和低频对账仍是确定性 Workflow。风险参数和交易策略没有被诊断模块修改。
- OKX WebSocket 由连接状态机、心跳、订阅管理和 EventBus 处理；Tracker 是实时快照，REST 是对账与修复的权威来源。
- SQLAlchemy/SQLite 保存交易业务记录；TaskManager 管理后台任务，Loguru 记录日志，Dashboard 保持独立进程。

### 本次新增

- `DiagnosticIncidentRecord`：持久化异常、证据、既定恢复步骤、重复次数和 Agent 检查点。
- `DiagnosticAgent`：模型根据每次工具结果选择下一工具、形成计划或转人工；不是固定顺序总结器。
- 六个只读工具适配器：订单、成交、持仓、当地交易记录、日志和处置手册。
- `ApprovalService`：批准、修改、拒绝、补充信息；绑定计划版本、摘要、对象指纹和有效期。
- `ControlledExecutor`：白名单动作、稳定 operation id、审批与实时指纹重查、硬风险检查、执行后后置条件验证。
- 保护单常规修复用尽 3 次后，生成去重异常并异步交给 Agent。Agent 关闭或故障不影响原交易任务。
- 离线演示和 10 个故障注入评估场景。

### 部分实现或尚未实现

- 真实交易所写操作没有注册到 `ControlledExecutor`。当前代码会拒绝未知动作；演示使用内存动作处理器。接入撤单、改单或保护单修改时，需要逐个实现风险检查、实时指纹和后置条件，不能开放任意交易方法。
- RAG 是带来源和版本的关键词检索。文档规模不足以证明需要向量数据库。
- 没有接入 LangGraph。显式状态机已经提供状态、工具循环、检查点和审批边界；模型与仓库协议允许以后迁移。
- Token 数是按结构化决策字符数估算的过程预算，不是供应商账单 Token。真实适配器可在响应 usage 可用时替换估算。
- 评估是离线模拟和故障注入，不代表真实交易所、网络或资金环境验收。

## 边界

```text
Telegram/WS/定时器
        |
        v
确定性 Workflow ----常规恢复成功----> 继续交易生命周期
        |
        +----常规恢复耗尽----> IncidentService（持久化、去重、非阻塞队列）
                                   |
                                   v
                          DiagnosticAgent（只读工具）
                                   |
                      +------------+-------------+
                      |                          |
                   无需动作                    处置计划
                   resolved                     |
                                               v
                                      人工审核（版本+指纹+有效期）
                                               |
                                               v
                                      ControlledExecutor
                                               |
                                      实时重查 -> 白名单动作 -> 业务验证
```

模型不能访问任意 SQL、Shell 或任意交易接口。日志、外部消息、手册和工具结果都作为待分析数据，不是权限指令。查询返回严格区分：

- `ok`：查询成功且有可解释结果；空列表可以是已确认的当前状态。
- `not_found`：权威接口明确返回对象不存在。
- `unknown`：查询成功但证据不足或状态仍未收敛。
- `error`：请求失败；带错误类型和 `retryable`，绝不解释成对象不存在。

## 配置与关闭

默认关闭，不改变现有启动行为：

```dotenv
DIAGNOSTIC_AGENT_ENABLED=0
DIAGNOSTIC_MAX_STEPS=7
DIAGNOSTIC_MAX_SECONDS=60
DIAGNOSTIC_MAX_TOOL_CALLS=10
DIAGNOSTIC_MAX_TOKENS=8000
DIAGNOSTIC_MAX_NO_PROGRESS=3
```

启用时将 `DIAGNOSTIC_AGENT_ENABLED=1`。默认复用 `DEEPSEEK_MODEL` 和现有 OpenAI 兼容客户端，也可配置 `DIAGNOSTIC_AGENT_MODEL`。关闭时改回 `0` 并重启；异常和审计记录仍保留。

## 运行、恢复和审核

安装原有依赖后，数据库迁移会只新增三张表，不修改已有交易表：

- `diagnostic_incidents`
- `diagnostic_approvals`
- `diagnostic_executions`

进程重启后，Worker 将有检查点且状态为 `open` 或 `investigating` 的事件重新入队，从保存的步数、证据、调用记录和预算继续。

查看事件与计划：

```powershell
python -m diagnostics.cli list
python -m diagnostics.cli show INCIDENT_ID
```

人工审核：

```powershell
python -m diagnostics.cli review INCIDENT_ID --decision approve --reviewer alice --expires-minutes 15
python -m diagnostics.cli review INCIDENT_ID --decision modify --reviewer alice --parameters-json '{"state":"partially_filled"}'
python -m diagnostics.cli review INCIDENT_ID --decision reject --reviewer alice --note '目标不唯一'
python -m diagnostics.cli review INCIDENT_ID --decision request_information --reviewer alice --note '需要新鲜持仓快照'
```

批准不等于自动执行。受控执行器必须注册对应动作，并在调用前重新验证计划版本、过期时间、对象指纹和风险条件。对象或参数变化时旧审批失效。

## 演示与评估

完整离线闭环：

```powershell
python -m diagnostics.demo
```

输出包含：异常持久化、两次证据查询、结构化计划、审核者与计划版本、稳定操作 ID、执行记录和业务后置条件。

故障注入评估：

```powershell
python -m diagnostics.evaluation
```

覆盖：请求超时但已成交、部分成交本地滞后、WS 断线但 REST 可用、WS 正常但无推送、查询无新证据、审批期间持仓变化、重复事件/审批/恢复、错误工具、证据不足、模型不可用。输出数据由当次实际运行生成，包括结果、升级状态、工具调用数、耗时和估算 Token；不要把一次本地结果描述成生产提升幅度。

## 为什么这里使用 Agent

已知状态迁移、数值风险和交易动作都有明确规则，Workflow 更稳定、可测试，也更容易证明不会越权。异常调查的困难在于下一步查询依赖前一条证据：订单已成交时要查成交与本地记录；接口超时时要保留未知状态；持仓变化时旧方案必须作废。这种动态取证适合受限 Agent。

面试时可按四点解释：

1. Agent 只减少人工收集证据的成本，不拥有交易权限。
2. 计划不是执行凭证；审批绑定版本、对象、参数和期限。
3. 稳定 operation id 与状态核查处理重试和恢复，不能假设数据库事务覆盖交易所。
4. 工具调用成功不代表业务成功；最终以订单、成交、持仓或保护单的后置条件验证为准，无法确认时保持待确认或升级人工。
