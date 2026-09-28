# Event-driven diagnostic agent design

## Existing system

The trading path remains a deterministic workflow: Telethon handlers enqueue messages; `signal_consumer` calls the existing LLM parser, normalizes fields, writes signal records, applies risk checks, and invokes the existing execution functions. OKX WebSocket data is normalized into `runtime_events.event_bus`; trackers keep snapshots while the reconciler treats REST as the source of truth. `order_monitor`, `position_sync`, `repair_worker`, exit rules, and the reconciler already implement known recovery paths. SQLAlchemy and SQLite persist trades, orders, signals, snapshots, and audit-like logs. The dashboard is a separate FastAPI process.

The existing system does not persist unresolved recovery incidents, resume investigations, bind approvals to an immutable plan, or run an evidence-driven tool loop. The repair queue is in memory. LangGraph, Redis, PostgreSQL, Prometheus, and vector search are not current dependencies.

## Decision

Version one uses a small explicit agent state machine instead of LangGraph. The project already owns its asyncio lifecycle and SQLAlchemy persistence, and the first loop needs only bounded tool selection, checkpointing, approval, and resume. Adding LangGraph now would duplicate lifecycle and persistence concepts without improving the minimum closure. `DiagnosticModel` and repository protocols isolate orchestration so a later LangGraph adapter can reuse tools, state, and approvals.

## Boundaries

- Existing workflow owns ingestion, validation, risk, execution, retries, reconciliation, and exits.
- `IncidentService` receives only failures that deterministic recovery could not resolve. It deduplicates by correlation key and persists evidence and attempted recovery steps.
- `DiagnosticAgent` can call registered read-only tools only. Tool calls are validated, timed out, capped, and recorded. Tool output is untrusted evidence and is truncated before model use.
- `ApprovalService` records approve, modify, reject, and request-information decisions against a plan version, object fingerprint, parameters, reviewer, timestamp, and expiry.
- `ControlledExecutor` accepts only registered actions. It rechecks approval, current object fingerprint, and action-specific risk policy before a stable operation id can be executed. It records intent before the external call, checks current state for idempotency, and validates business postconditions after the call.
- The initial production integration emits an incident when protection repair exhausts its existing deterministic retry limit. It does not change risk or execution rules.

## State and termination

Persisted state includes incident identifiers, evidence, tool calls, candidate causes, missing information, plan, approval, verification, loop step, elapsed time, tool-call count, and estimated token use. Terminal states are `resolved`, `waiting_human`, `failed`, `timed_out`, and `budget_exhausted`. Equivalent repeated tool results are detected by tool name, canonical arguments, normalized result digest, and a configurable time window.

## Tools and RAG

Tools expose structured results with source, observation time, status (`ok`, `not_found`, `unknown`, or `error`), retryability, error type, and bounded data. Adapters cover exchange order, fills, positions and snapshot freshness, local trade/order data, related logs, and a keyword runbook retriever. Real-time state never comes from RAG.

## Human review and execution

Plans contain target, action, parameters, evidence ids, preconditions, risk, expected result, and verification conditions. Read-only conclusions may resolve without approval. Any new exposure, cancel/replace, or protection change requires approval. Hard risk failures are rejected. Approval expires or becomes stale when the target fingerprint changes. Duplicate decisions and callbacks return the existing record.

## Verification and demo

Tests use SQLite, scripted model decisions, fake tools, fake clock, and fake action handlers. Fault scenarios cover uncertain timeout, partial fill drift, WS failure with REST available, quiet WS, no new evidence, state change during approval, duplicate event/approval/recovery, invalid tool choices, insufficient evidence, and unavailable model. The demo runs incident to investigation to approval to controlled execution to verification without a live exchange.
