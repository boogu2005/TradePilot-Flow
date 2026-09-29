# Diagnostic runbook

Version: 2026-09-28

## Request timeout with unknown order result

Applicability: an order submission timed out and the deterministic REST lookup did not establish an outcome.

Query the exact order id first. A timeout or transport error means `unknown`; it does not mean the order is absent. If the exchange confirms a fill, compare fills and the local order before proposing a local synchronization. Any compensating trade requires a new risk check and human approval.

## Partial fill mismatch

Applicability: exchange fills, local order quantity, and position quantity disagree.

Collect bounded exchange fills, the current exchange position with observation time, and the local trade. Preserve every source and timestamp. Escalate if the target order is ambiguous or quantities cannot be reconciled.

## WebSocket interruption or quiet channel

Applicability: WebSocket is disconnected, stale, or has no recent business push.

A healthy connection may legitimately have no position or order messages. Use heartbeat and subscription state to assess the connection. Use REST for current positions. A successful fresh REST query can support continued deterministic monitoring while WebSocket reconnects.

## Protection repair exhausted

Applicability: the existing REST preflight and three repair attempts failed.

Query the current exchange position and protection order state. Do not infer absence from an API error. Creating, cancelling, or changing a protection order requires a version-bound human approval and a fresh risk/state check immediately before execution.
