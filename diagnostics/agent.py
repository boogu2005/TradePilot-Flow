from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass

from .domain import AgentState, DiagnosticPlan, InvestigationBusy, PlanTarget
from .storage import repository_call


@dataclass
class AgentBudgets:
    max_steps: int = 7
    max_seconds: float = 60.0
    max_tool_calls: int = 10
    max_tokens: int = 8000
    max_no_progress: int = 3
    no_progress_window_seconds: float = 60.0


class DiagnosticAgent:
    def __init__(self, repository, tools, model, budgets: AgentBudgets | None = None):
        self.repository = repository
        self.tools = tools
        self.model = model
        self.budgets = budgets or AgentBudgets()

    async def run(self, incident_id: str) -> AgentState:
        owner = uuid.uuid4().hex
        if not await repository_call(self.repository, "claim_lease", incident_id, owner, self.budgets.max_seconds + 60):
            raise InvestigationBusy("investigation already leased")
        try:
            return await self._run(incident_id, owner)
        finally:
            await repository_call(self.repository, "release_lease", incident_id, owner)

    async def _run(self, incident_id: str, owner: str) -> AgentState:
        incident = await repository_call(self.repository, "get_incident", incident_id)
        checkpoint = await repository_call(self.repository, "load_checkpoint", incident_id)
        state = AgentState.from_checkpoint(incident, checkpoint) if checkpoint else AgentState(incident)
        state.lease_owner = owner
        if state.status not in ("open", "investigating"):
            return state
        started = time.monotonic()
        previous_elapsed = state.elapsed_seconds
        last_signature = None
        equivalent_count = 0
        for record in state.tool_calls:
            signature = (record.tool, json.dumps(record.arguments, sort_keys=True, default=str), record.result.normalized_digest())
            equivalent_count = equivalent_count + 1 if signature == last_signature else 1
            last_signature = signature

        while state.step < self.budgets.max_steps:
            state.elapsed_seconds = previous_elapsed + time.monotonic() - started
            if state.elapsed_seconds >= self.budgets.max_seconds:
                return await self._finish(state, "timed_out", "time_limit")
            if len(state.tool_calls) >= self.budgets.max_tool_calls:
                return await self._finish(state, "budget_exhausted", "tool_call_limit")
            if state.token_usage >= self.budgets.max_tokens:
                return await self._finish(state, "budget_exhausted", "token_limit")

            state.step += 1
            await repository_call(self.repository, "save_checkpoint", incident_id, state.to_checkpoint(), owner)
            try:
                if hasattr(self.model, "remaining_tokens"):
                    self.model.remaining_tokens = self.budgets.max_tokens - state.token_usage
                decision = await asyncio.wait_for(
                    self.model.decide(state, self.tools.descriptions()),
                    timeout=max(0.001, self.budgets.max_seconds - state.elapsed_seconds),
                )
            except asyncio.TimeoutError:
                state.elapsed_seconds = previous_elapsed + time.monotonic() - started
                return await self._finish(state, "timed_out", "time_limit")
            except Exception as exc:  # noqa: BLE001 - isolate provider failure from trading
                return await self._finish(state, "failed", f"model_error:{type(exc).__name__}")
            state.elapsed_seconds = previous_elapsed + time.monotonic() - started
            reported_usage = getattr(self.model, "last_token_usage", None)
            state.token_usage += reported_usage if type(reported_usage) is int and reported_usage >= 0 else max(1, len(json.dumps(decision, ensure_ascii=False, default=str)) // 4)
            if state.token_usage >= self.budgets.max_tokens:
                return await self._finish(state, "budget_exhausted", "token_limit")
            if not isinstance(decision, dict):
                return await self._finish(state, "waiting_human", "invalid_decision")
            kind = decision.get("kind")

            if kind == "tool":
                try:
                    record = await asyncio.wait_for(
                        self.tools.call(str(decision.get("tool", "")), decision.get("arguments", {})),
                        timeout=max(0.001, self.budgets.max_seconds - state.elapsed_seconds),
                    )
                except asyncio.TimeoutError:
                    state.elapsed_seconds = previous_elapsed + time.monotonic() - started
                    return await self._finish(state, "timed_out", "time_limit")
                state.elapsed_seconds = previous_elapsed + time.monotonic() - started
                state.tool_calls.append(record)
                signature = (record.tool, json.dumps(record.arguments, sort_keys=True, default=str), record.result.normalized_digest())
                recent = [call for call in state.tool_calls if
                          (record.started_at - call.started_at).total_seconds() <= self.budgets.no_progress_window_seconds]
                equivalent_count = sum(
                    (call.tool, json.dumps(call.arguments, sort_keys=True, default=str), call.result.normalized_digest()) == signature
                    for call in recent
                )
                last_signature = signature
                await repository_call(self.repository, "save_checkpoint", incident_id, state.to_checkpoint(), owner)
                if equivalent_count >= self.budgets.max_no_progress:
                    state.pending_information.append("Repeated equivalent query results produced no new evidence")
                    return await self._finish(state, "waiting_human", "no_new_evidence")
                continue

            if kind == "plan":
                causes = decision.get("candidate_causes") or []
                if not isinstance(causes, list) or len(causes) > 20 or any(not isinstance(cause, dict) for cause in causes):
                    return await self._finish(state, "waiting_human", "invalid_causes")
                state.candidate_causes = causes
                raw = decision.get("plan") or {}
                if not isinstance(raw, dict) or not isinstance(raw.get("target", {}), dict) or not isinstance(raw.get("parameters", {}), dict):
                    return await self._finish(state, "waiting_human", "invalid_plan")
                if type(raw.get("version", 1)) is not int or raw.get("version", 1) < 1:
                    return await self._finish(state, "waiting_human", "invalid_plan")
                for field in ("evidence_ids", "preconditions", "verification"):
                    values = raw.get(field, [])
                    if not isinstance(values, list) or len(values) > 100 or any(not isinstance(value, str) for value in values):
                        return await self._finish(state, "waiting_human", "invalid_plan")
                known_evidence = {e.id for e in state.incident.evidence} | {call.tool_call_id for call in state.tool_calls}
                if any(ref not in known_evidence for ref in raw.get("evidence_ids", [])):
                    return await self._finish(state, "waiting_human", "unknown_evidence_reference")
                target = raw.get("target") or {}
                state.plan = DiagnosticPlan(
                    version=max(state.plan_version_floor, int(raw.get("version", 1))),
                    target=PlanTarget(str(target.get("type", state.incident.object_type)), str(target.get("id", state.incident.object_id)), str(target.get("fingerprint", ""))),
                    action=str(raw.get("action", "manual_review")),
                    parameters=dict(raw.get("parameters") or {}),
                    evidence_ids=list(raw.get("evidence_ids") or []),
                    preconditions=list(raw.get("preconditions") or []),
                    risk=str(raw.get("risk", "high")),
                    expected_result=str(raw.get("expected_result", "")),
                    verification=list(raw.get("verification") or []),
                )
                # Only a deterministic verifier may resolve an incident.
                status = "waiting_human"
                return await self._finish(state, status, "plan_ready")

            if kind == "escalate":
                state.pending_information.append(str(decision.get("reason", "insufficient evidence")))
                return await self._finish(state, "waiting_human", "model_escalation")

            state.pending_information.append("Model returned an invalid decision")

        return await self._finish(state, "budget_exhausted", "step_limit")

    async def _finish(self, state: AgentState, status: str, reason: str) -> AgentState:
        state.status = status
        state.termination_reason = reason
        await repository_call(self.repository, "save_checkpoint", state.incident.id, state.to_checkpoint(), state.lease_owner)
        return state
