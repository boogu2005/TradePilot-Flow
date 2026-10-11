"""Persistent offline exchange lab. No credentials, network or live exchange imports."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    select,
    update,
)
from sqlalchemy.orm import sessionmaker

from database.models import Base

from .agent import DiagnosticAgent
from .approval import ApprovalService
from .domain import AgentState, IncidentInput, ToolResult, stable_digest, utcnow
from .execution import ControlledExecutor, ExecutionHandler
from .repository import DiagnosticRepository
from .tools import DiagnosticTool, ToolRegistry

metadata = MetaData()
lab = Table("diagnostic_fake_exchange", metadata, Column("id", Integer, primary_key=True), Column("state", JSON))


class Sandbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path.resolve().as_posix()}", connect_args={"timeout": 5})
        Base.metadata.create_all(self.engine, tables=[table for name, table in Base.metadata.tables.items() if name.startswith("diagnostic_")])
        metadata.create_all(self.engine)
        self.repository = DiagnosticRepository(sessionmaker(bind=self.engine, expire_on_commit=False))

    def read(self):
        with self.engine.connect() as connection:
            row = connection.scalar(select(lab.c.state).where(lab.c.id == 1))
            if row is None:
                raise ValueError("run init first")
            return row

    def write(self, state):
        with self.engine.begin() as connection:
            connection.execute(update(lab).where(lab.c.id == 1).values(state=state))

    def initialize(self, scenario="timeout_filled"):
        with self.engine.begin() as connection:
            if connection.scalar(select(lab.c.id)) is not None:
                raise ValueError("existing lab is preserved; choose a new database for a new scenario")
            connection.execute(lab.insert().values(id=1, state={
                "scenario": scenario, "order_id": "sandbox-order-1", "filled": .4 if scenario == "partial_fill" else 1.,
                "local_filled": 0., "revision": 1, "actions": [], "queries": [],
                "ws_connected": scenario != "ws_disconnected", "worker_alive": scenario != "restart_worker",
            }))
        item = self.repository.report(IncidentInput(
            "sandbox:order-1", scenario, "order", "sandbox-order-1", utcnow(),
            ["request_timed_out", "workflow_query_inconclusive"],
        ))
        return item.id

    def incident(self):
        return self.repository.list_incidents(1)[0]

    def fingerprint(self, _plan=None):
        state = self.read()
        return stable_digest({key: state[key] for key in ("order_id", "filled", "revision", "local_filled")})

    def tools(self):
        async def query(name, arguments):
            state = self.read()
            state["queries"].append({"tool": name, "arguments": arguments})
            self.write(state)
            if state["scenario"] in ("no_evidence", "insufficient_evidence"):
                return ToolResult.unknown("fake_exchange", {"order_id": state["order_id"]})
            if name == "query_order":
                data = {"status": "closed" if state["filled"] == 1 else "partially_filled", "filled": state["filled"]}
            elif name == "query_fills":
                data = {"filled": state["filled"], "position": state["filled"]}
            else:
                data = {"filled": state["local_filled"], "fingerprint": self.fingerprint()}
            return ToolResult.ok("fake_exchange" if name != "query_local" else "fake_local", data)

        def make_handler(name):
            async def handle(arguments):
                return await query(name, arguments)
            return handle

        return ToolRegistry([DiagnosticTool(name, {"order_id": str}, make_handler(name))
                             for name in ("query_order", "query_fills", "query_local")])

    def model(self):
        sandbox = self

        class EvidencePolicy:
            async def decide(self, state, _tools):
                scenario = sandbox.read()["scenario"]
                if scenario == "model_unavailable":
                    raise ConnectionError("injected provider unavailable")
                if not state.tool_calls:
                    if scenario == "invalid_tool":
                        return {"kind": "tool", "tool": "shell", "arguments": {"command": "blocked"}}
                    return {"kind": "tool", "tool": "query_order", "arguments": {"order_id": 123 if scenario == "invalid_arguments" else state.incident.object_id}}
                last = state.tool_calls[-1]
                if last.result.status == "error":
                    return {"kind": "escalate", "reason": "invalid tool or query failed"}
                if last.result.status != "ok":
                    if scenario == "no_evidence":
                        return {"kind": "tool", "tool": last.tool, "arguments": last.arguments}
                    return {"kind": "escalate", "reason": "insufficient evidence"}
                if last.tool == "query_order":
                    name = "query_fills" if last.result.data["status"] == "partially_filled" else "query_local"
                    return {"kind": "tool", "tool": name, "arguments": {"order_id": state.incident.object_id}}
                if last.tool == "query_fills":
                    return {"kind": "tool", "tool": "query_local", "arguments": {"order_id": state.incident.object_id}}
                order = next(call.result.data for call in state.tool_calls if call.tool == "query_order")
                return {"kind": "plan", "plan": {
                    "version": 1, "target": {"type": "order", "id": state.incident.object_id, "fingerprint": last.result.data["fingerprint"]},
                    "action": "sync_local_order", "parameters": {"filled": order["filled"]},
                    "evidence_ids": [call.tool_call_id for call in state.tool_calls],
                    "preconditions": ["fresh exchange fill and unchanged local fingerprint"], "risk": "medium",
                    "expected_result": "local filled equals exchange filled", "verification": ["filled equality"],
                }}
        return EvidencePolicy()

    async def investigate(self):
        return await DiagnosticAgent(self.repository, self.tools(), self.model()).run(self.incident().id)

    def plan(self):
        item = self.incident()
        checkpoint = self.repository.load_checkpoint(item.id)
        if not checkpoint:
            raise ValueError("investigate before reviewing a plan")
        state = AgentState.from_checkpoint(item, checkpoint)
        if state.plan is None:
            raise ValueError("no plan; inspect incident evidence")
        return state.plan

    def review(self, decision, reviewer, parameters=None):
        return ApprovalService(self.repository).decide(self.incident().id, self.plan(), decision, reviewer,
                                                      modified_parameters=parameters)

    async def execute(self):
        async def apply(plan):
            state = self.read()
            state["actions"].append({"action": plan.action, "parameters": plan.parameters})
            state["local_filled"] = plan.parameters["filled"]
            self.write(state)
            if state["scenario"] == "action_timeout":
                raise TimeoutError("exchange applied but response lost")
            return {"applied": True}

        def risk(plan):
            state = self.read()
            valid = plan.target.id == state["order_id"] and plan.parameters == {"filled": state["filled"]}
            return valid, "authoritative fake exchange quantity must match exactly"

        def verify(_):
            state = self.read()
            return {"status": "satisfied" if state["filled"] == state["local_filled"] else "unknown",
                    "filled": state["local_filled"], "source": "fake_exchange", "observed_at": utcnow().isoformat()}

        executor = ControlledExecutor(self.repository, ApprovalService(self.repository), [
            ExecutionHandler("sync_local_order", risk, self.fingerprint, apply, verify),
        ])
        return await executor.execute(self.incident().id, self.plan())

    def trace(self):
        item = self.incident()
        return {"incident": asdict(item), "checkpoint": self.repository.load_checkpoint(item.id),
                "approvals": [asdict(row) for row in self.repository.approvals_for(item.id)],
                "executions": self.repository.executions_for(item.id), "fake_exchange": self.read()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("command", choices=("init", "investigate", "review", "execute", "status", "change-position"))
    parser.add_argument("--scenario", default="timeout_filled")
    parser.add_argument("--decision", default="approve", choices=tuple(ApprovalService.VALID_DECISIONS))
    parser.add_argument("--reviewer", default="sandbox-operator")
    parser.add_argument("--parameters-json")
    args = parser.parse_args(argv)
    sandbox = Sandbox(args.db)
    if args.command == "init":
        output = {"incident_id": sandbox.initialize(args.scenario)}
    elif args.command == "investigate":
        output = asyncio.run(sandbox.investigate()).to_checkpoint()
    elif args.command == "review":
        output = asdict(sandbox.review(args.decision, args.reviewer, json.loads(args.parameters_json) if args.parameters_json else None))
    elif args.command == "execute":
        output = asdict(asyncio.run(sandbox.execute()))
    elif args.command == "change-position":
        state = sandbox.read()
        state["revision"] += 1
        sandbox.write(state)
        output = {"position_changed": True}
    else:
        output = sandbox.trace()
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
