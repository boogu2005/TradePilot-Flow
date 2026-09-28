# 1. Current Architecture

审查对象是 public-release。交易链路为 Telethon → asyncio 消费 → LLM JSON 解析 → 字段/来源校验 → 风控及仓位计算 → 订单/保护单 → ExitManager。WS 心跳、重连、REST 恢复和定时对账由现有模块完成。SQLAlchemy/SQLite 保存业务与独立诊断记录。

本次重点逐行检查诊断、审批、执行、持久化与任务监督，没有完成全仓库每一行的审计或真实服务联调。生产 Incident 入口包括保护单恢复耗尽、本次新增的 TaskManager 自动重启耗尽。HealthStatus 分类器和白名单恢复适配器已实现并测试，尚未全面接入线上监控。生产交易写操作未注册。

# 2. Problems Found

Critical：没有充分依据给出全仓库不存在严重问题的结论。

High，已修复：后续拒绝仍可能使用早先批准；执行记录先查后写没有原子抢占；执行未知结果不能恢复核查；模型无硬超时；重启清零耗时；no_action 可以自报解决；关闭 Agent 丢弃事件；无检查点事件不恢复；诊断数据库异常传播给交易恢复；修改审批没有生成新版本。

Medium，已修复：终止事件被重复告警重新排队；事件和检查点状态不一致；计划版本改变造成新的等价操作标识；布尔冒充整数、负 limit、数组参数；错误字符串匹配误判不存在；缓存持仓被标记为新鲜数据；读取整份日志后才截断；常见凭证未脱敏；无进展检测不包含时间窗口；旧评估把 wrong_action=False 写死。

Low，已修复：类型错误、导入规范、agent_run_id/tool_call_id 缺失。

# 3. Code Changes

主要修改 diagnostics 内 Agent、工具、状态、审批、执行器、仓库、服务、启动、CLI、演示和评估；新增 health.py、remediation.py、redaction.py、fault_evaluation.py。core/task_manager.py 在原有自动重启耗尽后报告持久化事件。新增 15 个可靠性回归及审批修改版本回归。

未修改现有仓位、杠杆、SL/TP、trailing、ROI 或持仓超时参数，未使用真实资金。没有新增数据库或 Agent 框架。

# 4. Workflow vs Agent Boundary

Workflow 负责正常交易、明确风险规则、心跳、重连、退出和常规对账。Agent 依据新证据选择只读工具并生成计划。人工审批绑定对象、参数、版本与期限。ControlledExecutor 只执行注册回调。Agent 没有任意交易 API、shell 或 SQL 权限。

# 5. Safety & Idempotency

correlation_key 合并事件，单 Worker 队列集合去重，终止事件不重新入队。批准后拒绝或修改使旧批准失效。MODIFY 保存新版本并要求重新批准。执行前检查当前计划与实时指纹。

operation_id 基于事件、目标、动作和参数，不因计划版本单独变化而变更。数据库唯一键 INSERT 抢占执行权。超时或崩溃留下 executing/pending_confirmation，恢复仅做验证，不重发动作。认领后尚未执行即崩溃仍可能一直未知，需要人工核实；不宣称跨交易所 exactly-once。

# 6. Agent Loop

状态记录事件、工具参数/结果、来源和时间、候选原因、缺失信息、计划、轮数、耗时和预算。模型输出 tool/plan/escalate；非法调用转换为结构化错误。默认 7 轮，模型与工具等待有上限，重启延续累计耗时。时间窗口内等价工具、参数和结果重复会转人工。只保存决策摘要，不要求完整内部推理。

当前终止状态为 waiting_human、failed、timed_out、budget_exhausted；resolved 由执行后置验证设置。waiting_approval/waiting_external_state 尚未分别建模。

# 7. Auto Remediation

现有 WS 重连和 TaskManager 重启保持 Workflow。新增适配器只接受 reconnect_websocket、refresh_exchange_snapshot、trigger_reconciliation、restart_telegram_consumer、restart_websocket_worker、restart_exit_manager。

适配器必须经受控执行器审核，验证要求 alive、authenticated、subscribed、snapshot_fresh、exit_healthy 和 reconciliation 通过。测试使用 Fake 回调；生产回调尚未注册，不宣称线上自动恢复已完成。当前执行器保守地要求所有动作先审核。

# 8. Tests

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check diagnostics
python -m mypy diagnostics --follow-imports skip --ignore-missing-imports --check-untyped-defs
python -m compileall -q diagnostics core database exchange_engine exit signal_engine telegram_engine backend
python -m diagnostics.fault_evaluation
python -m diagnostics.demo
```

实测全量测试 129 passed；故障专项 n=15、15 passed。诊断模块 lint 通过，类型检查覆盖 19 个文件。类型检查跳过其他模块导入内部检查，不能宣称全仓库严格类型通过。

全仓库 `python -m ruff check . --output-format concise --statistics` 实测未通过：1096 项发现，主要涉及导入、旧类型注解、未使用变量、宽泛异常。两个特别核查项：批量取消闭包当前在每次循环内 await，未观察到跨迭代延迟执行；旧模拟 SimTrade 的重复 open_rate 最终采用后一个默认值。未对这些旧模块做整库自动格式重写。发布扫描覆盖 236 个工作树文件，0 项发现。

# 9. Evaluation

fault_evaluation 运行固定测试文件并读取真实 JUnit XML，报告每条断言结果与耗时。覆盖模型挂起、无证据自报解决、无检查点恢复、数据库故障、非法参数、拒绝旧审批、响应丢失、并发回调、安静及断线 WS、SQLite 重新创建仓库、恢复健康/对账、重启预算、脱敏。

这 15 个回归不等同于需求中全部 15 个端到端业务场景。外部模型 Token、模型工具选择准确率、执行过的 Pure Workflow 基准、业务错误动作率没有测得，明确留空。旧 evaluation 只保留为 10 项脚本契约样例，历史“零错误动作”和“100% 正确率”不得用于生产或面试效果宣传。

# 10. Remaining Limitations

- 未完成全仓库逐行审查、真实 Telegram/交易所/LLM 联调和全部业务故障端到端验收。
- 监控来源统一 Alert→Recovery→Incident 尚未全面接通；生产目前只有两个恢复耗尽入口。
- 同步 SQLAlchemy/日志 IO 仍可能短时占用事件循环；数据库不可用只能记录错误，不能保证事件不丢。
- 没有多进程 Agent 租约或关闭事件的新代次，按单进程单诊断 Worker 使用。
- Token 只是决策输出估算，没有供应商实际 usage 或完整上下文预算。
- CLI reviewer 是本地身份声明，没有 RBAC；完整 Trace、已确认历史案例和补充信息重开流程尚未完成。
- 真实动作的实时指纹、前提、风险和后置条件需要逐个 handler 实现；生产 handler 尚未注册。
- 常见格式脱敏不能证明覆盖所有凭证形式。

# 11. Demo Guide

安装原 requirements 后，从仓库根目录执行 `python -m diagnostics.demo`。反馈驱动的离线模型替身依次根据证据选择查询或转人工，正常路径包含计划、模拟批准、受控执行、验证，输出 incident.status=resolved、execution.status=verified。

`python -m diagnostics.fault_evaluation` 运行真实故障回归。`python -m diagnostics.cli list` 和 `python -m diagnostics.cli show ID` 查看生产事件、检查点和审批。

`DIAGNOSTIC_AGENT_ENABLED=0` 关闭调查但保留事件；启用后恢复 open/investigating。演示是内存模式，进程恢复另由 SQLite 测试验证，尚未提供可中断的交互式持久化演示。

# 12. Interview Explanation

这个项目原本是 Telegram 信号交易机器人。我保留确定性的交易主流程，因为仓位计算、风险限制、下单和退出都有明确规则，需要可重复验证。大模型解析 JSON 是固定节点，不拥有交易权限。

Agent 放在常规恢复耗尽后的诊断环节。例如下单响应超时，不能直接认定下单失败。调查需要根据订单结果决定是否继续查成交、本地记录或持仓。下一次查询依赖上一步证据，这部分适合受限 Agent。第一版用单 Agent，因为一个状态循环就能覆盖调查；多个 Agent 会增加协调和重复操作风险，目前没有证据支持这种成本。

预警由规则产生，因为断线和重启次数是明确事实。Agent 的任务是解释未知状态，不能修改风险阈值。它只有只读工具，输入校验类型、数量和长度，输出保留来源、时间与错误类型。接口超时表示未知，不能变成没有订单或零持仓。

调查保存检查点、工具记录和决策摘要，默认最多七轮，并限制工具次数、累计时间和估算预算。相同工具、参数和等价结果在窗口内重复就转人工。模型挂起也有硬超时，诊断失败不会主动停止其他交易任务。同步存储仍存在阻塞风险，这也是后续需要改善的工程边界。

模型提出计划后还不能执行。人工审批绑定版本、参数、对象指纹和期限，修改会形成新版本。执行器重新检查实时状态与风险，只允许注册动作。数据库用唯一操作标识抢占执行权；响应丢失时后续只核查，不再次发送。这是防重复发起机制，不是数据库事务能覆盖交易所的 exactly-once 保证。认领后执行前崩溃时宁可保留未知，也不能贸然重发。

Tool success 不等于业务完成。例如进程重启成功，还要认证、订阅、刷新快照、确认退出管理健康并对账。订单操作要重新查最终状态。未确认时保持待确认，成交不能通过所谓回滚抹去。

这次 Review 也纠正了上一版评估：把错误动作写死为零没有证据价值。现在报告真实运行的故障断言及 JUnit 数据，未测量的模型质量与 Workflow 效果对比明确留空。项目已证明受限调查、审核执行和恢复核查机制可测试、可演示；生产写适配器及完整监控覆盖仍需继续实施。相比纯 Workflow，Agent 的目标是处理未知异常时动态收集和组织证据，这个收益还需要真实模型与足够样本验证。
