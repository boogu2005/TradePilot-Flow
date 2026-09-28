# Diagnostic Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a runnable, persistent, bounded anomaly investigation and human-approved execution loop without changing existing trading rules.

**Architecture:** Add a focused `diagnostics` package backed by new SQLAlchemy tables. Keep exchange and workflow integration behind adapters, and exercise the full loop with deterministic fault scenarios.

**Tech Stack:** Python 3.10+, asyncio, SQLAlchemy 2, OpenAI-compatible chat API, pytest, FastAPI.

---

### Task 1: Persistent incidents and checkpoints

**Files:** `diagnostics/domain.py`, `diagnostics/repository.py`, `database/models.py`, `tests/test_diagnostic_repository.py`

- [ ] Write tests for correlation-key deduplication, evidence persistence, and checkpoint resume.
- [ ] Run the tests and confirm they fail because the package is absent.
- [ ] Add isolated diagnostic tables and repository methods without changing existing trade tables.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Bounded read-only tool contract

**Files:** `diagnostics/tools.py`, `diagnostics/runbooks.py`, `docs/diagnostics/runbook.md`, `tests/test_diagnostic_tools.py`

- [ ] Write tests for validation, timeout, truncation, result status, and keyword retrieval provenance.
- [ ] Confirm the tests fail for missing tool infrastructure.
- [ ] Implement registry and adapters with explicit schemas and four-way result status.
- [ ] Run the focused tests.

### Task 3: Evidence-driven agent loop

**Files:** `diagnostics/model.py`, `diagnostics/agent.py`, `tests/test_diagnostic_agent.py`

- [ ] Write tests where the next tool changes after returned evidence, invalid calls are rejected, equivalent repeated results terminate, budgets terminate, and model failure does not affect workflow code.
- [ ] Confirm expected failures.
- [ ] Implement the model protocol, OpenAI-compatible adapter, scripted test model, state updates, budgets, and terminal states.
- [ ] Run the focused tests.

### Task 4: Approval and controlled execution

**Files:** `diagnostics/approval.py`, `diagnostics/execution.py`, `tests/test_diagnostic_approval.py`

- [ ] Write tests for approve/modify/reject/request-info, plan-version binding, expiry, changed fingerprints, idempotent operation ids, and postcondition verification.
- [ ] Confirm expected failures.
- [ ] Implement approval records and action-handler whitelist with state and risk rechecks.
- [ ] Run the focused tests.

### Task 5: Workflow integration and API

**Files:** `diagnostics/service.py`, `core/repair_worker.py`, `main.py`, `backend/api/routes.py`, `.env.example`, `tests/test_diagnostic_integration.py`

- [ ] Write tests for disabled mode, nonblocking queueing, retry-exhaustion incident emission, and resumable pending jobs.
- [ ] Confirm expected failures.
- [ ] Add the optional worker, one narrow repair-exhaustion hook, and minimal authenticated review endpoints.
- [ ] Run the focused tests.

### Task 6: Fault injection, evaluation, and demo

**Files:** `diagnostics/demo.py`, `diagnostics/evaluation.py`, `tests/test_diagnostic_scenarios.py`, `docs/DIAGNOSTIC_AGENT.md`, `README.md`

- [ ] Write scenario assertions for all required failure cases and process metrics.
- [ ] Confirm expected failures.
- [ ] Implement offline scenarios, JSON metrics, and CLI demo.
- [ ] Run the complete diagnostic suite, existing focused regressions, public-release scanner, and frontend build.
