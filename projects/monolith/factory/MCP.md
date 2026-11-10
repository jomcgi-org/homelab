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

Task detail's `start_diagnostics` reports the current `can_start` control gate,
effective deadline, and latest funding request, settlement and grant identities.
The funding projection retains pending-request relationships, cooldown and
operator/recovery fences while excluding private plans and refusal prose.
These are current observations, with no evidence that a reconciliation tick ran.
Malformed evidence produces unknown diagnostics.

`factory_diagnostics(kind="session_dispatch", session_id=..., seq=...)` reads
exact turn queue, permit and fence metadata. The compatibility name
`factory_session_dispatch_diagnostics(session_id=..., seq=...)` reaches that
same owner for connected callers whose tool catalog predates consolidation.
Both require a standing human operator and strict positive integer identities.
Neither contacts a guest, changes execution or authorizes recovery; recorded
cessation metadata is not independent fresh cessation proof.

Session dispatch also reports `factory_gate` after matching the session's
canonical task, node, attempt and workflow against its durable run. A missing
or conflicting identity remains unknown. This is the same `can_start` subgate
the executor checks before claiming a pending message. Transport, quota and
physical capacity gates remain unevaluated. A positive subgate grants no
dispatch or recovery authority. These reads never claim or alter queued turns.

If a cached caller still cannot invoke that restored name, the established
`factory_status(target="session_dispatch", session_id=..., seq=...)` reaches
the same diagnostics owner. Omit status paging and `include_recent`; both
identities are required. The default `target="factory"` retains ordinary
status behavior and rejects session identity arguments. Normal status includes
these native arguments and targets in `control_catalog`, without claiming that
the gateway or caller catalog has refreshed.

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
  operation.
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

`factory_refinement_delivery` (`preview`, then `apply`) creates a distinct
delivery receipt from a verified completed refinement whose only direction is
its conductor staging hold and conductor gates. The operator
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

`factory_diagnostics` kind `quota_probes` is a standing-operator read. It locates the latest
Claude probe audit and session and pages every unresolved probe, including older
ones. Pass `next_after_session_id` as `after_session_id` to continue. The global
`unresolved_probe_present` flag is independent of that page. Use returned session
and turn IDs with kind `session_dispatch` for exact dispatch state.
No prompt, result, credential or guest token is returned. Missing sessions or an
empty page do not authorize retry, cancellation or release of a fence. This tool
neither refreshes provider observations nor starts inference. The normal probe
claim uses the same unresolved-session predicate as this diagnostic.

### Return to backlog intake

`factory_backlog_intake(operation, reason, include_refinement,
expected_preview_sha256, request_key)` prepares the primary repository to
consume its existing agent-ready backlog after a bounded validation. Both
operations require a standing human operator. Preview requires
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
selection, backlog-refill checkpoints and the existing `factory-retro-daily` schedule.
A schedule or enabled flag does not prove generation, eligibility, admission or
execution; inspect those stages separately.

`refill.problem_issues.recovery` separately reads the repair producer's policy
observation, source watermarks and cursors, next source events, pending write,
latest creation/watch/observed-recovery checkpoints and ten recent events.
Missing recovery evidence stays null. This bounded metadata read does not check
live quota or GitHub ownership, advance producer history, or authorize recovery.

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

`factory_status.control_catalog` reports five entries from the running native
FastMCP registry: `factory_configure_refill`, `factory_amend_policy`,
`factory_reconcile`, `factory_continue_evidence` and `factory_configure_problem_issues`.
Each includes registration, schema and description hashes, bounded argument
names and offered operations/models/targets. It is operator-only, read-only and reports
`caller_visibility=not_observed`. It does not return the whole tool catalogue.

`factory_task_detail.nodes[].attempts[].run_id` identifies the exact persisted
node run. Use it as `source_run_id` for evidence continuation, never the attempt
number or session ID. Only the operator detail exposes it; the shared board
projection is unchanged. Pagination still returns at most three attempts per
node, so an omitted older run is not evidence of missing history.

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

### Enable recovery problem issues

After reviewed rollout, the operator enables the producer and recovery sources
through the guarded preview/apply below. Deployment never changes switches:
general policy history cannot reliably distinguish an intentional disable from
an old default. Enabling the master through this narrow control refuses when
an unrelated stored source is true; resolve that separate policy choice through
its owner first. The control never silently enables or edits those sources.

Required recovery discovery uses fresh per-account ordinary permitted quota.
The existing evidence, pacing and short-window guards still apply. A reported
window of five hours or less needs at least 10 percent headroom even when no
longer companion window is present. Optional scouting's surplus, remaining-quota
and reset-horizon targets do not apply. Discovery does not authorize a paid start.
A waiting source is retried on the next ordinary cadence, without a manual
trigger. It creates at most one deduplicated `bug` issue per tick within
the existing daily cap. Discovery accepts either enabled refinement of bugs or
matching direct intake labels, with repository exclusions, pauses and refinement
batches preserved. Closed gates and quota waits leave unrelated sources eligible.
Ordinary intake owns implementation, review, CI and
merge-queue landing. Repair tickets cannot recursively generate tickets. Public
issue bodies contain code paths and synthetic-test acceptance, not private
audit, receipt, workflow or quota details. A held source can produce a separate
code repair. Existing human cards and unknown starts remain fenced.

A new exhausted-node no-progress pause records `repair_allocation_wait` only
when the funding owner took a previously unpaused, admitted task with no card
or hold. After the producer's exact repair has accepted-merge and verified-rollout
evidence, the existing funding owner rechecks the original pause, card, graph,
attempts, policy, ownership and uncertainty. In one transaction it uses native
capacity release and prepares one exact-node funding review. It records
`repair_funding_requested` and retains the resolved machine card and its audit
history. A changed pause or card, including an operator reassertion, refuses.
The rollout is new review evidence, not invented source work progress. Only the
judge can grant another attempt, within the existing cumulative caps; settlement
rechecks holds, rollout and pause/resume identity. Transient or unstarted review
failures retain the same authority through bounded cooldown retries, without
another resume. Source evidence and the original rollout are revalidated; only
the funding owner's exact expired-gate retirement is an allowed graph change.
Refusals never create another repair authorization. Legacy cards are never adopted.

The existing scout remains a one-shot, operator-chartered evidence collector;
learning remains report-only. Neither is repurposed as an execution authority.
Blocker, stranded-workflow and observed-recovery events feed their existing
bounded private recovery sample, without entering publishable scout prompts.
The recurring event producer is the autonomous repair trigger. Its private
`problem_issue_recovery_watch` ledger observes at most 50 recent repairs per
tick. `problem_issue_recovery_observed` requires the repair delivery's recorded
rollout proof followed by a new successful run of the original task's exact
node, with no source hold or unknown start. A merge alone, another node or
task, missing node binding, or an existing escalation is not recovery proof.
This does not answer historical cards or re-admit tasks; those remain exact
autonomy gaps. General learning findings are not automatically promoted.

For activation after deployment, use the existing callable
`factory_configure_refill` as a standing human operator. Its explicit
`target: "problem_issues"` delegates to the same owner, digest and replay ledger
as the native `factory_configure_problem_issues` control:

1. Read `factory_status.version`. Confirm `control_catalog` advertises the
   `problem_issues` target and its source arguments on `factory_configure_refill`.
2. Call with `operation: "preview"`, `target: "problem_issues"`, `enabled: true`,
   `recurring_blocker: true`, `workflow_stranded: true`, that `expected_version`
   and a `reason`. Supply `model: "astra"` and `hourly: false` as compatibility
   placeholders; neither changes refill or learning settings for this target.
   Omit `min_new_issues` and leave `reset_breaker: false`. Mixed-target changes
   are refused. Inspect `before`, `after` and `effective_sources`.
3. Call with `operation: "apply"`, the identical target/placeholders/switches/version/reason,
   `expected_preview_sha256` copied from `preview_sha256`, and a new
   `request_key`. Retry identical arguments and key after a lost response.
4. Read status again. The next ordinary producer tick watermarks newly enabled
   sources; subsequent events can create bounded deduplicated `bug` issues.
   Existing intake/refinement settings must already allow future bug intake.

The control changes only those three switches. It preserves policy generation,
all other source/settings values, queued and active receipts, holds, ownership,
unknown starts and producer history. It neither files an issue immediately nor
dispatches work. Version or policy changes invalidate preview. To reverse a
switch, preview/apply its false value through the same operation. The control
does not replay historical signals or recover an existing escalated receipt.
Pass the version as a JSON integer. Strings, fractional values and booleans
are refused by the shared exact-integer contract.
The dedicated native control remains supported with the same arguments except
`target`, `model` and `hourly`. This existing-name adapter avoids requiring a
new tool name in a cached caller registry. If the caller also rejects its new
arguments, supported schema discovery must still be refreshed; server catalog
evidence alone cannot change a client's schema. Legacy `configure` is not a substitute.

### Enable bounded repair landing

Default-off standing delegation closes the producer-to-landing handoff for a
limited class of new repairs (#7601). It does not enable intake, producer sources,
global auto-merge or funding. Set those through their existing owners first;
any later policy change invalidates this delegation. No rollout migration
activates it.

After independent review and verified rollout, a standing human operator uses
the existing callable `factory_configure_refill`:

1. Read `factory_status.version` and confirm `control_catalog` advertises target
   `repair_landing`, `duration_minutes`, `max_repairs`, `delegation_mode` and
   `path_profile`, with `arguments_complete: true`. Caller schemas that
   reject these arguments must be refreshed; a new tool name is unnecessary.
2. Preview with `operation: "preview"`, `target: "repair_landing"`,
   `enabled: true`, `model: "astra"`, `hourly: false`, `duration_minutes: 1440`,
   `max_repairs: 3`, the current `expected_version`, and a trial `reason`.
   This selects the finite trial (the default `delegation_mode`). For ongoing
   operation explicitly set `delegation_mode: "ongoing"`, omit
   `duration_minutes`, and use `max_repairs: 3` per rolling 24 hours.
   The model/hourly arguments are unchanged compatibility placeholders.
   Omit producer switches, `reset_breaker` and `min_new_issues`. Inspect the
   returned fixed `allowed_paths`, policy digest, mode and bounds. The default
   `path_profile: "observation"` retains the original file boundary. Explicit
   `path_profile: "diagnostics"` adds only the four parser paths described below.
3. Apply with identical arguments except `operation: "apply"`; add a fresh
   `request_key` and the exact `expected_preview_sha256` from preview. Reuse
   the same key and arguments after an uncertain response. This writes only
   standing authorization to the existing audit journal and increments control
   version. Policy, generation, receipts and ownership stay unchanged.
4. Re-read `factory_status.repair_landing`. A successful replay reports the
   original command's outcome, so replaying an old enable after revocation
   cannot establish current authority. Fresh source and producer events must
   occur after this authorization. There is no historical adoption.

Trial duration is 1–1440 minutes; the limit is 1–10 lifetime exact machine
derivations. Existing authorizations remain trials, including their original
expiry and replay identity. No deployment or replay upgrades them.

Ongoing mode has no expiry or lifetime global count. It requires explicit
preview/apply and enforces 1–10 derivations per rolling 24 hours, plus at most
three cumulative derivations per repair task and per source task. Counts span
standing authorizations: revoke/re-enable or a new rate window cannot reset
either task's cumulative bound. Each changed head, review or source proof needs
another slot; unchanged exact replay spends none, even after unrelated repairs.
The existing landing tick retries a rate-limited repair when the window permits;
this writes a scope wait, not a permanent refusal. Already granted queue entries
are not invalidated merely because the rolling allowance is full. Task/source
exhaustion remains an explicit exception. No scheduler or automatic renewal is
added. Status reports mode, expiry, rate-window usage and cumulative limits.

A derivation does not guarantee queue entry and is not refunded after refusal.
To revoke,
preview/apply `enabled: false` with the current version, the same target and
explicit mode/count (duration only for trials). New arming stops immediately under the control lock;
the existing observer disarms/dequeues outstanding grants on its next tick.
Already completed external actions cannot be undone by revocation.

Only new node-bound `recurring_blocker` repairs from a fresh recoverable machine
pause qualify. The initial fixed paths are `.py` and `_test.py` files for
`factory_problem_issues`, `factory_intake_loop`, `factory_progress_watchdog` and
`factory_feedback` under `projects/monolith/factory/orchestration/`. Changes must
modify existing regular non-executable UTF-8 files. A separate independent
exact-head review must provide `landing_scope`, including the PR's exact merge-base commit
and complete path list, and positively classify the change as private, internal
and reversible. Its public/security/credential/destructive flags must all be
false. Authority or safety-gate changes and ambiguous scope require an operator.
Required CI, protected merge queue and source/repair holds remain binding.
Unrelated main-tip movement preserves that reviewed diff. A changed merge base,
head or file evidence refuses and needs fresh independent scope review.
The eight-file ceiling applies only to machine delegation eligibility. Ordinary
and legacy reviews, including `requires_operator` reviews with more files,
remain valid artifacts.

The opt-in `diagnostics` profile additionally permits existing `.py` and
`_test.py` files for `factory/execution/rationale` and
`factory/orchestration/unified_diff`, under `projects/monolith/`. These parse
private execution rationale and diff presentation; they do not grant execution,
funding, landing or credential authority. Selecting the profile is a separately
digested operator choice, never an automatic expansion of an older grant.

Routine faults in mixed-owner files are not inherently security exceptions, but
this file-bound mechanism cannot authorize only their non-authority functions.
For example, `factory/execution/push_format.py` also verifies admitted dispatch
and push-branch identity; `factory/orchestration/runtime.py` dispatches recorded
nodes; `factory_controls.py` implements standing operator decisions; and
`bazel/tools/ci/` owns required checks. Those complete files remain excluded.
A concrete safe expansion is to extract a pure parsing/presentation helper into
a named private module through independently reviewed delivery, then explicitly
add that module to a previewed profile. Granting whole mixed-owner files instead
needs a separately reviewed authorization design that preserves those guards.
Root `AGENTS.md` requires preservation of admission, ownership, limits, CI and
queue gates; `FACTORY.md` reserves control transitions to a standing human.
This patch changes neither rule and cannot authorize repairs to its own guards.

Unsupported automatic scope includes arbitrary stranded workflows without
node-bound machine-pause proof, historical cards, authority-owner repairs
(funding, graph, controls, landing or MCP), public files, new/deleted/renamed
files, security or credential changes. This follow-up itself therefore cannot
use the delegation it adds. It requires the assigned independent delivery owner.
Native integration tests exercise real producer, intake, independent review,
scope derivation and queue arming. They do not establish live acceptance; keep
#7583 and #7601 open until the full proof below is recorded privately.

### Verify fresh same-node recovery

Keep the recovery tracking issue open until live evidence proves both repair
delivery and resumed useful work. Activation, a funding grant and a merged PR
are intermediate outcomes. Keep receipt, run, quota and audit evidence private.

1. After verified rollout, confirm effective producer sources and an enabled
   ordinary bug-intake lane. Observe a new producer-created repair issue from
   a genuine recurring blocker. For unattended landing, activate the bounded
   delegation before the fresh source signal and use its supported file scope.
   Stranded workflows outside that scope still need their own landing authority.
   Source watermarks exclude
   historical signals. Follow its ordinary implementation, exact-head review,
   CI, merge queue and verified rollout. Do not submit a duplicate tracking task.
2. Select an existing fresh **admitted** task through private status and task
   detail. Its exact exhausted `work` node must have only failed prior attempts,
   succeeded dependencies and fewer than ten allowed attempts. Conductor nodes
   and evidence-continuation nodes are excluded. The task must retain cumulative
   turn authority and fewer than two prior node-attempt renewals. Verify fresh
   issue ownership and the synchronized label mirror, enabled execution, no
   cancellation, human/nonrenewable/landing/review hold, unresolved start
   or nonterminal run. An escalated receipt is ineligible even if its last work
   attempt failed. For end-to-end repair observation, select the original task
   and node bound by that repair's private `problem_issue_recovery_watch`.
   For a no-progress pause, require its fresh `repair_allocation_wait` provenance;
   the funding owner must release it for review without an operator resume.
3. Let ordinary reconciliation request the existing funding judge's decision.
   It detects the exhausted node automatically; review may refuse or steer.
   Retry-specific refusals do not suppress ordinary lease/deadline funding or
   closed-objective handling.
   On approval, verify `funding_granted.node_retry` and the immutable
   `renew_node_attempt` plan revision name the original node, preserve previous
   attempts and add exactly one allowance. Then verify a new actual attempt on
   that same task/node completes useful work. A grant alone is insufficient.
4. For the full loop, require `problem_issue_recovery_observed`: the repair
   delivery succeeded with recorded rollout proof, followed by a new successful
   run of its original source node. Independent retry proof without that repair
   binding does not establish the producer-to-recovery loop.

If no eligible live candidate exists, report acceptance pending and wait for
ordinary intake and real failure evidence. Do not inject a failure, answer an
escalated card, clear a hold or unknown invocation, replace the node, or start
competing work to manufacture proof. Historical cards and missing source-node
bindings remain unsupported by this automatic recovery path.

### Recorded landing refusals

Each compact status row includes `landing_refusal`: the latest recorded
`merge_arm_refused` reason, PR number, audit ID and timestamp. A newer recovery
request or accepted merge supersedes it. Arming or merely observing a merged PR
does not clear the recorded refusal. This is a bounded private
audit projection, not a live GitHub or queue query. Null does not prove landing
eligibility or queue membership. A `merged_delivery_identity_mismatch` means
the existing landing owner refused acceptance; inspect its approved delivery
identity and the current PR through the owning review/landing workflow. Status
does not rewrite the approved head, accept a different merge or clear a hold.

### Recorded landing waits

Each compact status row also includes `landing_wait`: the latest recorded
`landing_authorization_wait` reason, PR number, audit ID and timestamp. It says
why an approved delivery has no landing authority yet, for example a path
outside the delivery delegation or a missing current-head review. The landing
tick records a new row only when the reason or PR changes. A newer grant,
recovery request, arm or merge answers it. Null does not prove authority.

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
   An appended validated `leave_unmerged` gate is compatible only when its
   scope and effects exactly match an existing bound gate and it retains every
   required live check. Original records and all other direction fields remain
   exact. Each lease binds the accumulated holds, so a later renewal cannot
   remove one added after the initial grant.

   If the existing correction allocator already paused an authorized task,
   use the separate `progress_renewal` overlay with the exact PR and current
   reviewed head. Preview and apply bind the current version, original grant,
   graph, accounting, artifact and factory-owned allocation pause. This mode
   computes one further finite lease under the original authority, preserving
   holds and accounting. It refuses a later operator pause, cancellation,
   another pause cause, unresolved execution, stale evidence, reused heads or
   repeated failures. It never replaces the initial grant. Apply alone resolves
   the bound allocation pause. `factory_task_detail.progress_continuation`
   reports the last recorded lease or precise renewal refusal, including its
   reviewed head and subscription diagnosis when relevant. This historical
   observation is not a fresh admission verdict.
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

### Inspecting historical cessation

`factory_reconcile` kind `settled_retry` previews or applies one exact receipt/session/
permit recovery with the same stable request key. It covers only an empty first
response receipt whose observer released ownership, followed by a settled
uninvoked second dispatch. It does not accept the learning or quota-probe shapes.
The preview names the permanent side effect: that original guest lineage loses
restore authority. A fresh authenticated, bounded CP journal proof and complete
successor/pending-work exclusion are mandatory. Apply uses CP digest/CAS and
idempotency, then rechecks the local owner under control/pool/session locks.
It fails the exact factory run/start at its existing reserved unknown cost while
preserving the native session, turn, permit, result receipt and every other hold.
It never creates a result, deletes a guest, invokes work or changes quota.

An `outcome_unknown` fence response leaves the factory unresolved and its intent
durable. Reconcile using identical apply arguments and key. A response followed
by local CAS refusal reports that phase explicitly; it does not undo the fence
or settle changed work. Normal factory policy owns subsequent continuation.
This path shares the single-writer CP authority assumption below and does not
claim that orphan workspace-upload RPCs ceased; those cannot dispatch work and
the durable fence prevents their bytes from restoring the retired lineage.

`factory_diagnostics` kind `permits` projects `observation.recorded_cessation` from the
saved supervisor audit: its identity hash, guest identity, generation, invocation
timestamps, observation time, terminal state, cessation marker and response hash.
Only those typed, bounded fields leave the query. The free-form evidence document
is not returned. Invalid or absent fields remain null; the tool does not contact
the guest, validate the old response hash, or authorize recovery.

`permit_supervision._record` checks the exact permit/session/turn identity and a
fresh positive guest response before saving `guest_cessation_confirmed`. It stores
the observation and settles the permit in one transaction, retaining the failed
turn and its unknown result. A settled observation is not overwritten by later
supervision. The stored identity hash covered the pre-settlement state; it is
not a signature or a recomputable hash of the now-settled permit.

Historical settlement and a current recovery preview answer different questions.
`factory_reconcile` kind `learning_execution` requires fresh positive authority evidence
for the exact guest, generation and invocation in both preview and apply. When
the ordinary guest read returns a typed 404/410, it may read EmberVM's bounded
`cessation-evidence` projection. Permission failures and timeouts do not select
that path. Missing rows, an old reason string, and saved hashes are never proof.

The historical path accepts only a complete retained original-generation journal
chain establishing positive parking followed by idle eviction. The control plane
must prove no surviving lineage holder, retained successor creation, pending
dispatch-capable restore or unresolved residency claim. The verifier's
`single_writer_dispatch` authority assumes the deployed one-replica `Recreate`
control plane. Check the trimmed deployment's strategy, replica counts and
observed generation before live reconciliation. A crashed manager's orphan
Restore/Prime RPC may finish, but cannot register, Assign or invoke a session
through its dead owner; the proof does not claim zero orphan resource work.
Compacted or bounded-out history refuses
recovery. Monolith additionally checks the exact local history and all current
or prior lineage owners under its control/admission locks. It validates the
authority's own observation timestamp within thirty seconds, separately from
the local response time. Preview and apply bind the stable evidence digest;
fresh observation time is recorded separately in the audit so a new read does
not invalidate an otherwise identical preview. The acknowledgement changes only
the cadence guard: unknown results, failed sessions, costs, receipts and holds
remain unchanged. Native refusal codes identify insufficient historical proof.

For an expired guest, corroboration may remain in EmberVM's durable `ops` journal.
Terminal session projections become eligible for pruning after seven days,
subject to residency-intent retention pins. Journal prefix compaction uses a
separate configured horizon, normally thirty days. Retention policy is
not proof that a particular event still exists. An authorized evidence reader
would need the exact session's creation, invocation and terminal events, their
sequence/timestamps and generation, and coverage of any later transitions.
`session_destroying` is intent, not cessation. A terminal event's writer/version
must establish its teardown semantics. The supported reconciliation control uses
only the authenticated bounded verifier, never a general database or journal
reader. It exposes no raw journal payloads or session credentials.

Any proposal to accept a durable receipt must first corroborate the saved fields
against that identity-bound history (or a verified archival snapshot), prove the
terminal identity cannot resume, and retain fresh checks for all local work and
ownership. It must bind the exact evidence into preview/CAS/idempotency and retain
unknown results, accounting and unrelated fences. If corroboration is unavailable,
keep recovery blocked. A new operator attestation would need independently
verified teardown/absence evidence for the exact worker incarnation and separate
explicit authority; merely asserting that the record is missing is insufficient.

### Owner PR feed configuration

`factory_configure_owner_prs` supports `preview` and `apply` for one verified
GitHub `owner_login` and positive numeric `owner_id`; omit both to disable.
Use current `factory_status.version`, a reason, then the returned preview digest
and a stable request key on apply. The control changes only
`intake.owner_pr_author`, leaves generation and unrelated controls untouched,
and starts no task. It requires standing operator authentication. Enablement
requires existing intake and refinement with no restricted refinement batch.
The existing-name compatibility route is `factory_configure_refill` with
`target=owner_prs`, `enabled=true`, `owner_login`, `owner_id`, `model=astra` and
`hourly=false`; leave refill/problem-source options omitted/default. Disabling
uses `enabled=false` and omits both owner fields. It delegates to the same owner,
so cached client catalogs need no new tool name. The native control catalog
reports the target and argument names independently of client visibility.

Read `intake.owner_pr_backlog` for the latest observed backlog and separate
merged/verified delivery counts. An activated producer or queued receipt is not
operational acceptance; #7597 retains the real autonomous delivery demonstration.

### Owner PR standing landing decision

`factory_configure_refill` also accepts `target=owner_pr_landing`. This distinct
decision is default off. Use `model=astra`, `hourly=false`, the current version,
`enabled`, and a reason. Omit owner identity and all refill/problem/repair
options: preview binds the already configured numeric owner identity and lists
the exact frozen private paths. Apply with its digest and a stable request key.
It preserves policy, producer settings and history, and dispatches no work.

The preview explicitly permits pre-existing adopted PRs and fixes the bounds at
three new exact-head derivations per rolling 24 hours across authorizations and
three per canonical PR across receipts/generations. Required CI, independent
typed review and the existing queue remain mandatory. Status is reported under
`factory_status.owner_pr_landing`. Disable fences existing queued grants on the
next landing observation. Changing and restoring intake policy cannot revive an
earlier authorization. Activation alone is not autonomous backlog acceptance.

### Standing delivery landing decision

`factory_configure_delivery_landing` configures the default-off standing owner
for ordinary intake-admitted deliveries. See
[FACTORY.md](orchestration/FACTORY.md#standing-delivery-landing-delegation) for
the boundary.

1. Read `factory_status.version` and confirm automatic landing is enabled.
   Refresh the client's tool catalog if `factory_configure_delivery_landing`
   is absent. It is published automatically by the gateway; it has no
   `factory_configure_refill` target or `factory_status.control_catalog` entry.
2. Preview with `operation: "preview"`, `enabled: true`, `expected_version`,
   a reason and `max_landings` from 1 to 50. Review `deny_rules`, `max_files`
   and the bounds, plus `path_limitations`. The rules exclude the factory and
   its framework packages, deployment and schema, credential and isolation
   code, public routes and `projects/monolith/ARCHITECTURE.md`. Other private
   project code is eligible, including `projects/monolith/BUILD`. The deny-list
   is a second check, not isolation: safety relies on the typed independent
   review, required CI and the merge queue. Joe set this boundary on 2026-11-10.
3. Apply identical arguments with `expected_preview_sha256` and a new
   `request_key`. Replay the same key after a lost response.
4. Re-read `factory_status.delivery_landing` for `derivations_used` and
   `derivations_remaining`.

To revoke, preview and apply `enabled: false` with `max_landings` omitted. A
delivery outside the boundary records a `landing_authorization_wait` whose
reason usually starts `delivery_landing_`, and still needs
`factory_release_landing_hold`. Activation dispatches nothing and is not
evidence that any delivery landed.
