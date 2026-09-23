# Factory MCP

The shared monolith MCP surface supports operator status, existing-issue
intake, receipt inspection, decision replies, conductor context and controls. A spoken or typed
conversation can use these operations without waiting for a model turn.
The external gateway is configured at `https://mcp.jomcgi.dev/mcp`, with
Authentik OAuth. Calls require a standing human principal in `operators`;
a visible tool, workload identity or delegated credential is not enough.

## Operation ownership

Every mutation reaches an existing owner. MCP and the bearer HTTP surface do
not carry separate implementations.

| Exposed operation | Owner and identity | Enforced controls |
| --- | --- | --- |
| Status, escalations, task detail and context | Existing factory read models keyed by the server-selected receipt or task | Standing human `operators` principal; reads do not need the conductor model. |
| Existing-issue submission | `factory_intake.receive_issue`, reached through the repository-validating receipt route | Exact repository, issue and generation; available repository; live open issue; duplicate identity returns the existing receipt. |
| Admission pause, active-task pause/resume and terminal factory stop | `factory_controls.request_control` for both MCP and bearer HTTP | Authenticated actor, actor-scoped request key, exact expected control version and exact task ID where required, all serialized by the singleton control lock. |
| Decision reply and clarification | `factory_decisions.request_decision` for MCP, bearer HTTP and the private browser | Authenticated actor, actor-scoped request key, exact receipt and content-derived decision identity, checked before effects and again before resolution. |

The private browser derives a stable SHA-256 request key from the exact receipt,
decision, option or chat action, and note. A response loss therefore retries the
same owner request instead of creating a second GitHub effect. The backend still
resolves the actor from its verified `X-Auth-Email` claim and never trusts an
actor in the body.

## Review and submit work

1. Call `factory_status` to read the current control version, policy, work and
   admission blockers. `include_recent=true` also includes recent settled work.
2. Call `factory_submit_issue` with an available repository, an existing open
   issue number and the intended generation. Use the policy's generation for
   current work. Keep all three values unchanged when retrying.
3. Retain the returned `receipt_id`. `created=false` identifies an existing
   receipt, not a second submission. Intake preserves the original issue
   snapshot and returns before any eventual execution finishes.
4. Call `factory_task_detail` with that receipt ID, even before admission has
   assigned a task ID. It returns a node page, dependencies and the last three
   attempts per node. Follow `next_node_offset` for the next page. The default
   is 20 nodes and the maximum is 50; `attempt_count` exposes omitted history.

A receipt is queued intent. It does not override policy eligibility, paused
admissions, capacity or budgets. Node success does not establish accepted
delivery or deployment. Factory records do not index external cloud sessions;
`coverage.cloud_sessions=not_indexed` is unknown coverage, not zero work.

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
work and unresolved starts. Each task row has a `stop` summary. It distinguishes
`work_still_running`, `unknown_or_unreachable`, `cancellation_requested`, and
`cessation_confirmed`. The last state is emitted only when every owned workflow
has an exact durable stop event with positive cessation evidence. Cancellation,
a terminal row, and elapsed lease time cannot create that evidence.

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

## Remaining integration

This is a delivery slice of #5788, not the complete conductor interface.

- Free-form conductor requests, priority/direction edits, policy changes and
  exact-attempt stopping outside the existing receipt decision flow are not
  exposed by these tools.
- Exact-attempt stop remains on its existing HTTP owner and remains default-off
  behind `FACTORY_STOP_SUPERVISION_ENABLED=false`. It is not an MCP operation.
- The factory reconciler remains default-off under `swarm.factoryEnabled=false`.
  This repository slice does not enable it or any new mutation adapter.
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

Conductor rescope: this is repository-only staged delivery. Operational
acceptance remains on #5789, including a legitimate external operator bearer,
deployed replay and stale-version checks while the model is unavailable, an
unreachable-worker global stop trial, and live reconciliation proof that cost,
start records and capacity holds survive cancellation, replacement and retry.
The issue remains open until those checks are complete.
