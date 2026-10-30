# Factory MCP

The shared monolith MCP surface supports operator status, existing-issue
intake, receipt inspection, decision replies, conductor context and controls. A
spoken or typed conversation can use these operations without waiting for a
model turn.
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
   Each state bucket returns at most 20 rows by default and 50 when requested.
   Follow its independent `next_offset` when a bucket is truncated. Queue
   positions are the durable receipt FIFO order, not a claim that a blocked or
   policy-ineligible receipt will be admitted next.
2. Call `factory_submit_issue` with an available repository, an existing open
   issue number and the intended generation. Use the policy's generation for
   current work. Keep all three values unchanged when retrying. An issue
   outside the policy allowlist is refused with state `ineligible` and
   creates no receipt, because such a receipt could never be admitted.
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
`expected_version` from `factory_status`. Only task actions accept `task_id`;
only `cancel_receipt` accepts `receipt_id`.

| Action | Effect |
| --- | --- |
| `pause_admissions` | Stop admitting new tasks; existing tasks continue. |
| `enable` | Resume admissions under the existing policy. |
| `pause_task` | Fence new starts for an exact active task; its running workers continue. |
| `resume_task` | Remove that active task's pause; never choose an escalation option. |
| `stop` | Permanently fence the factory and request cancellation of owned work. |
| `cancel_receipt` | Settle one queued receipt as cancelled; only queued receipts move. |

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
- Exact-attempt stop remains on its existing HTTP owner. It is not an MCP
  operation. Chart defaults keep it off with
  `FACTORY_STOP_SUPERVISION_ENABLED=false`; the hub overlay
  (`deploy/values-gke.yaml`) turns it on.
- The factory reconciler defaults off under `swarm.factoryEnabled=false` in
  chart values, and the hub overlay turns it on. This repository slice changed
  neither flag and enables no new mutation adapter.
- Standalone conductor conversations and private external-chat transcripts
  remain outside the receipt-level contract. Broader conversation continuity
  (#5787) and planner context handoff (#5849) closed not planned; continuity
  enablement and live validation are #6561.
- Existing lower-level agent session tools are not conductor conversations.

The targeted tests cover the operation owners and an in-process FastMCP
client round trip. Linux CI remains the validation gate. After deployment,
verify OAuth discovery, tool listing and read calls through an authenticated
external client. Then use an explicitly approved issue/control action to
check its durable acknowledgement and retry. An in-process test does not
establish gateway publication, account permissions or voice-client support.

Conductor rescope: this was repository-only staged delivery. Its live checklist
named a legitimate external operator bearer, deployed replay and stale-version
checks while the model is unavailable, an unreachable-worker global stop trial,
and live reconciliation proof that cost, start records and capacity holds
survive cancellation, replacement and retry. #5789 closed on 2026-09-30. Its
closing comment reports read-only evidence: production flags on, gateway 401s
for anonymous and invalid bearers, and attributed stop and decision audit rows.
It records none of the checklist drills as run. Open #5788 owns the matching
post-deploy checks above: operators-group OAuth discovery and tool listing
through Context Forge with a legitimate bearer, submit retry returning one
durable receipt with exact decision-reply acknowledgements, and status, pause
and stop while the model is busy or unavailable. No open issue owns the
stale-version check, the unreachable-worker stop trial or the cost, start
record and capacity hold reconciliation. The closing comment calls the stop
trial and cost preservation unverified and unplanned, and asks for a specific
bug if an incident shows a gap.

## Prepare bounded validation

`factory_find_receipts` resolves exact task IDs, issue numbers or PR numbers in
an available repository. Selectors intersect. Follow `next_offset` and inspect
`task_class`, generation and task identity when `ambiguous=true`. An issue can
have several advisory and delivery receipts. PR lookup verifies GitHub's exact
repository and branch against recorded delivery identity. It does not infer a
receipt number from an advisory record or create work.

`factory_prepare_validation` exposes a narrow form of existing operator
configuration, not arbitrary policy editing. Preview a selected current primary
repository receipt while admissions are paused, with no competing queued,
active or landing receipts and no unresolved start reservations. Apply with the
returned digest, reason and an actor-scoped request key. The control lock and
fingerprint reject concurrent policy, version or receipt changes. Identical
replays return the recorded result; inspect current status after a lost response.

Preparation disables automatic landing (including repository overrides), intake,
advisory refinement, issue closing and problem-issue production, restricts the
issue allowlist to the selected receipt, and pauses other repositories. It keeps
admissions paused and preserves generation, accounting bounds, turn limits,
deadlines, receipt ownership and holds. Dollar amounts are policy/accounting
bounds, not evidence of marginal cash charges.

This operation does not recover, resume, admit or settle any task, and its
preview is not evidence that recovery will succeed. Before any separately
authorized execution, inspect live ownership, existing PRs and other owners,
then use the existing recovery and execution checks. Never restart externally
delivered work merely because its receipt remains escalated. Both tools retain
the existing standing-human `operators` requirement.

For a completed advisory refinement, the receipt selector can prepare its issue
for a **new delivery receipt**. It requires one authoritative `factory:refine`
`finish_task` audit with outcome `succeeded` and evidence `refine_agent_ready`,
a settled task, quiescent execution, current generation and an eligible open,
unassigned, `agent-ready` issue. Failed, deferred or ambiguous refinement,
other receipt history for the same work, unresolved starts, blocking work-item
edges and operator holds refuse preparation. The refinement receipt, task,
accounting and audit remain unchanged; no delivery receipt is created here.

Preview and apply independently read GitHub outside the control lock, then
recheck database state under that lock. The preview binds issue content and
refinement provenance as well as policy/version/receipt state. GitHub reads
are not an atomic reservation: repeat ownership and eligibility checks before
separate submission. Bounded PR searches fail closed if incomplete. A PR
reference is reported as `validation_refinement_pr_reference_ambiguous:<PR>`
for inspection, not proof of implementation ownership. Even an incidental or
closed PR reference refuses this path; there is no automatic override. This
can require resolving scope ambiguity before preparation can proceed.

## Continue held completed refinement

`factory_preview_refinement_delivery` and `factory_apply_refinement_delivery`
create a distinct delivery receipt from a verified completed refinement whose
only direction is its conductor staging hold and conductor gates. The operator
supplies an exact source receipt and reason, never replacement restrictions.
The source receipt, task, accounting and evidence stay terminal and unchanged.

Pause admissions first. Preview requires no queued/active work or uncertain
starts, fresh open unassigned agent-ready issue evidence and no ambiguous PR
references. It binds the entire source, its completed refinement proof, current
ownership, policy and the inherited restrictions. Apply rechecks that evidence
and requires the exact digest and an actor-scoped idempotency key. Source changes,
new owners/deliveries, unsupported holds and stale previews refuse atomically.

Apply derives the existing bounded selected-issue policy, preserving generation
and caps, disabling automatic landing, broad intake and issue production. It
queues one delivery with every source restriction intact and an audited lineage
marker. Admissions remain paused; inspect the new receipt before enabling.
Admission checks the destination restrictions, source identity and policy against
that audit before creating any task, including when the marker was removed.
No step releases a hold, accepts delivery, starts a worker, or permits landing.
The operation does not reclassify ambiguous PR references or renew old work.

### Claude quota probe diagnosis

`factory_quota_probe_status` is a standing-operator read. It locates the latest
Claude probe audit and session and pages every unresolved probe, including older
ones. Pass `next_after_session_id` as `after_session_id` to continue. The global
`unresolved_probe_present` flag is independent of that page. Use returned session
and turn IDs with `factory_session_dispatch_diagnostics` for exact dispatch state.
No prompt, result, credential or guest token is returned. Missing sessions or an
empty page do not authorize retry, cancellation or release of a fence. This tool
neither refreshes provider observations nor starts inference. The normal probe
claim uses the same unresolved-session predicate as this diagnostic.

### Return to backlog intake

`factory_preview_backlog_intake(reason)` and
`factory_apply_backlog_intake(reason, expected_preview_sha256, request_key)`
prepare the primary repository to consume its existing agent-ready backlog after
a bounded validation. Both require a standing human operator. Preview requires
paused admissions, no active, queued or landing receipts and no reserved or
uncertain starts. Apply locks and rechecks that snapshot, including receipt
history and the policy version, and records the before/after policy in audit.

The change enables existing intake while preserving its label/exclusion,
dependency, cooldown and daily-limit settings, generation and task bounds. It
clears the manual issue allowlist so it cannot bypass backlog eligibility. An
empty manual list is valid only with intake enabled. The effective primary
repository filter must be exactly `agent-ready` and retain the needs-human
and wontfix exclusions. Explicit custom exclusions, including security-finding,
remain unchanged. Automatic
landing, refinement, closure and issue production remain off; other repositories
are paused. No receipt or hold is changed. Apply leaves admissions paused. A
separate explicit enable permits normal live eligibility and ownership checks;
a preview does not promise that any candidate is eligible or reserve work.
Identical request replay returns the recorded result; changed arguments conflict.

### Bounded refill control

`factory_status.refill` reports the effective refill/scout policy, bounded intake
selection, producer checkpoints and the existing `factory-retro-daily` schedule.
A schedule or enabled flag does not prove generation, eligibility, admission or
execution; inspect those stages separately.

Standing operators use `factory_configure_refill` with `operation=preview`, the
current control version, an offered `astra` or `opus` model, `enabled` and
`hourly`. Apply the reviewed digest with identical arguments and a stable request
key. Replays return the original result; changed versions, policy or job state
refuse. Enabling requires fresh subscription admission for the configured
generator and independent Astra reviewer. The preview digest includes the new-run
review contract. Hourly learning also requires Codex admission for its existing
Luna/Astra stages. No paid fallback is
added. Model aliases come from deployed availability, not an assumed version name.

The control changes only the refill block and, when requested, the existing daily
job's interval to one hour. It caps new collections at five findings, preserves
old pinned collections and asks planning to prioritize useful blocked work without
releasing holds. It retains intake selection, generation, receipts, budgets,
uncertainty and breaker history. In-flight refill and held or uncertain learning
execution refuse modification. It neither creates a scheduler nor dispatches work.
Existing cooldowns, admission, deduplication and low-yield stopping remain in force.

Progress-continuation refusals include bounded `subscription_diagnostics` for
standing operators: the refused model, its effective-policy or graph sources,
adapter family, provider feed, observation time and age. Missing or unsupported
feeds are distinguished from stale observations and unavailable subscription
capacity. This is read-only evidence; it changes no admission rule or quota and
never authorizes a retry. At most 32 model rows are returned, with the exact
refused model retained separately if truncated. No prompts or credentials appear.

### Native registration and caller discovery

`factory_status.control_catalog` reports only two entries from the running
native FastMCP registry: `factory_configure_refill` and `factory_amend_policy`.
Each includes registration, schema and description hashes, bounded argument
names and offered operations/models. It is operator-only, read-only and reports
`caller_visibility=not_observed`. It does not return the whole tool catalogue.

A missing native entry identifies a backend registration/deployment problem.
A registered entry that the caller cannot invoke identifies a remaining
upstream discovery or visibility gap, not its exact layer. The deployed path
also has Context Forge discovery, existing tool associations/visibility and the
client's cached catalogue. Compare the native hashes with supported gateway
and client discovery evidence when available. See
[the gateway discovery contract](../../mcp/ARCHITECTURE.md). Repository tests
cover native registration and the gateway's description rules; they cannot
refresh an already-running client's tools. Deployment alone is not proof of
caller exposure. Use the connector's supported refresh/reconnect workflow if
available, or request its operator to reconcile discovery without changing
access. If the tool remains absent, report that capability gap and stop the
activation. Do not substitute legacy configure, a new alias, direct database
writes, or a previously denied HTTP path.

### Separate restriction, continuation and refill activation

After code release, each live change still needs its applicable authority.
Neither code deployment nor a successful preview grants execution.

1. For a held paused receipt, preview `factory_amend_policy` with the exact
   receipt and `overlay={"remove_models":["spark"]}`. Apply the returned digest
   and a stable request key only under restriction authority. The control
   refuses a current graph/scalar pin or unresolved execution. Read back the
   effective pool, unchanged task/artifact/accounting, pause, holds and expired
   deadline. Historical Spark runs remain evidence, not current selection.
2. Under separate explicit continuation authority, obtain a new preview with
   `overlay={"progress_continuation":{"pr_number":N,"head_sha":SHA}}`.
   This binds the current version, exact receipt/task/repository/artifact,
   original grant, funding, graph revision and independent native review.
   GitHub head/required CI and genuine subscription observations for every
   effective model are rechecked on apply. Missing, unsupported, stale or
   exhausted feeds refuse. Apply only the fresh digest with a distinct stable
   request key. It authorizes the same task under a two-hour lease and clears
   its pause, retaining landing and other holds; it does not dispatch a worker.
   Renewals require new reviewed progress, fresh quota and no repeated failure.
   Read back the lease and actual native progress separately. Do not use
   ordinary resume to replace this grant, or retry an unknown original start.
3. Once `factory_configure_refill` is actually callable and activation is
   authorized, read bounded status and preview `enabled=true`, `model=opus`,
   `hourly=true`, the fresh `expected_version` and a concrete reason. The
   existing runtime maps `opus` to `claude-opus-5-5`; verify the deployed mapping
   and fresh Claude/Codex subscription admission before applying. New
   collections use that generator, then independent Astra challenge/planning.
   Apply identical arguments with the preview digest and a stable request key.
   Read back generation, receipts, holds, budgets, breaker history and the
   existing job's hourly interval. Reconcile a lost response through status
   and idempotent replay before another action. There is no paid fallback.
4. Observe generation, independent review, issue publication, eligibility,
   admission and execution as distinct stages. Existing intake exclusions and
   allowlists may prevent a valid published issue from being admitted. No step
   above releases them or authorizes busywork, another scheduler or a duplicate
   executor. Unknown execution such as receipt 996 stays fenced.

The authenticated integration target `factory_operator_flow_test` drives the
real MCP schema and local ledger with only GitHub/quota I/O replaced. It covers
two saved-artifact lineages, separate authority/digests, expired paused deadlines,
idempotence, changed grants/heads/revisions/funding/holds, fresh subscription
coverage and an unresolved original despite a later non-invocation. These are
local regression fixtures, not claims of live continuation or activation.
