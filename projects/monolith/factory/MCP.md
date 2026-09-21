# Factory MCP

The shared monolith MCP surface supports operator status, existing-issue
intake, receipt inspection, decision replies, conductor context and controls. A
spoken or typed conversation can use these operations without waiting for a
model turn.
The external gateway is configured at `https://mcp.jomcgi.dev/mcp`, with
Authentik OAuth. Calls require a standing human principal in `operators`;
a visible tool, workload identity or delegated credential is not enough.

## Review and submit work

1. Call `factory_status` to read the current control version, policy, work and
   admission blockers. `include_recent=true` also includes recent settled work.
   Each state bucket returns at most 20 rows by default and 50 when requested.
   Follow its independent `next_offset` when a bucket is truncated. Queue
   positions are the durable receipt FIFO order, not a claim that a blocked or
   policy-ineligible receipt will be admitted next.
2. Call `factory_submit_issue` with an available repository, an existing open
   issue number and the intended generation. Use the policy's generation for
   current work. Keep all three values unchanged when retrying.
3. Retain the returned `receipt_id`. `created=false` identifies an existing
   receipt, not a second submission. Intake preserves the original issue
   snapshot and returns before any eventual execution finishes.
4. Call `factory_task_detail` with that receipt ID, even before admission has
   assigned a task ID. It returns a node page, dependencies and the last three
   attempts per node. Follow `next_node_offset` for the next page. The default
   is 20 nodes and the maximum is 50. `attempt_count` exposes omitted history.
   Work-item edges and correction events are independently bounded by
   `history_limit`.

A receipt is queued intent. It does not override policy eligibility, paused
admissions, capacity or budgets. Node success does not establish accepted
delivery or deployment. Factory records do not index external cloud sessions;
`coverage.cloud_sessions=not_indexed` is unknown coverage, not zero work.

Task detail keeps lifecycle evidence separate. A durable start establishes only
that work started. Settlement evidence can establish that an artifact was
produced and independently reviewed. Landing audits establish repository
delivery. Deployment remains `unknown` with `not_tracked_by_factory` coverage
unless another owner supplies evidence. Work-item context includes current open
blockers, the GitHub source timestamp and bounded correction history, without
turning those records into mutation authority.

## Apply controls

Call `factory_control` with an explicit action, a new `request_key`, and
`expected_version` from `factory_status`. Only task actions accept `task_id`.

| Action | Effect |
| --- | --- |
| `pause_admissions` | Stop admitting new tasks; existing tasks continue. |
| `enable` | Resume admissions under the existing policy. |
| `pause_task` | Fence new starts for an exact active task; its running workers continue. |
| `resume_task` | Remove that active task's pause; never choose an escalation option. |
| `stop` | Permanently fence the factory and request cancellation of owned work. |

A stop acknowledgement does not prove worker cessation or undo external
effects. `enable` cannot undo `stop`. Inspect factory status for outstanding
work and unresolved starts.

After a lost response, retry with the same key and identical arguments. The
existing factory audit ledger stores the original outcome under the verified
actor. Concurrent duplicates commit one transition. A replay returns that
original acknowledgement, including its time and version; it is not a fresh
status read and cannot reapply an old command over a newer stop or pause.

`control_version_changed` refuses a command based on stale state. Read status,
reassess the intended action and use a new request key. Reusing a key with
changed arguments returns `conflicting_control_request`. Refused requests are
also durable, so a retry cannot become effective after conditions change.
These operations reuse the singleton control lock, mutation owner and audit
records; spending and existing start records are preserved.

## Decisions and conductor context

1. Read `factory_escalations` and retain the exact `receipt_id` and `decision_id`.
   Decision cards use the same bounded `offset` and `limit` contract as status.
2. Use `factory_context` for that receipt to review current state, recorded
   direction, recent operator exchanges and repository-scoped KG notes.
3. Call `factory_decide` with an explicitly chosen `option_key` and a new
   `request_key`. Use `factory_request_brief` with a note to ask a question or
   provide direction instead of selecting an option.
4. Retry identical arguments with the same key after a lost response. The
   durable request ledger returns the original outcome without repeating
   GitHub effects. A conflicting key or stale decision is refused.

`completed` acknowledges the operation, not task delivery. For a clarification,
check `requeued` and `blocked_by`: posting the question does not guarantee that
the lane can run it. `accepted` means completion is unconfirmed, including a
possibly interrupted process. `outcome_unknown` means GitHub effects may have
occurred. These states retain the receipt's decision fence and require
inspection and reconciliation, not a new key or an automatic retry of effects.
Issue creation on GitHub cannot be made transactional with the factory database.

The same decision owner serves HTTP and MCP. It checks the brief identity under
the control lock before external writes and again before recording a resolution.
Concurrent duplicate MCP calls share one accepted request. Request outcomes are
append-only audit records, with completion committed alongside the resolution.
The operator page sends the same brief identity; legacy HTTP callers can omit it.

Successful answers and clarification requests are reported as unverified evidence
through existing KG ingestion and extraction. Reporting is bounded and cannot
undo a committed factory operation. The separate `knowledge` field reports
`queued`, `duplicate`, `pending` or `unavailable`; repeating the same factory
request also retries the deterministic knowledge report. No repeated factory
effects are needed to recover from a KG outage.

`factory_context` loads up to ten recorded operator exchanges and up to ten KG
notes (five by default). It derives repository scope from the receipt, never a
caller-supplied scope, and does not expand graph neighbours from other scopes.
Notes include dispute, verification, validity and observation metadata. They
are untrusted context, never execution authority. Factory records remain
available when KG retrieval fails. This lets a fresh Claude conversation recover
receipt-level context without selecting a worker session.

The context response carries a stable
`factory-receipt:<repository>:<receipt-id>` conversation identity. It is a
receipt-scoped continuity key that a fresh client may select again, not a claim
that a standalone cross-surface conductor conversation owner exists.

## Remaining integration

This is a delivery slice of #5788, not the complete conductor interface.

- Free-form conductor requests, queue-priority edits, policy changes and
  exact-attempt stopping outside the existing receipt decision flow are not
  exposed by these tools. `capabilities` reports these missing owners
  explicitly. Direction is supported only through an exact pending decision.
- Standalone conductor conversations and private external-chat transcripts
  remain outside the receipt-level contract. Broader conversation continuity
  remains #5787; planner context handoff remains #5849.
- Existing lower-level agent session tools are not conductor conversations.

The targeted tests cover the operation owners and an in-process FastMCP
client round trip. Linux CI remains the validation gate. After deployment,
verify OAuth discovery, tool listing and read calls through an authenticated
external client. Then use an explicitly approved issue/control action to
check its durable acknowledgement and retry. An in-process test does not
establish gateway publication, account permissions or voice-client support.
