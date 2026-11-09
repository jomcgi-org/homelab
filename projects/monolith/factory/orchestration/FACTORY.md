# Bounded issue delivery lane

Read this contract first. Rationale: [monolith section 4](../../ARCHITECTURE.md#4-agents); rollout history/checklists: linked issues.

## DBOS recovery needs process cessation

After orderly shutdown, a launched factory exits instead of relaunching DBOS. A rollout still fences admission on SIGTERM and drains native executor handoffs.

## Domain ownership

Factory owns orchestration, execution, recovery and private projections. Shared authentication establishes identity; controls require a standing human in `operators`. Browser decisions use Envoy’s `X-Auth-Email` claim. Each repository/issue/generation has one durable receipt; [two lanes](#two-lanes) bound concurrency.

### Sessions and legacy workflows

`agent_sessions` persists sessions, turns, pending messages, progress, model selection, workflow ownership and EmberVM lineage. Model aliases come from `factory.execution.model_family`: `spark` Muse, `pi-spark` Pi, `luna`/`terra`/`sol`/`astra` Codex, `opus`/`sonnet`/`fable` Claude. Session guests and `run_code` workloads are vsock-only. Legacy swarm runs `implement_then_review`, never merging. The drainer claims `claude_agent.routine_jobs` rows under the skip-locked lease every fifteen minutes, one fresh Luna session per job.

### Cadence and singleton ownership

Argo CronWorkflows own batch cadence, concurrency, deadlines and history. `internalApi` entries only POST a private leader endpoint; leader election confines side-effecting singleton hooks to one API replica.

## Conductor, Planner, and Executor

The **Factory** orchestrates Ember agents. Its operator-facing **Conductor** selects work, delegates tasks to the Planner and reports evidenced outcomes. A Conductor instruction never bypasses admission, controls or record authority. The Astra-selected **Planner** receives a task, acceptance, constraints and permitted knowledge, then creates or amends its DAG. The **Executor** owns graph execution and dispatch.

### Authoritative records and context

Owners: `swarm.work_item`; `swarm.factory_receipt` for submission, queue and admission state; `swarm.swarm_task`, `swarm_plan_version`, `swarm_node` and `swarm_node_run` for the task, DAG history, nodes and evidence; receipt `escalation_json` and `direction_json`; append-only `swarm.factory_audit` rows; the singleton `swarm.factory_control` row for authoritative state, policy and version. Queue order is `(created_at, id)`. Issues #5787/#5788/#5849 own Conductor knowledge, MCP exposure and Planner context.

### Submit, plan, execute, report

1. The Conductor submits an approved open issue through `factory_submit_issue`, receiving a durable `factory_receipt`.
2. Admission selects each lane’s oldest eligible unblocked receipt, creates its `swarm_task`, and the Planner records a complete DAG within pinned policy/allowance.
3. The Executor dispatches ready nodes and records attempts; the Conductor reports each task with evidence.

A paused task’s pending decision presents exact receipt/decision identities for `factory_decide` or `factory_request_brief`.

### Operating rules

1. Routine delivery needs required CI plus one independent exact-head review. The explicit `requires_operator` exception below binds an operator's decision to exact evidence and a finite expiry; ordinary delivery has no timed approval window.
2. One line per task: outcome, blocker, next action, starts and USD used.
3. Record cost per accepted PR on landing.
4. Planner turns share the task start budget; past half the starts, stop and respec by hand.

**Charter content.** The charter states authority, untrusted data, outcome/exit criteria and defaults the server cannot infer. Server-enforced mechanics (reserved keys, cost clamping, turn arithmetic, exception field minimums) live in the schema, refusal codes and `decision_feedback`. Restore removed lines only after repeated planner failures unprevented by feedback.

### Planner preview

Decision preview is staged behind `swarm.factoryPlannerPreviewEnabled=false`. Even enabled, the verifier returns no binding; results remain advisory. Acceptance lives on [the #6650 brief](https://github.com/jomcgi-org/homelab/issues/6650#issuecomment-5927671458).

## Availability and operator policy

Chart `swarm.factoryEnabled` defaults false; admission needs a separate durable operator transition. Policies name repo, issues, generation, caps, envelopes, models, base branch, timeouts and attempt bounds; admission pins them immutably. `max_task_turns_hard` is the envelope an accepted plan must fit; `task_budget_usd` records for accounting and never refuses a plan. `max_parallel_nodes` defaults to 1, `max_review_rounds` 2 and `max_review_recovery_rounds` 0.

### Serving more than one repository

Optional `repos` keys policy by repository slug. Non-primary entries, including staged `weave-hand/loom`, default disabled; landing serves only the primary repo. To enable it, set `swarm.factoryGithubWebhookRepositories` to `weave-hand/loom`, post `migration_policy_with_loom` through `configure`, confirm the entry is present and disabled, then enable it with `max_per_day: 2`. Adding the entry changes policy: `configure` returns `generation_not_advanced` while any task is admitted/uncertain; advancing generation retires homelab’s queued receipts. Post while idle. Acceptance: a ready loom issue yields a PR with passing CI and independent review artifact.

### Updating policy while work runs

Running tasks retain pinned policy; future admissions need a newer generation while tasks are active. An accepted advance cancels older-generation queued and escalated receipts in the same transaction.

### Two lanes

Class determines lane. Delivery produces pull requests: `bug-fix`, `mechanical-refactor`, `docs`, `judgment-analysis`. Advisory produces comments: `refine`, `advisory-diagnosis`, `advisory-triage`. `max_tasks` bounds each lane independently; an integer means delivery, leaving advisory at 0. `swarm.factoryBackgroundReserve` (default 2) is the background slots the factory leaves free. `swarm.factoryMaxConcurrentTasks` bounds the sum and must be at least delivery plus advisory.

### Reviewer fallback while the Claude window is spent

Optional `quota_guard` sets integer pause/resume percentages (defaults 85/75), resume strictly below pause. At/above pause, reviews use the next `model_pools.reviewer` member with provider quota. Judgment work waits for Opus, and an empty pool waits rather than skipping review. Unknown/hour-old readings cannot start or end fallback.

### Model pools

`model_pools` carries ordered preference lists; `select_model` takes the first member whose provider still has quota.

- `conductor`: planner rounds; default the `conductor_model` alone.
- `worker`: every other planner-added role; default the `worker_model` alone.
- `implement`: implement nodes, engine corrections, engine fan-in; default the `worker` pool.
- `refine`: the advisory briefing node; default `["spark", "sol"]`, narrowed to allowed models.
- `reviewer`: every review node; default `["opus", "astra"]`, narrowed, led by `reviewer_model`.
- `judge`: funding decisions and reservation-lease reviews; default `["opus"]` when allowed, else the `reviewer` pool.

Every pool member requires `allowed_models` membership.

### Reasoning effort by role and node

Dispatch pins effort by role: Planner and review nodes `xhigh`; funding judges, implement, integrate and correct nodes `high`; investigate and refine nodes `medium`. `role_effort` overrides any role. A Claude planner node reserves 1.5x `turn_budget_usd`, and the Opus review floor is $10. The judgment floor searches the `implement` pool, then the conductor pool, then the conductor model. Under the Astra coding policy (#6992) new workers and implementers prefer Astra, with `implement` at `medium` and `planner`, `reviewer`, `integrate` and `correct` at `xhigh`; Opus stays the independent reviewer. A plan edit may set `effort` to raise a role floor. A failed attempt escalates to the next pool member after the model that did the work, but a failed Astra attempt stays on Astra at least at `xhigh`, never downgrading to Sol. Capacity denials, proven no-model starts and judgment classes never escalate. Every `swarm.swarm_node_run` row records the dispatched model.

### Execution capacity and release intent

Admitted and uncertain receipts count across generations, except paused or landing-held receipts with no reserved or uncertain starts; missing evidence retains the slot. Factory nodes, drainer and probes share permits (`agentSessions.admission`: code 4/3/2, chart 16/12/2).

### Quota evidence and probe cleanup

KG admission needs at least one fresh permitting grant observation. Exhausted accounts never veto a healthy one. Tokenbroker refreshes usage; the Codex probe backs it up.

### Autonomous intake and refine

The `human` issue label declares human ownership over `agent-ready`, task class and allowlisting, excludes both lanes and queued admission, and survives label removal. The optional `intake` block defaults to `enabled: false`, `max_per_day: 5`, `cooldown_hours: 24`, `refine_enabled: false`. `close_enabled` with `max_closes_per_day` (default 3) gates whether a refine verdict may close.

An open `blocks` edge excludes its blocked receipt from sweeps and final admission. Sweeps refresh four minutes after a completed idle sweep, staging one observation per open `agent-ready` issue. [#7459](https://github.com/jomcgi-org/homelab/issues/7459) holds staged acceptance.

Intake admits at most one issue per lane per tick and at most `max_per_day` deliveries over a rolling 24 hours. An issue with a `succeeded` receipt in any generation is excluded as `delivered`. Operator-posted receipts take a separate linked-PR path, and local-authority work items are a second source.

Each intake class sets verification, implementer floor and gate:

- `bug-fix`, `mechanical-refactor`, `docs`: machine-verified, worker-pool floor, independent review plus required CI.
- `advisory-diagnosis`, `advisory-triage`, `refine`: advisory, worker-pool floor, no gate because nothing merges.
- `judgment-analysis`: judgment, Opus or better, independent Opus review plus a human spot check.

Issue labels select the class: `security-finding` and `needs-thought` give `judgment-analysis`, `bug` gives `bug-fix`, `documentation` gives `docs`, `todo` gives `mechanical-refactor`; unclassified `agent-ready` defaults to `bug-fix`.

A refine task runs one planner-class node, at most two attempts and no DAG, posts one `## Agent brief` comment and reaches one of five verdicts: `agent-ready`, `defer` (`needs-thought`), `needs-human` (`recommend:` plus one question), `reject` (`wontfix`) or `stale`; the last two close as `not_planned`. The server settles from a re-read of the issue, never from the node artifact alone. A new `leave_unmerged` hold needs server-observed operator provenance when `FACTORY_PLANNER_HOLD_REFUSAL_ENABLED` is on.

### GitHub read pacing

The factory GitHub reader shares a monotonic cooldown across callers and repositories after an observed rate limit or a response consuming the last request. Calls during it fail before network I/O and supply no cached ownership evidence.
(see: /projects/monolith/factory/orchestration/github_read_cooldown.py)

### Queue-driven backlog refill

New refill starts pin one verified base commit before either read-only role runs.
Collector and reviewer hydrate and inspect that same commit. Their versioned
artifacts distinguish completed inspections from blocked preparation; a blocked
collector stops before review, and blocked results cannot publish or count as a
successful empty audit. Existing run contracts and accounting remain unchanged.
Hydration verifies both HEAD and tracked/index cleanliness before model execution.
An authenticated, exact-dispatch native preflight error settles as failed outside
durable replay, retaining unknown cost and consumed starts. Missing or conflicting
receipt evidence remains uncertain. Preparation failures use the existing bounded
failure breaker. Refs #7485.

The optional `backlog_refill` block ships omitted; omission and `enabled: false` start nothing. Defaults per the #7376 decision: `min_eligible: 25`, `cooldown_minutes: 15`, `min_new_issues: 3`, `breaker_consecutive_low_yield: 3`, `max_per_24_hours: 24`, `model: astra`, `effort: high`. The generator runs one maintenance routine, recorded as `routine` on the start and rotated through `backlog_refill.ROUTINES`: `dead-code`, `finished-flags`, `test-gaps`, `useless-tests`, `abstraction-police`; a start without one uses the first. Independent xhigh review challenges every finding before issue writes. New starts prefer Astra, pinning Opus when cached quota walls Astra and policy permits Opus; dispatch needs fresh quota for the pinned model. Existing pins remain immutable. Astra remains required in `allowed_models`; hourly learning still requires Codex quota. A routine run's summary stays in its settled audit; a pre-routine start still publishes one `needs-human` inventory summary. Reaching the breaker opens one `needs-human` issue. `factory_configure_refill` keeps the breaker open unless `reset_breaker: true`, which records the reset without a full policy post or generation change. Optional `min_new_issues` sets the low-yield floor the same way. Acceptance stays on [#7376](https://github.com/jomcgi-org/homelab/issues/7376).

### Exact-event problem issues

Fresh native refill starts pin `repair_contract` version 2 and a control
epoch. Two matching, distinct-task attempt-one failures within 24 hours may enter
the existing repair loop only with native positive `not_invoked` proof and typed
preinvoke database metadata. The second original task remains machine-paused;
historical failures, breakers and uncertain execution remain unchanged. The
source can be closed charter #7366 only while its exact recurring contract,
unassigned GitHub identity, synchronized mirror and current ownership allow it.

After exact accepted repair rollout, the funding owner may renew the exhausted
original node from one to two attempts using only the original unused turns and
budget. It reserves room for every unfinished fixed role, including independent
review, and grants a finite 30-minute lease. The atomic authorization binds the
original pause, source run, rollout, policy and graph renewal; physical admission
rechecks that proof, holds, current accounting and exact reservation. There is no
paid judge, model verdict or budget increase. Unknown starts refuse. Collector
and reviewer retain their original model, source commit and read-only contracts.
No second repair cycle is created for that task. Successful retry alone is not
recovery: the producer also requires independently reviewed useful publication.
Historical starts and breakers are not adopted, and landing profiles stay fixed.

The earlier `factory-funding-v1` paid-decision protocol remains staged with
`PROVIDER_ROUTE_AVAILABLE` false. Its guest endpoint refuses before provider I/O;
subscription OAuth cannot be reused for direct Messages. Existing v1 waits do not
acquire v2 authority. See the [supported-authentication proposal](../../ARCHITECTURE.md#funding-stays-disabled)
for that separate future route.

Optional `problem_issues` creates GitHub issues from `node_stalled`, `workflow_stranded`, `recurring_blocker` and terminal `landing_recovery_exhausted`. It neither creates receipts nor admits tasks. The master switch defaults false; `recurring_blocker` defaults true behind it. Each tick scans at most 50 source audits, creating at most one issue within a rolling 24-hour cap of 3. `factory_configure_problem_issues` lets a standing operator preview/apply the master, recurring-blocker and stranded-workflow switches without changing generation, existing receipts or any other settings. Newly enabled sources are watermarked on the next producer tick. The existing producer owns deduplication and recursion prevention; ordinary intake/refinement owns future work. [MCP activation](../MCP.md#enable-recovery-problem-issues) records the exact arguments.

### Bounded repair landing delegation

Repair delivery still needs native scoped landing authority. The default-off
`repair_landing_delegation` owner derives it only after a standing human's
guarded `factory_configure_refill` preview/apply with `target: repair_landing`.
It records the standing decision and a separate `factory:repair-landing` audit
in the existing factory journal. It never writes a human per-head grant or
delegates identity credentials. Explicit per-head operator grants stay separate.

The initial boundary permits modifications of existing regular UTF-8 files in
the problem producer, intake loop, progress watchdog and feedback modules,
including their tests. Only fresh node-bound recurring-blocker repairs created
after activation qualify. Native independent exact-head review must attest the
complete diff's private, internal, reversible scope; public, security, authority,
credential and destructive changes require a separate operator decision. Missing
or ambiguous review scope refuses. The eight-file cap is delegation eligibility,
not a constraint on ordinary or legacy review artifacts.

Existing and default authorizations are finite trials: at most 24 hours and ten
exact derivations, with no automatic renewal. Explicit `delegation_mode: ongoing`
instead permits 1–10 derivations per rolling 24 hours until revoked or policy
drift, bounded by three cumulative derivations per repair task and source task
across authorizations. Exact unchanged replay spends nothing. Rate exhaustion
waits for the existing landing tick; it does not permanently refuse the repair
or invalidate already granted queue entries. Indexed task lookup and exact
producer-marker lookup avoid lifetime journal-scan cutoffs. No new scheduler,
ledger, migration or implicit authority renewal is introduced.

The optional `diagnostics` path profile adds only existing rationale and unified
diff parsers and their tests. It requires an explicit previewed choice. Mixed
executor/control/build modules that also enforce native gates remain excluded,
even when a proposed change is routine. See the concrete expansion boundary in
[MCP.md](../MCP.md#enable-bounded-repair-landing).

Landing rechecks source machine-pause provenance, both issues' ownership, holds,
unknown execution, review, immutable file evidence and CI before arming through
the existing protected queue. Revocation fences new arming and the native queue
observer disarms/dequeues outstanding grants on its next tick. It cannot undo
an external action that already completed. Policy replacement or drift invalidates
the authorization. Rollout and same-node recovery remain their existing owners.
See [activation and limitations](../MCP.md#enable-bounded-repair-landing).

### Standing delivery landing delegation

An ordinary intake-admitted delivery has no repair provenance and no owner-PR
adoption, so before this owner it waited for a per-head operator grant even
after independent approval and passing CI. `delivery_landing_delegation` is a
third default-off standing decision, configured through
`factory_configure_delivery_landing`. It records the standing decision and a
separate `factory:delivery-landing` audit per exact head. It never writes a
human grant, and explicit per-head operator grants stay separate.

Each receipt has one authority owner: an explicit grant, the owner-PR
delegation, the repair delegation for producer-marked repairs, or this owner
for everything else. None falls through to another.

The boundary is a deny-list, shown in full by preview. Only paths under
`projects/` qualify. The factory's own code, deployment and chart directories,
migrations, CRDs, cluster and platform projects, public surfaces, credential
and authentication paths, agent instructions and Bazel macros need an operator.
The excluded monolith directories include `factory/`, `core/`, `agent/`,
`agent_sessions/` and `swarm/`, covering shared identity, GitHub credentials,
MCP authorization and factory runtime dependencies.
At most forty files may change, as regular non-executable blobs. The typed
independent review must attest the complete diff at its exact merge base and
head as private, internal and reversible, with every public, security,
credential and destructive flag false. The deny-list is a second check on that
attestation, not a replacement for it.

`projects/monolith/BUILD` remains eligible because ordinary monolith deliveries
also change it. It declares the gazelle-excluded factory and guard tests; the
deny-list cannot detect removal of those declarations. Preview discloses this
limitation in `path_limitations`. Independent review and required CI must
preserve those test declarations.

Preview and apply bind the standing operator's subject as well as the policy,
version and bounds. File evidence records base and head blob identities, with
both names for renames. The review's `landing_scope.paths` lists only the new
name for a rename, matching GitHub's compare listing. Removed files must be safe
at the merge base; modified and renamed files must be safe in both trees.
Missing or truncated tree evidence refuses derivation.

Only the task's own `factory/<task-id>` branch qualifies, on an open, unassigned
issue with no held label. Receipt holds, unknown execution, recovery limits,
required CI and the protected queue apply as for the other owners. The operator
sets 1 to 50 new derivations per rolling 24 hours across authorizations; each
task gets at most three. Rate exhaustion waits for the next landing tick.
Disable, policy replacement, or a rollout that changes the deny rules or bounds
invalidates the authorization, and the queue observer fences outstanding grants.

Repository delivery remains default-off. Operational acceptance is outstanding:

- [ ] Verify the deployed revision containing this delivery.
- [ ] Obtain separately authorized standing-operator preview/apply evidence.
- [ ] Observe one eligible ordinary delivery landing without a per-head grant.
- [ ] Confirm revocation fences an outstanding queued derived grant.
- [ ] Record the delivery measure, regression checks and stop-condition verdict
      documented in [ARCHITECTURE.md](../../ARCHITECTURE.md#4-agents).

### Escalation decisions

A `needs-human` verdict arrives as two to four options, recommendation first; `effect` is one of `agent-ready`, `close`, `split`, `defer`, `hold`, `supersede`. Every unresolved escalation also carries the fixed escape group, of which only the dismiss is non-final. Deciding uses `POST /api/swarm/factory/decisions/{receipt_id}` with an exact `decision_id` and a stable `request_key`. `FACTORY_OPERATOR_EMAILS` is empty by default. The derived allowance is `max_attempts` over live unsucceeded nodes plus spent turns plus engine-insertable rounds; an edit exceeding the envelope refuses with `envelope_exceeded`, never for dollars.

### Routine planner pauses

With `FACTORY_ROUTINE_PAUSE_FUNDING_ENABLED` (chart false, GKE overlay true) and funding on, an ungated planner `pause` whose validated first option is `agent-ready` goes to the funding judge on the same task instead of a card: `factory_funding.routine_pause` records it as `routine_pause_funding_requested`, and the judge continues, steers or stops under the existing allocation, progress-watchdog, refusal and turn-ceiling rules. Receipt, task, graph, branch, PR and accounting stay unchanged; the grant's `next_plan` reaches the next planner turn. Typed gates keep `factory_gates` as their owner. Before routing, the pause must also have no landing hold, no `nonrenewable_recovery`, no review-recovery hold, no recorded `human_handoff`, an open issue with no `human`, `needs-human`, `needs-thought`, `security-finding` or `security` label, and its text must miss both `HUMAN_GATE_BACKSTOP` and the extra access/security pattern. The patterns are backstops, never authority. Under the control lock the request also needs an `enabled` lane, and it never rides a review already pending for another trigger. Scout tasks are never routed. The judge's escalate, any refusal at request time, and a stopped or paused lane leave the ordinary card for a person. The routed pause is kept on the review request, so exhausting the review refusal limit raises that card instead of failing the task. The routed pause is persisted on the review request as `routine_pause`, and its settlement waits, ungranted, while the lane is not `enabled`. `_issue` excludes labels, so labels are re-read separately. The shared hold check (`routine_hold`: task pause or cancellation, `nonrenewable_recovery`, landing hold, review-recovery hold, recorded `human_handoff`) runs before routing, again under the control lock in `request`, and again under the lock at settlement, where issue labels are also re-read. A database hold set at any point before the settlement commit yields no grant. Held labels are revalidated under the lock from the synchronized work-item mirror (the source the human-handoff fence trusts), and also from a fresh GitHub read just before settlement. A receipt without a mirror is never routine. The residual window is a label GitHub has but the mirror has not yet received (webhook latency) at the locked decision. The existing nonrenewable owner pauses the task, every other hold raises the planner's card, and a card that fails to post is retried after the refusal cooldown from the held refusal, without another review and without re-checking the hold.

A repeat pause after a grant without new work evidence (`work_progress` digest: artifacts and moved heads, never prose) does not pause the allocation: it writes one `recurring_blocker` audit, keyed by issue and code across re-admitted tasks, and the ordinary card follows. The `recurring_blocker` problem-issue source turns that audit into one deduplicated `bug` issue, which is refined like any other. It defaults on behind the producer's own `problem_issues.enabled` switch, so a deployment whose operator policy already enables the producer activates it on normal rollout. New source history is watermarked, so nothing is replayed, and `sources.recurring_blocker: false` keeps it off. A receipt carrying a factory problem marker never emits another, so repair does not recurse.

Escalated, uncertain and paused receipts stay owners but no longer make improvement context or scout transport incomplete (`factory_improvement_fence.PROGRESSING_STATES`: queued, admitted, landing). Queued work keeps first claim. A full lane also runs the improvement scout, which admits nothing.

A funding proposal above the approved `task_budget_usd` is accounting and is granted. Only the operator policy key `task_budget_binding: true`, read from the pinned or live policy, keeps it waiting for an amendment.

Activation: the chart flag on normal rollout. Repair issues also need the operator policy to enable the problem-issue producer (`problem_issues.enabled: true`), which may already be set; the source then follows its default. Measure over the 72 hours before and after the verified rollout: planner-pause cards raised, `routine_pause_funding_requested` outcomes (grant, steer card, stop), `recurring_blocker` audits, and judge refusals. The source is factory audit rows. Stop or roll back if routed pauses end in steer cards or stops more often than they grant useful turns, or if any routed pause later proves to have needed a person. Rollback: set the flag false; no historical receipt is rewritten.

## Execution and evidence

The reconciler expands only active receipts from a fresh control row. Source nodes receive a server-authored push-format contract for the exact task, node, attempt and dispatch key; a chained pre-push hook validates the outgoing commit in a detached linked worktree and refuses the push on missing tools or changed outputs. [Experiment #7458](https://github.com/jomcgi-org/homelab/issues/7458) stages repository-only.

Gate every checkpoint-sequence change to `drain_cycle`, `implement_then_review` and `start_session_workflow` with `DBOS.patch`; steps, `DBOS.sleep`, send/recv, events, child workflows and patch markers consume function ids. Add with `if DBOS.patch("<name>")`, remove with `if not DBOS.patch("<name>")`, make reorder or rename a remove plus add, and never wrap an unchanged call. Retire with `DBOS.deprecate_patch` only once no pre-patch workflow can be PENDING. Unpatched edits strand in-flight nodes: the reconciler audits `workflow_stranded` and settles the attempt as uncertain for bounded retry. A stale-checkpoint PENDING workflow settles the same way, audited `node_stalled` once. A replica lost mid-invoke holds the turn as `response_lost`.

Receipt preparation and attempt intent are separate when
`agentSessions.stagedReceiptsEnabled` (`AGENT_STAGED_RECEIPTS_ENABLED`) is enabled.
The default is false. One flag snapshot per executor supplies the handoff guard
that selects both staged preparation and promotion. Preparation preserves the
last attempted receipt; the existing pool/session locks plus the bounded local
handoff guard serialize promotion against the first response-loss hold. A hold
that wins cannot be superseded by a late preparation or promotion. Intent is
conservative evidence of a possible invoke, not proof a POST reached the guest.
Cancellation or an uncertain promotion commit cannot become `not_invoked` or
authorize another attempt. Callback credentials, exact ownership, accounting,
first-hold bounds and normal retry admission remain with their existing owners.

Roll out this protocol in three reviewed GitOps changes. First land schema
prerequisite [#7620](https://github.com/jomcgi-org/homelab/pull/7620) and verify
Atlas applied `20261104180000_agent_receipt_attempt_phase.sql` with its exact
reviewed SQL and checksum. Its metadata postcondition proves the column shape.
Only then land runtime [#7619](https://github.com/jomcgi-org/homelab/pull/7619)
with the flag false and verify the complete compatible backend and progress-ingest
rollout on every replica. In a separate reviewed activation change, set
`agentSessions.stagedReceiptsEnabled: true` in the private deployment overlay,
verify the rollout and rendered environment, and observe a naturally occurring
handoff retaining the exact attempted receipt followed by normal result adoption
and useful delivery. A merged default-off implementation or synthetic tests do
not meet [#7583](https://github.com/jomcgi-org/homelab/issues/7583) acceptance.

Rollback sets the flag false for future executors; already running executors
retain their snapshot. Keep the additive nullable column. Legacy NULL rows keep
their prior protocol. A prepared row also carries `superseded_at`, so old readers
and lease recovery exclude it even after binary rollback. An old callback writer
may capture its authenticated body, but cannot make that row active, and the new
promotion path refuses any nonempty preparation. Attempted rows use the existing
active-receipt representation and remain readable by old code. Never backfill,
revive or reinterpret a historical receipt or an existing uncertain execution as
part of deployment or rollback.

Staged promotion uses a one-second transaction-local PostgreSQL lock timeout.
An exact `55P03` refusal before attempt intent may retry four times with bounded
backoff, rechecking normal admission before every retry. Any published intent,
other database error, exhausted retry window, cancellation or hold keeps the
existing conservative failure/uncertainty path; it never authorizes another
physical POST.

The lost-before-session sweep (#6285) runs before task reconciliation and the deadline backstop, once the node's pinned `turn_timeout_seconds` has elapsed. The owning DBOS workflow must report `ERROR` or `CANCELLED`, or a lookup must prove it absent. Under the control lock shared with session creation, the proof also needs immutable run and start ownership and no bound or deterministic session, cost, outcome, permit or result receipt. Both ledgers then settle atomically as `failed` at zero cost with `no_model_post` and `never_dispatched`; anything else leaves the attempt untouched.

`provider_error_before_work` settles a guest provider error with no tool activity, diff or artifact at reported spend. `FACTORY_DRAINED_LOSS_SETTLEMENT_ENABLED` (chart false) settles a drained continuation only on durable `evicted`/`node_gone` proof. Sessions 8410 and 8416 remediation and live checks stay on [#6271](https://github.com/jomcgi-org/homelab/issues/6271#issuecomment-5750415773). The node workflow carries a brick-drain relay to an owner.

A planner decision applies together under one expected revision. The server owns review correction up to `max_review_rounds`; the planner returns only for a named deviation. Nodes able to start together dispatch together up to `max_parallel_nodes`, each on `factory/<task-id>-<node key>`, with an `integrate` node merging into the task branch. The guest hydrates the task branch in a dedicated linked worktree; transient artifacts go under `/workspace/src/.factory/`. Completion needs a non-draft PR on the exact task branch, a successful implementation node, the latest independent review approving the current head, and passing PR status checks, all re-read from GitHub. The body carries `Closes #<issue>` when no live checks are recorded; otherwise it carries `Refs #<issue>`, a `Conductor rescope:` line and the outstanding checklist, with no closing keyword.

#### Bound zero-turn settlement runbook

`FACTORY_BOUND_ZERO_TURN_SETTLEMENT_ENABLED` stages the post-guest proof from #6288 and defaults to false in the chart and GKE overlay. Once the owning workflow is terminal and the bound matures, the supervisor commits an exact cleanup fence, then settles run, start and session failed with the permit as `delivery_error` and the reserved ceiling charged. Live validation stays on [#6559](https://github.com/jomcgi-org/homelab/issues/6559) with its [runbook migration comment](https://github.com/jomcgi-org/homelab/issues/6559#issuecomment-6545695108).

### Turn observations survive queue deletion

`AgentTurn.usage_json["observation"]` stores one physical dispatch; null means unavailable, and settlement paths never backfill observations.
(see: /projects/monolith/factory/execution/store.py)

### Permit supervision boundaries

Capacity releases only to the explicit reconciliation owner after fresh authoritative terminal proof. Uncertain permit settlement is always on.

A native interactive `delivery_error` may release only its exact permit when
fresh terminal guest evidence predates the first dispatch and the complete,
unexpired receipt chain contains one matching attempted POST with its observer
released. Missing, prepared, superseded or conflicting receipts, results, pending
work, changed ownership and explicit holds refuse this proof. Factory and routine
owners retain their existing paths. Settlement preserves the failed turn,
unknown-outcome marker, cost, receipt and binding; it does not retry the turn.

### Guest reuse cleanup

The invoke terminal path releases the exact receipt it fenced, then runs completion cleanup. A receipt inside its acceptance window with nothing committed stays fenced.
(see: /projects/monolith/factory/execution/result_receipts.py)

### Landing

For an unheld, settled delivery requiring an explicit operator decision,
`factory_release_landing_hold` accepts `requires_operator` scope with evidence,
four acknowledged effect flags, a rollback plan and an absolute UTC expiry of
at most 24 hours. Preview binds the authenticated standing human, owned receipt
and PR, current base tip, immutable merge base, head, complete bounded file
manifest, native typed independent review and successful CI. Apply requires the
same actor and preview digest. The schema-2 grant is revalidated by the existing
landing and queue owners. Revocation retains an audit-specific tombstone;
expired, revoked or invalid explicit grants cannot fall through to delegation.
See the [operator approval contract](../../ARCHITECTURE.md#explicit-operator-landing-approval)
for supported files, review evidence and the asynchronous revocation limit.
This scope preserves all holds and task state. Routine model and runtime
maintenance retains the root delivery authority.

`factory_release_landing_hold` supports explicitly approved `public_docs` publication only for modifications to `projects/monolith/ARCHITECTURE.md` and `projects/monolith/factory/orchestration/FACTORY.md`. The server verifies exact-head paths, regular non-executable files and bounded UTF-8 text. Apply atomically records an audited grant and removes only the landing hold, preserving acceptance history, accounting and other holds. Replay acknowledges only; changed heads need fresh authorization. Except for the handoff below, native independent exact-head review remains mandatory. CI and protected queue gates always apply. The grant neither resumes execution nor refreshes accepted delivery.

Only receipt1045/task `t-a4702857-5c95-41cd-aa21-415e10bc9355`, PR #7492, supports `operator_handoff`: exact-head reviewer/implementer identities, durable reference, approval and explicit independence. The operator attests review authenticity; the server verifies original `7da9676f` acceptance, settled/quiescent execution and CI. Apply audits operator landing ownership. Native actions remain quarantined, including after stale grants; queue/hold fencing remains active. This never refreshes native acceptance or changes accounting.

The health loop reports landing/observed-merged work waiting over 30 minutes without settling or repairing it. A recorded merge must match the owned repository, base, branch and approved head. An exhausted implementation opens `implement_handoff_1` and `review_handoff_1` in one engine-owned graph edit only when all six hold: the deviation is `node_failed` on an `implement_` node; the version ledger has no applied `factory_handoff` round; exactly one open PR (drafts qualify) names the task branch, repository and base, matches any granted PR number and has no definite merge conflict; `pr-checks` and every non-advisory context succeed at its exact head; every live direct dependent is an unrun, unarmed review leaf; and the implementation and reviewer pool heads exist and are policy-allowed. A handoff consumes no review round; the ledger allows one per task, even after discard.

#### Dependency queue evidence

`factory/dependency-evidence` is a trusted monolith check, staged off with `factory.dependencyGate.enabled: false`. The publisher uses only the dedicated App installation token through the trusted broker path. A dependency PR needs a settled, unpaused, uncancelled latest receipt generation and both safe independent assessments on its exact head; changed, moved, unsafe or invalid evidence records `dependency_approval_invalidated` and that receipt never passes again. Success waits for exact-head `pr-checks`. Live checks are on [issue #6799](https://github.com/jomcgi-org/homelab/issues/6799#issuecomment-6545691566); #7186 owns the Renovate switches and schedule.

#### Landing recovery authority

A request-bound engine edit proves a bounded correction round exists. Completion still needs the paired recovery audit and fresh independent review of the delivered head. The landing-recovery deadline is a fixed cap that funding cannot extend. A refused request that is neither waiting for capacity nor at `recovery_limit` writes `landing_recovery_declined` with its reason whenever the reason changes or a new recovery round starts; the delivery stays eligible for the next tick.

#### Review publisher

The landing path contains a trusted `factory/review` check publisher, off by default. It accepts only a validated approval from a session distinct from every implementation session at that head, and uses only `FACTORY_REVIEW_PUBLISHER_TOKEN`. Activation lives on [issue #3835](https://github.com/jomcgi-org/homelab/issues/3835#issuecomment-6545692966).

Merge landing is off unless policy `auto_merge` is `true`; then exact-head delivery approval records `delivery_ready` and moves the receipt and task to `landing`. Exactly one factory pull request arms at a time. Recovery preserves the same task, graph, PR and accounting, at most two episodes per task. A merge is not task success: landing verifies a completed chart publication receipt on main whose source includes the merged commit, plus successful `pr-checks` on that source; only that proof lets `landing` settle `succeeded`.

### Outcome evidence: CI at merge, reverts after it

The manual read-only cohort report is [outcomes.md](../outcomes.md). `merge_ci` writes once, right after `merged`, and `factory_reverts.revert_tick` writes one terminal row per task; neither gates landing.

### Explicit review-only recovery

Review-only recovery is a separate paid episode on the escalated receipt, never a resumed DAG or renewed landing-recovery deadline. Late results cannot approve, a head or base change invalidates the result, and the control has no release or landing operation.
(see: /projects/monolith/factory/orchestration/review_recovery.py)

### A delivery pause is a decision request

The delivery planner's fifth action is `pause`, which leaves the lane rather than holding a slot. A pause carries the one `question` a person must answer and the options a refine escalation carries; option one always has effect `agent-ready`. The receipt goes to `escalated` and every reservation frees; the graph and accounting stay. `agent-ready` returns the receipt to `queued` with choice, detail and note as `direction_json`; `close`, `defer`, `split` and the escape group settle it `cancelled`, never succeeded. A re-admission is a whole new task, so the receipt carries `previous_task_ids`.

### Automatic pause questions

Automatic safety pauses keep their active receipt and reservations. The pause and its question commit under the control lock. The issue-body hash excludes the factory's own generated gate blocks (`issue_body_hash`), so a gate block the factory wrote does not read as a person's edit; a card stored under the older whole-text hash still matches through `issue_body_unchanged`. #7165 owns historical audit attribution and post-rollout observation.
(see: /projects/monolith/factory/orchestration/factory_decisions.py)

## Controls and uncertainty

`pause_admissions` lets admitted work finish, `pause_task` stops new nodes for that task, and `stop` durably fences factory admission and descendants. MCP and `POST /api/swarm/factory/control` use `request_control` with a stable `request_key` and the exact control version. `cancel_receipt` settles one queued receipt; `cancel_task` takes a `task_id` and `cancellation_reason` and refuses landing or settled receipts. Both are MCP-only. An operator pause never expires; a reconciler-written pause is cancellable after two hours only with all starts terminal. Only exact positive stop evidence for every owned workflow produces `cessation_confirmed`.

The deadline-release implementation stays hard-staged off, and agent workloads have a twelve-hour runtime backstop. An uncertain attempt keeps its capacity until exact control-plane evidence proves its guest ceased. Settled cessation proof extends to the at-most-once 502 and the never-invoked guest. Attempt stops lock factory control, capacity pool, then session; Ember owns physical cessation.

### Supervised cessation

Native interactive delivery errors after a drain redispatch may settle capacity
from a complete retained two-dispatch receipt chain. This separate proof accepts
at most three attempted receipts and two guests, including a first-dispatch
restore. Every guest needs fresh positive eviction/destruction evidence; absent
or live records refuse. The supervisor locks and revalidates all same-turn
receipts, current ownership and quiescence after observation. Retention must
cover both session and permit creation, every observer must have finished, and
the latest exact receipt must have no result. Earlier interrupted responses,
failed turns, costs, unknown markers and workspace bindings remain unchanged.
This releases only capacity and grants no retry, task recovery or hold override.

`factory_reconcile` kind `settled_retry` is an explicit standing-operator recovery for
one missing first-dispatch response followed by a proven uninvoked second
dispatch. Preview binds the receipt, session, permit, released observer, terminal
workflow and exact CP dispatch. Apply records intent before requesting a durable
restore fence for that original lineage. EmberVM requires complete retained
journal and successor coverage, positive idled parking, exact interruption and
node-gone eviction, and no dispatch-capable pending work. A missing record or
node-gone status alone never qualifies. The fence prevents a retained terminal
holder from authorizing a later restore, including after a CP restart.

Only a fresh matching applied fence lets the exact factory run/start fail with
reserved unknown cost. Native session, turn, settled permit and receipt history
stay unchanged; this is not a fabricated interrupted response or zero-cost
refund. Existing policy decides continuation and all other holds remain in
force. A lost fence response remains uncertain: repeat only the identical apply
and key to reconcile its durable intent. If local ownership changed after the
fence, settlement refuses and the exact lineage stays fenced. This operation
does not delete guests, invoke models, adjust quota or authorize public release.

### Proposed internal recovery authorization

`swarm.factoryRecoveryDelegationEnabled` defaults false, including the GKE
overlay. The private standing-human `/api/swarm/factory/recovery-authorizations`
preview/authorize route records one exact shared-owner request and digest.
It does not grant the Planner operator identity or credentials. The fixed
`factory:recovery-worker` actor consumes the append-only grant on the existing
Conductor cadence. The recorded human remains the separate authorizer.

The proposed scope is one evidence-only settled correction recovery or one
settled retry lineage fence, a maximum fifteen-minute admission window, and
at most two calls of the identical shared-owner request. The second call can
only replay the same idempotency key; it grants no additional task starts.
The underlying owner still requires all current identity, head, decision,
quiescence, funding, journal, successor and restore proofs. Its mutation lock
also checks grant expiry and revocation. Revocation stops future admissions;
an already admitted call may finish and must be reconciled from its journal.
Unknown calls remain fenced after expiry, revocation or exhaustion. One bounded
diagnosis is retained in the existing task's planner context without restarting
it. No unknown result or cost is rewritten, and budgets, cumulative start caps,
independent review and landing holds are unchanged.

Enabling this delegation and exercising any specific grant require separate
explicit approval. The draft implementation is not authority to invoke a
missing MCP control through another path.

`agents.drainer.learningInputDedupEnabled` also defaults false. When enabled
alongside the existing learning handoff, the routine pins its collected input,
code head and prompt. A matching prior completed empty output suppresses only
that repeated model turn. One report-only diagnosis stays with the existing
program #6781 owner, or its existing routine queue when no native owner exists.
Changed evidence or coverage allows another observation. Missing/unknown prior
output never counts as empty. Diagnostic output is a recommendation; it cannot
file or dispatch work, reset a breaker or establish an HTTP 502 root cause.

For an attempt whose run and start are both `uncertain` and whose node workflow is terminal, `factory_cessation.py` records a `cessation_intent`, destroys that guest at most twice, then settles run and start failed, the permit `supervised_cessation` and cost unknown in one transaction. The automatic path always runs; `request_cessation` runs the same steps for one operator-reviewed attempt fingerprint, never cancelling the receipt.

## Validation

Linux orchestrator CI and a real bounded operating trial stay the delivery gates; local tests are advisory. Kubernetes owns process recovery: a running conductor loop fails after 600 seconds without a completed reconciliation pass.

## Funding

Funding lives in `factory_funding.py`: `request` records one judge node, `settle` verifies its authority, `decide` resolves a validated artifact to `continue`, `steer` or `stop`. A continue or steer grants at most 20 work turns and a 30-minute review horizon. Reservations and unknown outcomes stay charged; a factory start reserves a ceiling and settles at the reported cost, falling back to the token-usage list price. A judge proposing a larger budget pauses the receipt for operator approval. Continuation contracts remain: the legacy pair below and an operator `progress_continuation` two-hour lease for one paused recovered task.

Native refill repair uses the deterministic version-2 remaining-allocation grant
above (#7583). It records `native_repair_resume_authorized` and an explicitly typed
`funding_granted`, with no funding-review request, settlement or model verdict.
The original task budget and turn ceilings do not change. A 30-minute deadline
covers the retry, independent review and publication; grants cannot renew it.
Only fresh typed positive not-invoked evidence on the exact recurring charter
#7366 contract can produce a bounded repair pause. Closed-charter authority never
overrides current human/security holds. The repair ticket exports only fixed role,
allowlisted stage/SQLSTATE and code-selected owning paths, reauthenticated before
publication. Exact verified repair rollout precedes the original-node retry;
useful reviewed publication is observed separately. Historical failures and
breakers are not enrolled. The paid `factory-funding-v1` profile and its guest
endpoint remain disabled; ordinary funding is unchanged.

The currently classified database stages belong to execution admission or result
receipts, outside both standing landing path profiles. Their repairs need explicit
operator-owned exact-head landing authorization; this implementation does not
expand those profiles. The exact existing route and its limits are recorded in
[the per-repair runbook](../../ARCHITECTURE.md#native-repair-landing-authority).
Eligible observation repairs exercise the real candidate
and queue owners in regression tests. Injected external rollout evidence tests
post-operator recovery with the paid provider disabled;
they do not prove autonomous native repair landing. Naturally occurring
useful-delivery acceptance remains unproven;
neither a passing fixture nor this PR completes #7583.

Eligible funding reviews include progress, proximity-to-landing and issue-value criteria and server-read evidence from `factory_judge.py`. Explicit doubt steers to an operator card, and the turn ceiling is the original allowance plus `factoryMeritJudgeMaxExtensionMultiple` times it. Missing pinned policy and historical requests without merit metadata retain their legacy handling.

The GKE overlay retains `factoryMeritJudgeEnabled: true` solely for pre-retirement charts. HEAD values can reach an older chart before promotion or during pin-lowering rollback; the compatibility key preserves its enabled behavior. The current chart and image ignore it. The staged #7585 scope allows this single config reference; #7596 tracks removal after those charts are no longer supported revert targets. Operational acceptance for #7585 is a verified rollout of the delivered chart revision, separate from #7502's checks and #7596's removal prerequisite.

Three switches stage funding, each false in the chart and enabled in the GKE overlay:

- `FACTORY_CONDUCTOR_FUNDING_ENABLED`: new funding reviews when work turns, review rounds or a lease run out. Enrolled reviews finish after it is disabled, a failed or stale review waits five minutes, and six consecutive refusals end the task.
- `FACTORY_RESERVATION_REVIEW_ENABLED`: a 30-minute lease on the running invocation, reviewed from five minutes before expiry. Heartbeats never renew it.
- `FACTORY_AUTONOMOUS_CONTINUATION_ENABLED`: the legacy pair, one final named correction plus independent review at a work-turn limit. It cannot renew itself, and funding supersedes it for enrolled tasks.

The no-progress watchdog is the `progress_watchdog` policy block: `enabled` and `threshold_turns` (default eight, one to 100) over settled work and planner turns. New commits or PRs rearm it; an operator resume or exact policy amendment records `funding_progress_rearmed`, so the next review waits a full step. A crossing without delivery requests the same funding review as an exhausted allocation, with no second classifier or card. Pending reviews are shared, failed ones keep the wait and refusal limit above, and only a successful settlement advances the checkpoint. Legacy watchdog cards still enact the operator's Resume, Stop or Rescope first.

### Native registration and caller discovery

Bounded factory status exposes refill/receipt-amendment hashes and argument names; no alternate route bypasses stale discovery.
(see: /projects/monolith/factory/MCP.md)

### Bounded failed-node renewal

Funding can grant one extra attempt on one exhausted failed work node whose
dependencies succeeded. The request binds its full node snapshot and latest
failed run, graph revision, run evidence, issue and effective policy. Settlement
rechecks them under the control and task locks. The graph records
`renew_node_attempt` with before/after bounds; historical revisions, attempts,
admitted pins, identity, branch and accounting remain unchanged.

Transient or unstarted funding-review retries retain the preceding request's
exact node and repair authority through the existing cooldown and refusal cap.
They revalidate source evidence; only funding's exact retirement of its expired,
unstarted gate may advance the graph. Retrying a review never consumes another
machine-pause resume or attaches retry authority to ordinary funding.

At most two such renewals are permitted per task across all nodes, each node
remains at or below ten attempts, and granted turns stay below the pinned merit
extension ceiling even when merit prompting is disabled. Existing no-progress
and funding-refusal limits still apply. Only an enabled lane and admitted task
qualify. Fresh GitHub labels and the synchronized work-item mirror must permit
recovery; absent mirrors, human handoffs, landing holds, review-recovery holds,
nonrenewable recovery, paused/cancelled tasks and unresolved starts refuse it.
Escalated or cancelled nodes retain their existing recovery owner. A new grant
does not answer an existing operator card or re-admit an escalated receipt.

### Work item pointer comments

`FACTORY_WORK_ITEM_POINTER_ENABLED` and `FACTORY_WORK_ITEM_BASE_URL` enable one GitHub comment per GitHub-sourced work item, linking its durable factory record. It is display-only and never read back.

### Conductor-owned reversible gates

**Why.** A landing hold is an operator act (#7449). With `FACTORY_PLANNER_HOLD_REFUSAL_ENABLED=true` the server accepts an unmerged hold only from an `operator:` cause, an authoritative work-item `human` label or a durable operator handoff; model prose cannot establish provenance. Refused planner gates persist `landing_hold_refused` evidence and actionable feedback before any gate or hold is written, and admission excludes only holds with matching planner provenance. Unknown and operator holds stay binding, including an operator reassertion after a refusal. The flag ships false everywhere and disabling it restores prior handling; a verified disabled rollout, reviewed activation coordinated with #7451 and the measurement remain on #7449.

Refine, investigate and planner artifacts carry a typed `gate` when a missing parameter, unavailable live check or existing delivery target would otherwise raise a card. A `reversible` gate carries its proposed `value` and `reason`; `spending`, `prod_deletion` and `external_account` keep the human decision path, as does any true flag in the four-boolean `effects` object. A recorded live-validation scope and checklist never authorizes performing the checks: the conductor records a default-off or staged repository `scope`, appends `live_checks` as unchecked issue lines, and the PR body follows the `Conductor rescope:` rule under Execution.

#### Handing off live checks

Operational handoff makes "repository work delivered, only live checks remain" an autonomous outcome rather than a page. A planner whose scope is already merged returns a `repository_delivered` gate; the server verifies each PR merged here and references the issue, then opens one child issue with the checks and `needs-human`, or uses the open `tracked_in` issue the rescope names. A delivery task first adopts the oldest matching open PR closing its issue and directs rebase onto main, repair and fresh independent review. `factory_notify_policy` decides per notification kind whether the operator pages or digests; cards stay on `/factory/escalations`.

### Restrict receipt model choices

`factory_amend_policy` accepts a `{"remove_models": [...]}` overlay removing unused models from one paused, quiescent receipt, leaving the task paused with no funding, execution or lease authority.

### Settled learning execution

`factory_reconcile` kind `learning_execution` lets a standing operator preview and acknowledge one exact `factory-retro-daily` execution after a fresh positive control-plane cessation read; missing remote records never prove.

### Settled quota-probe reconciliation

`factory_reconcile` kind `quota_probe` gives standing operators an exact preview and apply for a failed Claude probe whose permit supervision proved no guest delivery. It appends a resolution audit and never rewrites the unknown turn, permit or accounting.

## Factory evolution follows delivery authority

### Recurring repair discovery and observed recovery

The existing problem producer owns the automatic loop for stranded workflows
and recurring blockers after guarded operator activation. Deployment never
changes policy switches. Required recovery discovery accepts fresh, complete
per-grant ordinary permitted quota, including on-pace accounts without surplus
due for scouting. It retains preview's evidence, pacing and short-window guards
and the ordinary per-account availability check. A reported window of five hours
or less retains at least 10 percent headroom even when it is the only window;
window names do not decide this reserve. Opportunistic scouting keeps
its separate surplus, remaining-quota and reset-horizon thresholds. Discovery
grants no paid-start authority. Enabled refinement or
matching direct labels permit bug intake; exclusions, pauses and batches bind.
Required recovery repairs blocked work; optional scouting spends surplus finding
new work. The scout's due threshold must not suppress otherwise permitted repair
discovery.
Waiting recovery sources do not block unrelated sources. Ordinary intake,
exact-head review, CI, merge queue and managed
rollout retain authority over the separate repair issue.

Repair descriptions expose code paths and synthetic acceptance only; the
private ledger retains exact source identity. Recursive repair issues refuse.
For a fresh exhausted-node allocation pause, funding records exact machine
provenance before any repair. Verified rollout of the bound producer repair
permits one funding review for ordinary tasks, or the deterministic original-allocation
retry for native version-2 refill tasks, using native capacity release with no manual resume.
The review and settlement preserve cumulative caps, prior attempts, ownership,
uncertainty and explicit holds. Legacy cards are never adopted; operator
reassertion or concurrent evidence changes refuse this authority.
A passive check records recovery only after the repair's rollout proof and
a later successful attempt on the original task/node, with holds and unknown
starts absent. Missing bindings or historical cards remain unverified; this
does not re-admit them. The one-shot improvement scout and report-only learning
pipeline keep their existing contracts. See the activation and limitations in
[MCP.md](../MCP.md#enable-recovery-problem-issues).

Operator status exposes the separate repair ledger at
`refill.problem_issues.recovery`: observed source policy, historical watermarks,
terminal cursors, next signals, pending publication and recovery checkpoints.
The adjacent refill checkpoints remain backlog-refill evidence only. These reads
do not refresh quota or GitHub ownership, consume a signal, or grant any start or
recovery authority. A pending source is a diagnostic candidate, not permission.

The Conductor pursues continuous self-improvement and delivery velocity toward Joe goals. Factory-owned budgets and workflow policies evolve through their owning interfaces; dollars stay accounting. Root `AGENTS.md` defines standing delivery authority; the [evolution contract](../../../../docs/agents/evolution.md) defines how improvements evaluate and retain.
(see: /projects/monolith/factory/orchestration/factory_improvements.py)

### Owner-authored PR backlog

`intake.owner_pr_author` is omitted by default. A verified `{login, id}` enables
owner PR discovery on the existing intake sweep, including drafts; dependency
bots retain their separate policy. `factory_configure_owner_prs` previews and
applies only this field with version CAS, a preview digest and replay-safe request
key. Existing intake and refinement must already be enabled, without a refinement
batch. Disabling discovery does not discard already owned work.

A PR closing one accepted issue adopts that issue's work item and existing branch.
An unlinked PR becomes a PR-backed work item and enters the existing bounded
advisory lane. A server-verified `agent-ready` brief, citing an accepted goal or
standing maintenance policy, permits delivery on that same PR without another
issue. A label or author identity alone is not a scope grant. Older histories,
active/queued ownership, explicit holds and unknown execution win over discovery.
The feed prefers finishing PRs over fresh implementation candidates. It never
imports prior approval: independent review and required CI must cover the current
head, followed by the existing merge queue and rollout owner.

`owner_pr_landing_delegation` is a separate, default-off standing landing
decision, configured through `factory_configure_refill` with
`target: owner_pr_landing`. Preview binds the configured login and numeric owner
ID, repository/base, intake policy and latest relevant configuration event.
Disable/change/restore cannot revive an earlier grant. It explicitly permits
pre-existing PRs and already adopted receipts with genuine `receive_owner_pr`
provenance, canonical work-item ownership, and either an accepted issue or a
server-settled direct PR refinement. Legacy cards supply no authority.

The frozen `private_diagnostics_v1` profile contains the twelve Python source/test
paths listed by preview, with at most eight existing regular UTF-8 files modified.
An independent typed review must approve that exact immutable merge base, head
and path set with all public/security/credential/destructive flags false. Other
paths require separate operator scope. The existing landing owner derives at
most three grants per rolling 24 hours across authorizations, and three per
canonical repository/PR across receipts and generations. Exact replay is free.
An explicit per-head human grant always takes precedence, including malformed or
revoked grants. Owner delegation refusal never falls through to repair authority.

Fresh PR and canonical/referenced issue discussions, local ownership, history and
unsettled or claimed execution fence both arming and existing queue entries.
The same checks run under the existing control/task lock before queue mutation.
No producer, scheduler, merge owner or independent ledger is added. This narrow
capability does not establish that any current backlog PR fits its profile.

Fresh PR/discussion holds fence dispatch and verification. The private
`intake.owner_pr_backlog` projection reports the latest sweep's states, age,
receipt owners and blockers, alongside merged and verified delivery counts.
The shared text check recognizes only two exact, unqualified absence-of-hold
sentences seen in accepted briefs; it does not infer scope or release authority.
Other hold matches, quoted or qualified wording, labels and durable holds still
veto work. Existing discussion-refusal cache entries keep their normal expiry
and fresh observation path; this classification never clears them.
Superseded or closed PRs keep their history; advisory refinement cannot close a
PR. External merges retain the existing merged-delivery verification owner and
its review/operational fences. #7428 owns that owner's remaining external-review
handoff. Activation, rollout and a pre-existing PR reaching reviewed, merged and
verified delivery without manual selection remain open on #7597.
