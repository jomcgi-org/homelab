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

1. No frozen packets, hash pinning or timed approval windows: a PR needs required CI plus one independent exact-head review.
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

Optional `problem_issues` creates GitHub issues from: `node_stalled`, `workflow_stranded` and terminal `landing_recovery_exhausted`. It neither creates receipts nor admits tasks. Block and source switches default false. Each tick scans at most 50 source audits, creating at most one issue within a rolling 24-hour cap of 3. Rollout checklist and evidence live on [#6574](https://github.com/jomcgi-org/homelab/issues/6574).

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

### Guest reuse cleanup

The invoke terminal path releases the exact receipt it fenced, then runs completion cleanup. A receipt inside its acceptance window with nothing committed stays fenced.
(see: /projects/monolith/factory/execution/result_receipts.py)

### Landing

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

Admitted, uncertain and landing receipts keep their pinned policy across generation advances. Eligible automatic and watchdog questions on admitted or uncertain receipts remain actionable. Landing cards stay non-actionable. Chat and rebrief requests still require the current generation.

Automatic safety pauses keep their active receipt and reservations. The pause and its question commit under the control lock. The issue-body hash excludes the factory's own generated gate blocks (`issue_body_hash`), so a gate block the factory wrote does not read as a person's edit; a card stored under the older whole-text hash still matches through `issue_body_unchanged`. #7165 owns historical audit attribution and post-rollout observation.
(see: /projects/monolith/factory/orchestration/factory_decisions.py)

## Controls and uncertainty

`pause_admissions` lets admitted work finish, `pause_task` stops new nodes for that task, and `stop` durably fences factory admission and descendants. MCP and `POST /api/swarm/factory/control` use `request_control` with a stable `request_key` and the exact control version. `cancel_receipt` settles one queued receipt; `cancel_task` takes a `task_id` and `cancellation_reason` and refuses landing or settled receipts. Both are MCP-only. An operator pause never expires; a reconciler-written pause is cancellable after two hours only with all starts terminal. Only exact positive stop evidence for every owned workflow produces `cessation_confirmed`.

The deadline-release implementation stays hard-staged off, and agent workloads have a twelve-hour runtime backstop. An uncertain attempt keeps its capacity until exact control-plane evidence proves its guest ceased. Settled cessation proof extends to the at-most-once 502 and the never-invoked guest. Attempt stops lock factory control, capacity pool, then session; Ember owns physical cessation.

### Supervised cessation

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

Four switches stage funding, each false in the chart and enabled in the GKE overlay:

- `FACTORY_CONDUCTOR_FUNDING_ENABLED`: new funding reviews when work turns, review rounds or a lease run out. Enrolled reviews finish after it is disabled, a failed or stale review waits five minutes, and six consecutive refusals end the task.
- `FACTORY_MERIT_JUDGE_ENABLED`: adds the progress, proximity-to-landing and issue-value criteria and server-read evidence in `factory_judge.py`. Explicit doubt steers to an operator card, and the turn ceiling is the original allowance plus `factoryMeritJudgeMaxExtensionMultiple` times it.
- `FACTORY_RESERVATION_REVIEW_ENABLED`: a 30-minute lease on the running invocation, reviewed from five minutes before expiry. Heartbeats never renew it.
- `FACTORY_AUTONOMOUS_CONTINUATION_ENABLED`: the legacy pair, one final named correction plus independent review at a work-turn limit. It cannot renew itself, and funding supersedes it for enrolled tasks.

The no-progress watchdog is the `progress_watchdog` policy block: `enabled` and `threshold_turns` (default eight, one to 100) over settled work and planner turns. New commits or PRs rearm it; an operator resume or exact policy amendment records `funding_progress_rearmed`, so the next review waits a full step. A crossing without delivery requests the same funding review as an exhausted allocation, with no second classifier or card. Pending reviews are shared, failed ones keep the wait and refusal limit above, and only a successful settlement advances the checkpoint. Legacy watchdog cards still enact the operator's Resume, Stop or Rescope first.

### Native registration and caller discovery

Bounded factory status exposes refill/receipt-amendment hashes and argument names; no alternate route bypasses stale discovery.
(see: /projects/monolith/factory/MCP.md)

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

The Conductor pursues continuous self-improvement and delivery velocity toward Joe goals. Factory-owned budgets and workflow policies evolve through their owning interfaces; dollars stay accounting. Root `AGENTS.md` defines standing delivery authority; the [evolution contract](../../../../docs/agents/evolution.md) defines how improvements evaluate and retain.
(see: /projects/monolith/factory/orchestration/factory_improvements.py)
