# 可复现评估（2026-09-29）

数据文件：[业务场景](evaluation-results.json)、[安全故障回归](fault-results.json)。

## 本次实测

| 指标 | 结果 |
|---|---:|
| 模拟业务场景 | 15 |
| 场景断言通过 | 15 |
| 最终 resolved | 9 |
| 等待人工（含旧审批被拒绝） | 5 |
| 模型不可用，调查 failed | 1 |
| 工具调用 | 21 |
| 循环步数 | 32 |
| 实际动作记录中的错误动作 | 0 |
| 实际动作记录中的重复动作 | 0 |
| 场景执行耗时合计 | 0.993731 秒 |
| 外部模型 Token / 费用 | 未调用，未测量 |
| 独立安全故障回归 | 15/15 |

这是固定小样本中的观测结果，不是总体错误率或生产正确率估计。延迟是本机模拟接口时间，不能推断真实 API 延迟。每个场景在独立 SQLite 库执行，检查最终业务状态、工具选择/参数校验、动作账本和审批结果。

## 场景

timeout_filled、partial_fill、ws_disconnected、ws_quiet、no_evidence、position_changed、duplicate_event、duplicate_approval、restart、invalid_tool、invalid_arguments、insufficient_evidence、model_unavailable、restart_worker、action_timeout。

部分成交时模型替身根据上一个结果增加 query_fills；查询失败或证据不足转人工；安静但正常的 WS 不触发 Agent；重复审批和动作响应丢失均检查实际动作次数。

## Workflow 对比的精确含义

每个案例都实际执行一个保守参考 Workflow：常规 WS 问题直接恢复，其余通过一次订单查询后转人工。Agent 路径采用根据证据选择分支的确定性模型替身，使用相同初始场景和受控执行器。

基准不是旧交易机器人整条链路的历史回放。它用于展示机制与调用成本，不能用其 resolved 数量宣传比原机器人提升多少。原 Workflow 新增的 submit_and_confirm 已另用 3 个回归证明“下单响应丢失但订单已成交”可直接常规查询恢复，无需 Agent；只有查询仍不确定才产生异常事件。

## 复现

运行 python -m diagnostics.evaluation 生成新报告；失败场景使进程非零退出。fault_evaluation 真实运行 pytest 并读取 JUnit。单独 tests/test_diagnostic_runtime.py 启动多个 Python 进程验证调查、审核和执行跨进程恢复，以及审批期间状态改变、租约失效、数据库故障 spool 等。

现有结果没有外部模型质量数据，也没有真实交易环境验收。后续可将同一组模拟只读工具接到真实模型适配器，多次重复试验；报告供应商 usage、路径正确性和最终业务状态，不能只检查最终自然语言总结。
