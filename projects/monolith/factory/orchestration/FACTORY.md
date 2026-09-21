# Bounded issue delivery lane

## Domain ownership

Factory is one domain inside the monolith. It owns private interactions and
MCP, published public views, task orchestration, execution, budgets, and recovery.
Shared authentication verifies caller identity; factory entry points enforce
which operations and records that caller may access. Public composition loads
only the read-only factory descriptor and published projections.

The private registry composes `factory.module` once. Its lifecycle owns both
the conductor/DBOS runtime and session maintenance, including partial-start
cleanup and the process watchdog. Implementations live in `factory.orchestration`
and `factory.execution`. The old `swarm` and `agent_sessions` packages contain
compatibility shims for durable exception identities and the existing Discord
adapter. Database schemas and durable workflow identities remain stable.

The private interface starts at `/factory`, with decisions at
`/factory/escalations` and run, session, and voice details at
`/factory/execution`. The launcher presents one Factory entry. Legacy `/agents`
links and browser API requests resolve internally to the corresponding factory
routes on the private host, preserving query parameters and request methods.
The backend `/api/agents` contracts remain stable. Both legacy and canonical
prefixed VM stream paths retain the gateway's 600-second timeout.

The public reader ships `factory.public_view` and excludes private execution,
orchestration, access, and projection modules. It reads only published public API
views and snapshots with the existing restricted database role.

Discord integration is outside this consolidation. Existing integration behavior
is preserved pending an explicit retirement or redesign. This change introduces
no general-purpose cross-domain factory API or separate factory deployment.


The first autonomous lane reads explicitly selected GitHub issues, records one
durable receipt per repository/issue/generation, and admits up to the policy's
per-lane `max_tasks` at a time, with the chart's
`swarm.factoryMaxConcurrentTasks` (4 today) bounding the sum of the two lanes.
`max_tasks` is a concurrency, not a lifetime count: as tasks settle the lane
keeps admitting until its issue list is exhausted, so it runs without an
operator re-arming it.
A planning session in Ember builds the whole graph at plan time and is called
back only when the plan deviates. The server reconciles the mutable graph and
dispatches each admitted node as an independent DBOS workflow with immutable
inputs. Planning does not run in the monolith process.

This slice prepares a reviewable PR. The broader factory conductor in #5784,
shared admission across all execution surfaces, other incident feeds and
autonomous landing retain their own acceptance gates. A successful node is not
evidence that its task delivered the requested outcome.

## Conductor, Planner, and Executor

The **Factory** deploys and orchestrates agents on Ember to produce features
and address issues. The **Conductor** is its operator-facing manager. It
receives goals and steering, maintains a prioritized view of work and bounded
task context, selects work, delegates each selected task to the Planner, and
reports outcomes with evidence. A Conductor instruction proposes work through
the existing factory interfaces. It does not bypass admission, controls, or
the authority of the record being changed.

The **Planner** is the Astra-selected per-task planning role. It receives one
selected task, its acceptance and operator constraints, current factory state,
and permitted task-relevant KG knowledge. It creates or amends that task's DAG.
Existing `factory_conductor`, `task conductor`, `conductor_model`,
`SwarmConductorCall`, and `conductor_<n>` names are legacy names for this
Planner boundary. They are retained to keep durable and code identities stable,
not because the Planner is the operator-facing Conductor.

The **Executor** is the existing graph engine and dispatch path. It runs
accepted DAG nodes for the existing `implement_`, `investigate_`, `review_`,
`refine_`, `integrate_`, and legacy `conductor_` role prefixes. Those workloads
keep their existing principals, profiles, and server-enforced authorization.
Conductor, Planner, Executor, and node role names describe responsibility only.
No role name grants permission.

The broader #5784 direction retains the Conductor's goal priority order:
platform stability, useful product progress, then efficient quota use. It also
retains the ask-first boundary for charter or quota-policy changes,
security-relevant changes, anything needing a new or amended decision record,
and irreversible or production-impacting actions outside the allowed GitOps
operations. No clause authorizes a cluster write that the GitOps invariant
forbids.

### Authoritative records and context

The roles use the existing owners rather than a second scheduler or approval
ledger:

- `swarm.work_item` and its append-only `swarm.work_item_event` history record
  the durable unit of requested work and its source-owned state. A
  `swarm.factory_receipt` captures submission, queue/admission state, its work
  item and issue identity, and the admitted `swarm_task` link.
- Queue order is the eligible `factory_receipt` order `(created_at, id)` after
  lane, policy, active-work, and blocker checks in `admit_next`. There is no
  separate priority field for the Conductor to rewrite. Priority rationale may
  be retained as context, but changing live order must use an existing
  authorized task or control operation. KG text is never queue state.
- `swarm.swarm_task`, `swarm.swarm_plan_version`, `swarm.swarm_plan_node`, and
  `swarm.swarm_node_run` are the task, accepted DAG history, nodes, and execution
  evidence. The Planner proposes graph edits and the server records accepted
  edits; the Executor dispatches only recorded nodes.
- A receipt's `escalation_json` is the current pending or resolved factory
  decision, and `direction_json` is the approved direction supplied to a
  re-admitted Planner. The append-only `swarm.factory_audit` rows retain
  decision claims, requests, results, and control acknowledgements. A model
  suggestion is not an approved decision.
- The singleton `swarm.factory_control` row is the authoritative factory state,
  policy, and version. Per-task pause and cancellation state remains on the
  `factory_receipt`. Every mutation revalidates current records and authority.

Issue #5787 owns maintenance and scoped retrieval of Conductor knowledge.
Committed operator exchanges enter the existing KG ingestion and extraction
path as attributed, unverified evidence. `factory_context` retrieves
repository-scoped notes with provenance, freshness, verification, and dispute
metadata while continuing to return factory state during a KG outage. Issue
#5788 exposes that same contract through factory MCP. Issue #5849 supplies the
task-relevant subset to the Planner, with current task and decision state read
from the authoritative factory records above.

Keep three inputs visibly distinct. Operator instructions are attributed task
requests or steering applied through an authorized interface. Approved
decisions are resolved receipt decisions and their audit evidence. Retrieved
KG claims are cited context, not instructions, approval, or permission. A stale
summary cannot reopen a completed task, reorder the queue, or restore revoked
authority.

### Submit, plan, execute, report

A concrete flow uses the records and interfaces that exist today:

1. The Conductor submits an approved open issue through `factory_submit_issue`.
   The server returns a durable `factory_receipt`; a retry with the same
   identity returns the same receipt.
2. Admission selects the oldest eligible unblocked receipt in its available
   lane and creates its `swarm_task`. The Astra Planner records a complete DAG
   through plan versions and nodes within the receipt's pinned policy and
   allowance.
3. The Executor dispatches ready nodes under their existing workload identities
   and records attempts in `swarm_node_run`. Required CI and an independent
   exact-head review remain delivery evidence; a successful node alone is not
   accepted delivery.
4. The Conductor reports receipt and task state, blockers, accepted outcome,
   evidence links, starts, and cost. It labels missing or stale coverage rather
   than converting it into success or an empty queue.

For steering, suppose the Planner pauses a task with a pending decision. The
Conductor presents that exact receipt and decision identity. An operator uses
`factory_decide` to select an option or `factory_request_brief` to provide
bounded direction. The decision owner records the request and result, stores
approved direction on the receipt, and, when the selected operation permits,
returns it to `queued`. Normal admission order still applies, and the next
Planner round receives `operator_direction`. This steers a pending task without
editing KG text, silently changing policy, or inventing a priority mutation.

### Operating rules

The practical MVP decision in #5956 retains four rules for all of these roles:

1. Do not add frozen packets, hash pinning, or timed approval windows. A PR is
   accepted by required CI plus one independent exact-head review, as for a
   human PR.
2. Report one line per task: outcome, blocker, next action, starts, and USD
   used. Do not narrate what did not happen.
3. Cost per accepted PR is the metric. Record it on the PR when it lands.
4. Planner turns share the task's start budget with implementation. If planning
   consumes more than half of the starts, stop the task and respec it by hand.

## Availability and operator policy

The chart's `swarm.factoryEnabled` defaults to false. It makes the reconciler
available, while a separate durable operator transition enables admission.
The factory control migration starts disabled. The HTTP control surface under
`/api/swarm/factory` requires a verified standing human principal in the
`operators` group. Forwarded email headers and agent credentials do not grant
operator authority.

Configure an explicit policy, then enable it through `/control`. All policy
fields are required. An example for one approved issue is:

```json
{
  "repo": "jomcgi-org/homelab",
  "issue_numbers": [1234],
  "generation": 0,
  "max_tasks": {"delivery": 1, "advisory": 1},
  "max_task_turns_hard": 40,
  "max_parallel_nodes": 1,
  "task_budget_usd": 60,
  "turn_budget_usd": 5,
  "allowed_models": ["opus", "luna"],
  "conductor_model": "opus",
  "worker_model": "luna",
  "base_branch": "main",
  "turn_timeout_seconds": 900,
  "task_timeout_seconds": 14400,
  "max_attempts": 2,
  "max_review_rounds": 2
}
```

The plan sizes the task and policy keeps the envelope. `max_task_turns_hard`
and `task_budget_usd` are what an accepted plan has to fit inside; the number of
delivery starts a task actually gets is derived from the plan the conductor
accepted. `max_turns_per_task` was the old fixed cap and is still accepted: a
policy that carries it and no `max_task_turns_hard` reads it as the envelope, so
a live policy needs no re-post. A policy must carry one of the two.

`max_parallel_nodes` is how many nodes one task may hold in flight at once and
defaults to 1, so a policy written before fan-out existed stays serial until an
operator raises it. Conductor planning rounds are capped separately by the
optional `max_planner_turns`, which inherits the envelope when it is omitted.
Planning rounds still draw on `task_budget_usd`. The optional
`max_review_rounds` bounds the review correction rounds the engine runs on its
own and defaults to 2. Those, `reviewer_model`, `model_pools`, `quota_guard`,
and `intake` are optional. `max_review_recovery_rounds` is also optional and
defaults to 0; it admits up to two extra rounds under the CI gate below.

### Updating policy while work runs

`configure` accepts a new policy while tasks are admitted or uncertain. Each
running task keeps its pinned policy, dispatch history, budget and deadline;
only future admissions take the new policy. While tasks are active, a changed
policy must supply a generation newer than the current control policy. An
identical retry is accepted without another generation change. An accepted
generation advance cancels queued and escalated receipts from older
generations in the same control-row transaction that publishes the new policy.
Each retirement is audited with both generations and a reason. The receipt is
never re-stamped, and admitted or uncertain work keeps running under its pinned
old policy. Unrelated terminal history is left unchanged.

An unresolved escalation on a retired receipt is dismissed locally in that
same transaction, with a resolution explaining the generation change. The
paced escalation reconciler repairs preexisting old-generation queues and
cards in bounded groups of 50, including cards attached to terminal receipts;
it resolves those cards without rewriting their terminal receipt state or
touching GitHub. Repeated passes are idempotent. A claimed decision temporarily
refuses the generation change, so policy retirement cannot race an external
issue mutation. Conversely, a decision request whose receipt no longer matches
the policy generation is refused before labels, comments, child issues, or
issue state can change.

Configuration preserves control state: enabled work continues, paused
admissions stay paused, and initial configuration still needs `enable`.
`stop` remains irreversible. The configure audit records
`active_tasks_on_previous_policy` so the transition accounts for work left
running under its old pins.

Active tasks from every generation count against lane and chart capacity,
including on the board. Lowering a limit waits for that work to finish rather
than stopping it. Admission cannot start another task on the same repository
and issue while an older task is active, even in another lane; autonomous
intake skips such issues with `active_issue` evidence. Other eligible queued
work can still fill the available capacity.

### Two lanes

A task runs in one of two lanes and its lane follows its class, which is not
configurable. Work that ends in a pull request is delivery: `bug-fix`,
`mechanical-refactor`, `docs`, `judgment-analysis`. Work that ends in a comment
is advisory: `refine`, `advisory-diagnosis`, `advisory-triage`.

`max_tasks` bounds each lane on its own:

```json
{"max_tasks": {"delivery": 1, "advisory": 2}}
```

A bare integer is still accepted and reads as the delivery lane, which is the
capacity it always asked for, so a live policy needs no re-post. In that shape
the advisory lane is 0 and no advisory task is admitted at all: **an operator
running `refine` has to post a lane map to keep it running.** The advisory lane
is opt-in on purpose, because the number that used to bound it was bounding
delivery too.

Tasks in flight also draw on the shared background session pool, which the
drainers and the synthetic probes draw on too, so
`swarm.factoryBackgroundReserve` (default 2) is the number of slots the factory
will not take. The factory is the only member of that pool that can wait for
nothing: a node the pool declines stays ready and starts on a later tick, so the
factory is the one that yields. Every node is gated on it, the first node of a
settled graph included.

`swarm.factoryMaxConcurrentTasks` bounds the sum, and **it must be set at least
as high as delivery plus advisory.** When it covers their sum, each lane simply
has its own maximum. Below their sum the lanes contend for it, and the free part
of the ceiling is dealt one slot at a time to whichever lane is furthest from
its own maximum, with delivery taking the first slot so it is never left unable
to start anything. The sweep audits `lane_ceiling_below_lanes` with the ceiling,
both maxima and their sum, at most once an hour, so an operator sees which of
the two numbers is actually binding.

Reserving delivery's whole maximum first, as the limits used to, is what starved
advisory work: at a ceiling of 4 with lanes 4 and 8 on 2026-09-11 the advisory
lane had zero room and one sweep excluded 272 candidates as `lane_full`. The
chart ceiling is now 12, which covers the 4 and 8 the lane runs today, and the
split is the safety net for a ceiling somebody sets low. At a ceiling of 1 the
advisory lane still cannot open at all. Note that `admit_next` takes the oldest
queued receipt across the lanes that have room, so under a contended ceiling a
backlog of older delivery receipts can still take most of it; raising the
ceiling, not the split, is the fix for that.

Admission and intake both work per lane. A full delivery lane no longer refuses
an advisory admission, and intake ranks candidates exactly as it did, delivery
before refine, but fills at most one candidate per lane per tick rather than
one candidate in total. A candidate whose lane is full is counted as
`lane_full` in the idle audit. The board shows each lane as used against limit.

### Reviewer fallback while the Claude window is spent

Delivery review prefers Opus on the shared Claude subscription, and that
window is one input the factory can exhaust:
implementation autoscales and the cheap implementers bill elsewhere. The answer
is to review on a cheaper model, not to stop delivering. The optional
`quota_guard` block sets the thresholds:

```json
{"quota_guard": {"claude_7d_pause_percent": 85, "claude_7d_resume_percent": 75}}
```

Both default to those numbers, so a policy that predates the block still routes.
Both are whole percentages between 1 and 100, and the resume threshold must be
strictly below the pause threshold: equal thresholds are a flap, not a guard.
Naming only one is allowed as long as the pair stays ordered.

At or above the pause percent, review nodes run on the next member of
`model_pools.reviewer` that has provider quota, default `["opus", "astra"]`.
Opus comes back on its own once the window falls below the resume percent.
**Nothing else changes.** Delivery admission is never held, implement nodes on
Sol or Muse run exactly as they did, and the advisory lane is untouched.

Reviewer independence is a property of the session a review runs in, never of
the model it runs on. A fallback reviewer still runs in its own session and is
still refused if that session is the implementer's, which is the check
`verify_delivery` has always made. Delivery evidence records the model that
approved, so an accepted PR says who reviewed it rather than leaving it to be
inferred from the date. That model is read from the immutable dispatch pin, not
from the review artifact, because the artifact is written by the agent and
cannot be authority on its own identity.

The model is chosen when the review is dispatched, not when it was planned. A
plan written while the window was quiet can reach its review hours later, so
the node keeps the model the planner asked for and the pin records what really
ran. A retry after the window moved is a new attempt and takes a new pin.

The planner uses that dispatch evidence too. A permitted fallback does not
require another review merely to obtain Opus provenance. The factory approval
is the successful independent review run's structured artifact at the current
PR head. A posted GitHub approval is a separate requirement only when an actual
repository rule or explicit task acceptance requires it. Explicit model-specific
acceptance criteria and the judgment capability floor still apply.

Two things never fall back:

- **Judgment work waits.** `judgment-analysis` has a capability floor rather
  than a price, so its review runs on Opus or waits for it.
- **An empty pool waits.** When no member has quota, review waits rather than
  falling further. Review is the gate, and a gate that lets itself be skipped
  is not one. Astra and Sol share one Codex grant, which is why the default
  pool stops at Astra: a Sol rung could never be reachable when Astra is
  walled, and offering it would make the fallback look deeper than it is.

A waiting review is not a paused task. The work waits where it stands and
starts on the tick a reviewer has quota again, audited `review_waiting` once
per node per window transition with the models it skipped and why.

The routing reads the 7-day window the token broker already observes for the
`claude` provider, once per tick behind a short cache. Transitions are audited
`reviewer_fallback` and `reviewer_restored`; changed quota readings and a
five-minute evidence refresh also update an existing routing choice. The board renders
the state from that ledger without a broker call. An unknown reading, or one
older than an hour, never starts a fallback: it audits `quota_guard_unknown` at
most hourly during a continuing outage, records each new outage after recovery,
and review stays where it is, because downgrading every review
whenever a broker read fails would turn one outage into two. It does not end a
fallback either, so a fallback entered at 95 percent does not snap back to Opus
the moment the broker goes down. A known weekly reset releases that window's
high-usage latch even if the broker subsequently loses its observation. The
reset is carried through waiting/fallback transitions. Legacy records without
a known reset remain conservative rather than guessing a reset date.

The board never presents the old decision's percentage as current after a newer
unknown observation or more than an hour of age. It names retained fallback
routing with unavailable quota instead. A current percentage is labelled as
usage, separately from the fallback threshold.

### Model pools

`model_pools` carries up to four ordered preference lists, and `select_model`
takes the first member whose provider still has quota.

| Pool | Routes | Default |
|---|---|---|
| `conductor` | planner rounds | the `conductor_model` alone |
| `worker` | every other planner-added role | the `worker_model` alone |
| `implement` | implement nodes, engine corrections, engine fan-in | the `worker` pool |
| `refine` | the advisory briefing node | `["spark", "sol"]`, narrowed to allowed models |
| `reviewer` | every review node, planner-added and engine-inserted | `["opus", "astra"]`, narrowed to allowed models, led by `reviewer_model` |

A role pool must start with that role's own configured model, so the policy's
stated preference is what the pool is ranked from. A class pool, `implement`,
`refine` and `reviewer`, names no policy field and so has no head to anchor to; its order
is the preference. Every member of every pool must appear in `allowed_models`.

The `refine` default only applies to what a policy allows: a policy that allows
neither `spark` nor `sol` falls back to the conductor pool, which is where
refine ran before class pools existed. `spark` needs no separate guest profile,
because the Muse family lands on the same `claude-runtime` session workload as
Claude and Codex; its plaintext egress lane and endpoint transport pin are
guest-side and already shipped (#5978).

The judgment floor searches the `implement` pool first, then the conductor
pool, then the conductor model, so judgment work keeps its Opus-class floor
whichever pool routes the rest. The floor ignores quota on purpose: a
quota-walled Opus holds judgment work rather than demoting it.

The `reviewer` pool leads with the policy's own `reviewer_model` when that is
not already in it, so a policy naming one reviewer and no pool keeps exactly
the reviewer it asked for and gains a fallback behind it. A planner may name a
review model only from this pool; anything else is refused as
`reviewer_model_mismatch`.

### Shared session admission

Factory nodes draw execution permits from the same pool as the work-queue
drainer and the synthetic probes. The bounds are chart values under
`agentSessions.admission`: `total` over every tier, `background` over the
non-interactive tiers under it, and `kg` under that. The code defaults are
4/3/2 and the chart sets 16/12/2. A narrower `total` clamps the two inner
numbers.

`total` must stay at or below what EmberVM will actually create for the session
workload: `claudeRuntimeWorkload` concurrency `cap` in
`projects/embervm/deploy/values.yaml`, 16, with `session.maxSessions` at 24. It
is `total` that has to fit rather than `background`, because every model family
except pi lands on `claude-runtime`, so an interactive permit reaches the same
workload. Above the cap a granted permit meets a `workload_cap` denial at guest
create, which fails the turn rather than making it wait.

### Autonomous intake and refine

The optional `intake` block is fully defaulted when an older policy does not
carry it:

```json
{
  "enabled": false,
  "labels": ["agent-ready"],
  "exclude_labels": ["needs-human", "wontfix", "security-finding"],
  "max_per_day": 5,
  "cooldown_hours": 24,
  "refine_enabled": false
}
```

`auto_merge` is a separate top-level policy field rather than part of this
block, because it governs what the lane does after a delivery is approved
rather than what it takes in. It is optional and defaults to `false`.

`enabled` controls autonomous issue discovery. `labels` names delivery-ready
labels and may be empty, in which case no issue is a delivery candidate.
`exclude_labels` rejects an issue before selection. `max_per_day` counts
delivery admissions only and is between 1 and 10000, and `cooldown_hours` is
between 1 and 168. `refine_enabled` allows an
otherwise eligible issue with none of the delivery labels to enter the refine
path. `close_enabled` allows a refine verdict to close an issue and
`max_closes_per_day` bounds how many it may close in a rolling 24 hours,
default 3. All three feature flags default off or, for the cap, bound a
capability that is itself off.

An open `blocks` edge holds the blocked work item's receipt out of both the
autonomous candidate sweep and final admission. The sweep counts the exclusion
as `blocked`, and final admission records one `admission_blocked` audit for the
first queued receipt when it is blocked. Closing the blocking work item
releases the receipt on the next tick without operator action.

Block edges also derive from issue bodies. During each intake sweep, after
synchronizing GitHub issues as work items, the factory parses issue body text
for blocking phrases: "blocked by #N", "depends on #N", "waits on #N" all mean
that issue N blocks this one, and "blocks #N" means this issue blocks N.
All phrases are case-insensitive and accept both "#N" and "owner/repo#N"
formats, where "owner/repo#N" is only recognized if the repository matches the
current repo. Matches inside fenced code blocks are ignored, and a bare "#N"
without a phrase is never treated as a link. The factory reconciles these
derived edges on every sweep: hourly while idle, immediately after a receipt
settles, and immediately after an admission. It adds edges for newly mentioned
issues and removes edges for issues no longer mentioned in the body. Manual and
decision-authored edges are never touched; only edges with source "github_body"
are updated. Block cycles in body edges produce a throttled
`work_item_edge_cycle` audit but do not prevent intake from proceeding.

Intake sweeps GitHub at most once an hour while it is finding nothing, again
immediately after any receipt settles, and again on the tick after it admits
anything. That last clause is what lets a lane fill: a sweep takes at most one
candidate per lane, so on the hourly clock alone four delivery and eight
advisory slots filled at one slot an hour. It costs at most one extra sweep per
admission, because the sweep stamps its own clock before reading GitHub, so a
sweep that admits nothing leaves the newest admission behind the newest sweep
and the hourly clock governs again. A tick with no room in any lane returns
before it reaches the clock at all. A tick that cannot read GitHub audits
`intake_error` rather than failing silently. The sweep reads open issues
and open pull requests oldest first, up to five pages of one hundred each; a
read that hits that cap records `truncated` on the audit, so a partial sweep
reads as partial.

Intake admits at most one issue per lane per tick, and never opens more than
`max_per_day` deliveries over a rolling 24 hours. The cap bounds delivery
churn, which is pull requests and the Opus reviews they cost. Advisory
admissions are uncounted: a refine writes a comment for cents and buys no
review, so letting one consume the cap would throttle a burn-down for nothing.
A capped tick refuses the delivery candidate and audits `daily_cap` once, and
still admits an advisory candidate on the same tick. The board reads
`admitted_today` the same way, as deliveries. A failed or cancelled receipt
cannot be selected again until
its cooldown expires, whatever class it settled under. Assigned issues,
excluded labels, issues linked from an open pull request, issues already
delivered, and issues already received in the current generation under the same
task class are not candidates.

A refine candidate carrying `needs-thought` is excluded as `deferred`, so an
autonomous `defer` verdict does not brief the same issue again after its
cooldown. A delivery candidate is still admitted when it also carries
`needs-thought`: an include label such as `agent-ready` determines that path
before the deferred exclusion is applied.

An issue with a `succeeded` receipt in any generation and any class is excluded
as `delivered` and is never admitted again. The lane's own labels are not
evidence that work is outstanding: #3877 shipped as PR #6007, the pull request
body carried no closing keyword, the issue stayed open with `agent-ready`
intact, and the next generation's sweep read it as fresh work. The exclusion is
unconditional, including for an issue a human reopened. GitHub's issue listing
carries no reopen timestamp and `updated_at` moves on every comment, label and
edit, so it cannot tell a reopen from a comment, and establishing the difference
would cost one events read per candidate on a request budget the whole lane
shares. An operator who wants a delivered issue worked again names it in the
policy `issue_numbers` allowlist under a new generation, which writes the
receipt directly and never consults this sweep. Issues closed on GitHub are
excluded as `not_open`, which the open-issues listing already implies and
selection asserts independently. Scoping that last one to the class is what lets an issue a refine
pass moved to `agent-ready` be delivered in the same generation, with no
operator bumping `generation` to release work the lane itself made ready. The
receipt's identity carries the class for the same reason. Intake-created receipts become admissible only while
`intake.enabled` is true. Turning intake off therefore leaves the operator's
`issue_numbers` allowlist exactly as it was.

Operator-posted and allowlisted delivery receipts take a separate linked-PR
path. Before the receipt is written, the server performs the same bounded open
PR discovery used at first reconciliation. A same-repository `factory/` PR
that closes the issue is stored immediately as the receipt's delivery target,
so an operator repost or a later readmission continues that branch. A person's
branch or a branch held by another running task is never granted. Failed or
truncated discovery fails closed instead of creating an unbound receipt that
could open a competing PR. Ordinary autonomous discovery keeps the
`linked_pr` exclusion because it has no operator repost to authorize a new
receipt.

Delivery candidates rank before every refine candidate. Within either group,
`critical` ranks before `bug`, then the oldest issue ranks first. A refine
candidate carrying `critical` never outranks a delivery candidate without a
rank label.

Local-authority work items are a second intake source alongside GitHub. State
`ready` makes a local item a delivery candidate, while state `open` makes it a
refine candidate when `refine_enabled` is true. The same label, receipt,
cooldown, blocker, lane, and one-per-lane exclusions apply to both sources.
Local and GitHub candidates rank together by priority and age.

The `not_open`, `assigned`, `linked_pr`, and `pull_request` exclusions do not
apply to local items. The item is ours, and intake does not read back a human
closing or assigning its migrated issue on GitHub. A local item's labels are
ours too and never resync from GitHub.

Each intake receipt carries the task class that sets its verification mode,
implementer floor, and gate (ADR agents/038 decision 5):

| Class | Verification | Floor | Gate |
|---|---|---|---|
| `bug-fix`, `mechanical-refactor`, `docs` | machine-verified | the worker pool | independent review from the pinned reviewer pool plus required CI |
| `advisory-diagnosis`, `advisory-triage`, `refine` | advisory | the worker pool | none, because nothing merges |
| `judgment-analysis` | judgment | Opus or better | independent Opus review plus a human spot check |

Both intake paths classify. An issue an operator names in `issue_numbers` is
read for its labels exactly as a discovered one is, so the judgment floor
applies whether the lane found the work or was told to do it. Classes come from
the issue's labels in this order:
`security-finding` and `needs-thought` select `judgment-analysis`, `bug` selects
`bug-fix`, `documentation` selects `docs`, and `todo` selects
`mechanical-refactor`. `security-finding` remains excluded by default. An
unclassified `agent-ready` issue defaults to `bug-fix`. A receipt written
before classes existed also reads as `bug-fix`, so operator-posted receipts are
unaffected.

The judgment floor is a capability constraint. A quota-walled Opus holds
judgment work instead of demoting it. The floor searches the implement pool
first, then the conductor pool, and falls back to the conductor model when
neither names an Opus-class member, so a policy whose pools carry no such model routes
judgment work to its strongest configured model and says so in the node's
stated reason. Configure an Opus-class worker pool member before enabling
intake on a repository whose issues carry `needs-thought`. Advisory classes other than `refine` have
no producer yet and park the task paused; phase 3 fills that hook.
Reviewer-driven class escalation and the verdict ledger are deliberately not
built in these phases (#3843).

A refine task runs one planner-class node with at most two attempts and has no
DAG. It researches before briefing: the knowledge graph first, then the
checkout's ARCHITECTURE.md `**Why.**` paragraphs, file history and referenced
pull requests, then the web only for a named external product, version or CVE.
The brief cites those sources under `### Evidence`, and after the verdict the
node reports its issue number, verdict and one-sentence reason back to the
knowledge graph on a best-effort basis. The node then posts exactly one
`## Agent brief` comment with `### Outcome`,
`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that order,
then reaches exactly one of five verdicts and acts on it:

| Verdict | What the node does | What settlement demands back from GitHub |
|---|---|---|
| `agent-ready` | applies `agent-ready` | the label, the brief, and the issue still open |
| `defer` | adds `### Why defer` naming the concrete condition that would make the issue actionable, applies `needs-thought`, and leaves the issue open | the label, the brief, the issue still open, and the condition in the artifact evidence |
| `needs-human` | applies `needs-human`, ends the brief with `### Decision needed` carrying `recommend: deliver \| close \| split` and the one quick-unblock question a person must answer | the label, the brief, the issue still open, and one warn notification naming the issue, the recommendation and the question |
| `reject` | adds `### Why not` citing a file, pull request or recorded decision, applies `wontfix`, closes with reason `not_planned` | the label, the brief, and the issue closed |
| `stale` | adds `### Why stale`, applies `stale`, creating the label if the repository has none, closes with reason `not_planned` | the label, the brief, and the issue closed |

`reject` and `stale` are for a premise a reader can check: a decision recorded
in an ARCHITECTURE.md **Why.** paragraph, work already merged, a file or flag
that is gone. `needs-human` is for a decision a person can make in about a
minute and that unblocks the work, such as which of two scopes, whether the
work is still wanted, or confirming that #z supersedes this. A question that
needs real thought, a design, or a window only the author can declare is the
`defer` verdict, taken by the node itself.

Closing is gated twice. It needs `close_enabled`, and it needs room under
`max_closes_per_day`, counted from the `intake_closed` audits of the last 24
hours. Both are read before the node runs, so the prompt offers three verdicts
rather than five when closing is unavailable, and again at settlement, so a cap
spent while the node was running still holds. A close verdict the lane may not
act on is downgraded to `needs-human`: the server then demands the
`needs-human` label and an open issue, exactly as it would for an escalation,
and records `refine_close_downgraded`. An issue carrying `critical` or
`security-finding`, or assigned to any milestone, is never closed and takes the
same downgrade. Every close that settles audits `intake_closed` with the
evidence cited.

A `reject` that finds the work belongs in another issue can list up to ten
other open issues in `supersedes`, but doing so also requires the surviving
issue in `in_favour_of`. The server refuses the relationship unless that
favoured issue is open, is not a pull request, and is neither in `supersedes`
nor the issue the node briefed. The node closes only the issue it briefed.
After verifying that primary close, the server re-reads each sibling, comments
with an idempotency marker, applies `wontfix`, closes it as `not_planned`, and
writes its own `intake_closed` audit. It skips and audits a sibling that is a
pull request, already closed, protected by a label or milestone, assigned,
represented by an active or escalated receipt, or beyond the daily close cap.
The marker proves only that the comment landed. On a retry the server re-reads
the sibling: if it is closed, it fences in the missing audit; if it is still
open, it repeats every other guard, then retries the label and close without a
second comment. One sibling failure is also audited and does not fail
settlement of the verified brief. If an operator later chooses `supersede` and
the receipt's own issue is the survivor, the server re-queues that receipt for
a fresh brief that includes the folded-in scope.

The server settles from a re-read of the issue, never from the node artifact
alone. A verified `needs-human` result succeeds because the briefing and the
escalation both completed, and a verified close succeeds because the issue is
demonstrably closed with its reason on record. A mismatch fails the task and
the issue takes the cooldown. Two failed attempts settle the task failed and
apply nothing. So does any other limit that leaves the node unable to run:
advisory work has one node and no planner to fall back on, so a tick that finds
no attempt in flight, no success, and the node absent from the conductor's
ready set settles `refine_failed` there and then, naming the limit it hit.
Idling instead held the advisory slot and its reservation for ever (#6045).

### Exact-event problem issues

The optional top-level `problem_issues` policy block turns three demonstrated
factory failure signals into ordinary GitHub issues: `node_stalled`,
`workflow_stranded`, and terminal `landing_recovery_exhausted`. It does not
create a receipt, post an intake event, or admit a task. Generated issues can
enter the factory only when the existing GitHub discovery and refine/intake
policy later selects them.

The block and every source switch default false. Its repository defaults are:

```json
{
  "problem_issues": {
    "enabled": false,
    "sources": {
      "node_stalled": false,
      "workflow_stranded": false,
      "landing_recovery_exhausted": false
    },
    "source_audit_limit": 50,
    "issue_pages": 2,
    "issues_per_page": 100,
    "max_per_tick": 1,
    "max_per_24_hours": 3,
    "labels": ["bug"],
    "retry_minutes": [2, 4, 8, 16, 32, 60]
  }
}
```

The first tick after the top-level switch or an individual source is enabled
records `problem_issue_policy_observed` with a watermark at the newest matching
source audit. History from before enablement is never replayed. Each later tick
scans at most 50 exact source audits and creates at most one issue. The rolling
24-hour cap counts durable `problem_issue_write_started` intents, including an
ambiguous write, so uncertainty cannot buy extra external writes.

Every body links the delivery issue, links the pull request when the signal is
about landing, names the factory task and source audit, and contains an exact
`factory-problem` fingerprint marker. The receipt's repository owns discovery,
links and creation even if later global policy names another repository.
Discovery inspects at most two pages of 100 newest open or closed issues for
that marker. If both pages are full, the producer audits the bounded scan and
continues from its durable pre-write ledger; otherwise a repository with more
than 200 issues could never produce a new issue. Pull requests returned by the
GitHub issues API never satisfy an issue marker. A repeated or replayed source
event reconciles the existing issue. Fingerprint lookups use the audit trail's
action index and database predicates rather than loading producer history. A
missing source receipt, malformed event, discovery failure, scan cap, daily
cap, or rejected GitHub write is audited and visible on the factory board.

The producer records `problem_issue_write_started` before the GitHub request.
A pending intent retains its original repository even if later policy points at
a different repository. A definite client refusal is terminal. A timeout, rate
limit (including a rate-limit `403`), transport loss, server error, oversized
response, or response that does not confirm the marker is ambiguous. Response
bytes are streamed into the documented bounded buffer. Disabling the producer
stops new writes but lets these read-only reconciliations finish. The lane
does not repeat that create request. It performs six marker reconciliation
reads after 2, 4, 8, 16, 32, and 60 minutes, recording
`problem_issue_write_uncertain`, `problem_issue_reconcile_retry`, and finally
either `problem_issue_reconciled` or `problem_issue_unresolved`. This makes a
crash immediately before the request conservative too: it can omit an issue,
but cannot create the same one twice by guessing whether the write happened.

Repository delivery is staged and does not complete #6002 operationally. The
rollout checklist remains:

- Deploy with `problem_issues` omitted or disabled. Confirm there are no issue
  writes and the board reports the producer off without affecting factory
  reconciliation.
- Enable one source in a staged window and replay one safe known audit. Confirm
  exactly one issue has only the `bug` label, the exact marker and source links,
  with no receipt or task directly admitted.
- Repeat the event across a reconciler restart and an uncertain-write drill.
  Confirm marker reconciliation, visible retry/backoff audits, no duplicate,
  the one-per-tick cap and the three-per-day cap.
- Leave intake/refine policy unchanged, observe the generated issue using only
  normal GitHub discovery, then disable the source and confirm writes stop.
- Validate phase 4 chart write-back and live rollout separately before any
  future change claims the parent programme complete.

### Escalation decisions

A `needs-human` verdict is a decision waiting on a person, so it arrives as
options rather than as a question. The artifact carries two to four, ordered
with the recommendation first, and the server refuses the settlement when they
do not hold up:

```json
{
  "key": "split",
  "label": "Split the operator console out of the API",
  "effect": "split",
  "detail": {"children": [{"title": "...", "body": "..."}]}
}
```

`effect` is one of six. `agent-ready` applies the delivery label and takes
`needs-human` off, with an optional scope note posted as a comment. `close`
comments the reason and closes with `not_planned` or `completed`. `split`
opens one to five child issues, then closes the parent against them. `defer`
applies `needs-thought`, takes `needs-human` off, and comments the condition
that would make the work worth doing. `hold` writes nothing and records that
someone looked and chose to leave it. `supersede` closes one to ten named
issues as `wontfix` in favour of one surviving issue. The receipt's own issue
must be among the closes or be the survivor.

Each child created by `split` carries a `parent` edge from the split issue's
work item.

The escalation context document presents five sections: the ask, what stopped,
what happened, cost, and lineage. Each section's `line` is a claim, and the
fields beneath it are the evidence for that claim. The private read endpoint is
`GET /api/agents/factory/escalations/{receipt_id}/context`. It reads only the
factory database and performs no GitHub reads.

The private card renders the five context lines above its options, and pressing
`e` expands their evidence.

The first option is the recommendation, and its effect has to be the one the
`recommend:` line names: deliver is `agent-ready`, close is `close` or
`supersede`, and split is `split`. `defer` remains available as an alternative
but cannot be recommended, because a node that reaches that conclusion takes
the autonomous verdict. The prompt asks for labels that name the concrete
act ("Close as superseded by #5656") rather than the verb the effect already
carries, because the label is the whole of what a person reads before
deciding. The same options are the numbered list in the brief's
`### Decision needed` section, so a reader on GitHub and an operator on the
console are choosing from one list.

A close verdict the server downgrades never wrote options: the node reached a
verdict it was allowed to act on and the server is what turned it into an
escalation. Settlement synthesises two for it, the close the node wanted and
`hold`, so every escalation is decidable rather than leaving the downgraded
ones as a question with no buttons under it.

The escalation is stored on the receipt as `escalation_json`, which is also
where the resolution lands, and the options are repeated on the
`refine_settled` audit as what was offered at the time.

**The escape group.** Every unresolved escalation also carries three fixed
options the board view appends rather than a brief writing them, so an
operator who agrees with none of the offered options can leave the card
without a chat round trip and the wait for another brief. `escape:close`
closes the issue as `not_planned` and takes `needs-human` off. `escape:defer`
swaps `needs-human` for `needs-thought`. `escape:dismiss` resolves the
escalation on the factory side with no GitHub write at all, so the card leaves
the list while the issue keeps `needs-human` and intake goes on skipping it.
All three record the operator's note on the resolution, and the first two post
it as the comment.

The dismiss is the one resolution that is not final, and it has to be, because
it is one keypress with no confirmation. Because it wrote nothing, the issue
is still exactly what it was, so a later decision, a chat request, and a fresh
brief are all still accepted on a dismissed escalation: the claim it holds is
superseded the way a failed one is, `_resolve` overwrites it rather than
returning it, and settlement replaces the whole document so the card comes
back carrying the answer. Every other resolution stays final. This is a
property of the record, not of the page, and the page has no control that
un-dismisses a card: the way back is a chat request or another decision
through `POST /api/agents/factory/decisions/{receipt_id}`. The keys carry a colon, which the option key pattern
forbids, so a brief can never author one that collides; the brief's own
options keep the numbers 1 to 4 and the escapes answer to `x`, `d` and `Esc`.

`escape:close` takes its close reason from the option's `detail`, the same way
the brief's own `close` does, so the two read one field rather than one of
them hardcoding `not_planned` beside a `detail` nothing consulted. It is never
weighed against the protected-label rule that downgrades a node's own close on
a `critical` or `security-finding` issue.
That rule exists so a node does not close one of those unwatched, and the
operator clicking here is the authority it was deferring to. It is the one
escape the page confirms before sending, because it is the one no other button
on the card undoes.

The close comments, closes, then drops the label, in that order. A failure
between the close and the label leaves a closed issue still carrying
`needs-human`, which nothing acts on. The other order leaves an open issue
with the label gone, which is exactly the state that puts it back in front of
intake.

**Deciding.** `POST /api/swarm/factory/decisions/{receipt_id}` behind the same
operator gate as `/control`, with `{"option_key": "...", "note": "..."}` or
`{"action": "chat", "note": "..."}`. The private factory page at
`/factory/escalations` reaches the same code through
`POST /api/agents/factory/decisions/{receipt_id}`, which the browser can use
because it is gated on `X-Auth-Email`, the address Envoy projected from the
verified Access JWT and the gateway strips on ingress so it cannot be
smuggled. Not `Cf-Access-Authenticated-User-Email`: nothing in the cluster
validates or strips that one, so a caller reaching the backend can set it to
any address. A request carrying no projected address, two of them, or only the
Cloudflare header is refused.

Cloudflare Access is the gate. `private.jomcgi.dev` is zero trust locked to one
identity, so an address arriving on the projected header was already authorised
to be there and a second list would only restate that.
`FACTORY_OPERATOR_EMAILS` is empty by default and any single verified identity
decides. Set it only to narrow that: non-empty it is an allowlist, so someone
Access admits who is not on it reads the board and the escalations and gets a
403 on the click.

That list, once it is set, is a secret and not a values entry. Provision it as a
key on the `monolith-chat-secrets` 1Password item and wire it with
`valueFrom.secretKeyRef` the way `GITHUB_API_TOKEN` is wired, rather than naming
operator addresses in a public `deploy/values.yaml`.

A decision is refused while the receipt is `admitted` or `uncertain`, because a
brief running on the issue would keep writing to something the decision has
just closed or relabelled. The page shows that card as `briefing` with its
buttons disabled rather than offering a click the server would refuse. The
escape options are refused on the same terms: the reason is the live node, and
it does not care which option the operator picked.

Every write is idempotent on the pair of receipt and option. A comment carries
a hidden marker naming that pair and is skipped when the marker is already on
the issue, a label add is idempotent already, and each child a split opens is
fenced by its own `decision_child_created` audit row, so a retry after a
network failure finishes the decision rather than doubling it. A decision is
claimed before it is applied, which is what refuses a second, different option
against one escalation; repeating the same option returns the first result.
A claim is superseded by a later `decision_failed` naming the same option, so
one failed GitHub call does not lock the escalation to the option that failed.
Every failure is audited, including the ones an effect raises from inside
itself. Claims and child records are matched on the receipt id carried in the
audit detail rather than on `task_id`, which is null for a receipt waiting on
a re-brief and would otherwise match every other receipt in that state.
Applying audits `decision_applied` with the actor, the option and what it did.

**Chat.** `action: chat` posts the note on the issue prefixed
`Operator asks:`, records it on the escalation, audits
`decision_chat_requested`, and returns the refine receipt to `queued` so the
lane briefs the issue again in the same generation with the note in its
prompt. The receipt rather than a new one, because admission selects on the
policy's generation and a receipt at any other generation would never be
admitted at all. The issue text is never rewritten. This is the one path that
returns a settled receipt to the queue, so total spend per generation is
bounded by the receipts a generation can hold plus the re-briefs an operator
asks for by hand.

The re-brief replaces the escalation document rather than adding to it. The
second brief settles onto the same receipt, so keeping the first one would
leave the page showing options written before the question was answered, and
pressing 1 would apply a stale first option. The chat history carries forward,
because it records what was asked rather than any one brief's answer.

The question is posted whether or not the lane can take the re-brief, so the
response says which happened. `requeued: false` carries `blocked_by` naming
the reason: not a refine receipt, a brief already queued or running, a
issue that is neither in the policy allowlist nor discoverable with intake on.
A generation-stale chat is refused before its issue comment is posted, because
the retirement path owns that card and the old receipt can never be selected by
the current policy.

Deciding after asking for chat is allowed and cancels the re-brief: the
resolution returns a `queued` receipt to `succeeded`, so the lane never spends
an advisory slot briefing an issue that is already closed, split or labelled
for delivery.

The `needs-human` warning on Discord carries the escalations link, so the
notification is the way in rather than a thing to read and then go looking.

Before enabling `refine_enabled`, confirm the guest GitHub token has
`issues: write` on a fine-grained token, or `repo` on a classic one. Opening a
pull request needs `pull_requests: write` or `repo`. A fine-grained token
granted only `pull_requests: write` and `contents: write` can push and open pull
requests but receives 403 on refine comments and labels. That 403 becomes a
failed refine attempt rather than silent success because settlement re-reads
the issue. Set `FACTORY_EXECUTOR_LOGIN` to a non-empty GitHub login to require
the verified brief comment to come from that author. The chart does not set it
today, so that check is inert until an operator does. Settlement still requires
a `## Agent brief` comment posted at or after admission, and reads the comments
written since then rather than only the first page, but without the login it
accepts such a comment from any author.

The derived allowance is `max_attempts` summed over the live nodes that have
not succeeded, plus the work turns already spent, plus what the engine may still
insert on its own. Review rounds are reserved lazily, one round at a time: the
next round only, at the two turns it really costs, because the engine inserts
its correction and its re-review at one attempt each. A correction that fails
costs the round rather than the turn: the engine opens the next round against
the same reviewed head and the same findings, and the failed round still counts
against `max_review_rounds`. One attempt is a deliberate trade: a correction or
re-review that fails or stalls spends a round rather than retrying silently
inside a round nobody sized for it, and the planner is asked once the rounds
are spent.

An operator can set `max_review_recovery_rounds` to 1 or 2 to continue a
review that still requests changes after the ordinary round bound. The default
is 0. Recovery requires an open PR on this task's branch in this repository,
the configured base branch, the exact head recorded by the latest review,
and both successful `pr-checks` and successful aggregate commit status. Draft
PRs can be corrected. The PR identity is read again after CI to detect a head
change during the observation. Missing/pending CI and transient GitHub reads
wait on the same task without a planner turn; red checks or changed PR identity
return to planning. Waiting remains subject to the original task deadline.

Recovery is another engine-owned correction and independent re-review pair,
not a new admission. Its starts and dollars count against the original task
envelope, and its deadline, receipt, branch and history remain unchanged. The
round count comes from the durable graph, so restarting the reconciler does
not replenish it. The engine checks each pair against the remaining envelope
when inserting it, without reserving conditional recovery before it qualifies.
`review_recovery_observed` audits the evidence, and the inserted graph records
the CI head in the correction's reason. Failed corrections do not qualify for
these extra rounds, and `max_review_rounds: 0` still disables all corrections.
Neither agents nor planner decisions can change either server-owned bound.
After recovery is spent, the existing planner/escalation path applies. This
field is pinned at admission along with the policy; enabling it affects future
admissions and does not alter in-flight pins or resolve existing escalations.

Reserving every remaining round up front instead priced a loop the task would
probably never open, and it left a nine-turn envelope unable to hold a
three-node plan at all.
Each round the engine inserts is then counted like any other live node while the
round behind it is reserved, so the allowance grows by one round at a time. A
fan-out wave still reserves its one fan-in node at `max_attempts`, exactly as
the inserted node will carry, so a fan-in is turn-neutral. Review rounds are
reserved only when the plan holds a review node. Its dollar figure is the same
sum over node `max_cost_usd` ceilings plus charged history. It is stored on the receipt as
`allowance_json` with the graph revision it came from, re-derived and audited
whenever an accepted edit changes the graph, and it is the bound work starts
meet.

The reserve is headroom for sizing the planner's next edit, not a charge against
the engine's own. Every insertion is checked against the envelope as it happens,
and an engine insertion, a review round or a fan-in, is checked on the nodes it
really adds with no forward reserve counted on top: otherwise a round the
envelope can afford is refused because of a round that may never open. The
forward reserve is recomputed after the insertion, for the planner's view. An
edit whose derived allowance would exceed the envelope is refused whole with
`envelope_exceeded` and a detail naming needed against allowed for both turns
and dollars, beside `spare_turns` and `spare_usd`, what the envelope would still
fund once the reserve that edit brings with it is counted. The planner reads
that in `decision_feedback` and answers by shrinking the edit to fit whichever
is binding, splitting the work, or pausing; it must not re-propose a refused
edit unchanged. Discarding a node drops its unspent slots and never refunds a
consumed turn.

The example is documentation, not live authorization. Use the selected issue,
current capacity and an explicitly accepted policy for an operating trial.
Admitted tasks retain their policy even if later configuration changes.
The initial worker model uses the proven Luna route. Muse can be selected after
its actual dispatcher and guest route are verified on the deployed stack.

## Execution and evidence

Each graph admission and factory turn reservation commit atomically. The
node's deterministic session identity is reused after an interrupted submit.
A workflow replay cannot read a different graph or silently replenish its
attempt, task-turn, deadline or budget bounds. One failure does not count
against `max_attempts`: a session create the control plane refused with a 429
capacity denial never reached a model, so the run records `capacity_denied`,
audits it against the task, and is excluded from the attempt count by both
`graph.admit_dispatch` and the conductor's readiness. At most three such
denials per node are excluded, after which they count like any other failure,
so a saturated control plane still retires the node. The start ledger records
the same verdict in `factory_start.accounting_basis` and excuses the turn and
the budget an excused denial would otherwise spend. The two ledgers have to
agree: a node the graph keeps ready whose every start the turn gate refuses
pauses the task instead of retrying it. Confirmed failed artifacts feed
bounded retry context back to the next attempt. Unknown execution retains its
reservation and requires reconciliation before another attempt starts.

The DBOS application version is pinned to the node workflow's own source:
`execute_node`, the helpers that decide which steps run, and every step they
call. DBOS otherwise derives it from every registered workflow in the process
and neither recovers nor dequeues anything an older version started, so any
unrelated workflow edit stranded in-flight nodes. Pinned, a deploy that leaves
those functions alone recovers its in-flight nodes natively. The cost is that
another workflow changed in a deploy now keeps its version and is recovered
against its recorded steps, which DBOS refuses loudly with
`DBOSUnexpectedStepError`, and a loud refusal beats the silent PENDING strand
this replaces.

Editing the node workflow or one of its steps is therefore the one deploy that
strands in-flight nodes, and nothing can recover them. The reconciler cancels
such a workflow, audits `workflow_stranded` with both versions, and settles the
attempt as uncertain, after which the node's real session outcome is reconciled
and the node retries within `max_attempts`.

A node whose workflow is PENDING on the current version but whose newest
`dbos.operation_outputs` checkpoint is older than its `turn_timeout_seconds` is
stalled rather than stranded. A healthy node checkpoints every poll and every
sleep, so that gap means the workflow stopped progressing. It is settled exactly
like a stranded one: `node_stalled` audited once per workflow, one Discord
warning, the workflow cancelled, and the attempt settled uncertain. Cancelling
is what makes the workflow terminal so stop supervision can confirm the guest
ceased, and a node that then fails with no retry left reaches the planner
through the ordinary deviation path rather than a planner node queued behind
the stalled run.

Settling either one as uncertain does not start stop supervision on its own.
Supervision runs only once the attempt's stop is due: an operator stop, a
cancellation request, the turn timeout elapsed measured from dispatch, or two
minutes after the session's own turn was recorded as failed. That last term is
what makes the deadline track the guest instead of the policy. A turn timeout
bounds how long a turn may run, so it was the right deadline only for a turn
that could still be running, and supervision never sees one:
`read_uncertain_factory_attempt` refuses the attempt unless its turn is already
terminal with `terminal_reason` `error`, so a live turn is out of scope upstream
and the failure stamp is always there. Waiting out the turn timeout was
therefore waiting for a turn that had already ended. The two-minute grace is
there so the conductor's own native completion check settles the attempt first
where it can. Every term is a minimum, so the grace only ever brings a stop
forward. A strand or
a stall caught early still holds its reservation until one of those deadlines
passes rather than self-healing at the moment it is observed.

Stop supervision also checks the Kubernetes node inventory. When an old parked
or banked guest names a node that is no longer in the cluster, supervision asks
Ember to destroy it and waits for the next observation to prove `destroyed`
before settling the attempt. The destroy carries the observed generation and
consumes one of two durable request slots before the network call. Exhaustion
requires operator intervention. An unavailable inventory never counts as an
empty cluster. If a factory session is still `recovering` after its DBOS
workflow is terminal and its executor claim is older than fifteen minutes, the
conductor records the turn as an unknown invocation first so the same stop
supervision path can own the guest. Issue #6091 tracks the control-plane root
cause.

A drained factory turn has a separate settlement path. The normal pending-row
rejection remains unchanged because the row is normally the valid continuation
grant. With `FACTORY_DRAINED_LOSS_SETTLEMENT_ENABLED=true`, the factory consumer
may remove that row only when every local identity still matches under the
factory control, capacity-pool, session, turn, pending-message, permit, run and
start locks. The session must still be `recovering`; the pending message must be
unclaimed; and its turn, dispatch count, permit owner, guest, lineage and CLI
transcript must still describe the same first factory attempt. A newer turn,
dispatch, guest, claim, receipt fence, cleanup claim, prior binding or relight
refuses settlement.

The remote proof is equally narrow. Ember must return the exact guest as
durably `evicted` with terminal reason `node_gone`, a transition its dormant
departure reconciler writes only after the owning brick is authoritatively gone
and neither a surviving local artifact nor an exported bundle has a valid
relight target. Its persisted `interrupted_turn` must match the opaque dispatch
ID, turn sequence, CLI session and transcript path computed from the local
rows. This is the paired evidence that the drained dispatch finished its flush
and then permanently lost its only restoration paths. `failed/brick_gone`
proves cessation but not permanent loss and is insufficient here. A missing
guest, generic `destroyed`, timeout, API failure, malformed or stale marker, or
a guest that is running, banking, banked, parked or relighting leaves the
continuation untouched. Settlement keeps the interrupted turn and its result,
transcript and cost history, marks the attempt failed without calling it
`not_invoked` or `UNKNOWN_INVOCATION`, releases its reservation, and feeds the
ordinary bounded replanning path.

The feature is staged off in chart defaults. Enabling it and any production
row remediation are separate operator actions. Before enabling it, exercise a
real disposable factory drain through the consumer and verify both the exact
`evicted/node_gone` settlement and refusal of `failed/brick_gone`, live,
restorable, stale-dispatch and unavailable-control-plane observations. Sessions
8410 and 8416 are not changed by this repository delivery; their remediation
and the final issue acceptance remain separately authorized live checks.

A replica lost mid-invoke used to cost the attempt outright. The rollout
cancelled the executor watching the turn, the guest carried on working, and the
executor recorded an unknown invocation that failed the session and left stop
supervision to destroy a guest that was in the middle of the work. The guest
publishes its complete native record to a result receipt before it writes the
synchronous response, so that record outlives the observer. An invoke whose
response is lost while the control plane still shows the guest running, with an
invoke started and no invoke completion, is now held rather than settled: the
turn is marked interrupted with stop reason `response_lost`, its pending row
keeps its claim so nothing re-dispatches the prompt, and its permit keeps its
state so nothing releases capacity the guest is still using. DBOS workflow
recovery already brings the node back on the new replica, so the recovered
node's next dispatch poll finishes the attempt from that receipt through the
ordinary turn writer, and the conductor's late-completion reconciliation does
the same before deciding the attempt has no result. The model runs once, and
the node owner supplies the declared artifact path a hold reconstructed from
durable rows cannot know, so a recovered attempt keeps its artifact.

Neither owner ends a hold on its own judgement. A guest that has ceased, that
completed its invoke without ever publishing, or that has moved on to another
invoke or another generation, can no longer produce the evidence, so the hold
becomes the ordinary unknown outcome the existing reconciliation settles. A
guest still invoking keeps waiting. Whether a guest is still invoking is the
order of two control-plane stamps rather than the absence of one:
`invoke_started_at` is stamped per invoke and strictly increases, while
`last_invoke_at` is stamped on completion and is never cleared, so an invoke is
in progress when its start is later than the last completion. Reading a missing
completion as "in progress" would have held a guest's first turn and no turn
after it.

Every hold is bounded by the invoke budget clamped to the twelve-hour workload
backstop, after which the claim lease settles it unknown exactly as it did
before. There is one hold per dispatch, ever: an attempt whose marker is
already a hold is refused a second one, or an unrecoverable result would be
re-held at every lease pass until the receipt's own seven-day retention and
pin the admission slot for a week rather than twelve hours. A committed body
that can never be adopted, one that is not a native completion, ends its hold
at once rather than waiting out a bound that cannot change the answer.
Recovery is behind `agents.sessions.responseLostRecoveryEnabled`, off by
default and dependent on `resultReceiptsEnabled`: with no receipt to adopt, a
hold would only delay the same unknown outcome. Every owner that writes,
finishes or ends a hold reads that one flag, so off is the behaviour this lane
had before any of this existed (#5938, #4322).

The pre-guest response-loss window is intentionally separate. A response-loss
hold requires the exact persisted guest binding, result receipt, executor
claim, dispatch count, and permit. If the executor is lost before a guest is
bound, there is no receipt to recover and no remote invocation to resume. The
session instead records `invocation_outcome_unknown`; the
`lost_before_guest` proof requires a terminal failed session, no current or
prior binding, no pending executor, no receipt or work product, and one exact
uncertain permit. The factory may then fail the attempt at zero cost and
release that reservation exactly once. Changing
`agents.sessions.responseLostRecoveryEnabled` does not widen or disable this
proof. A bound guest, including one whose generation or invoke stamp changed,
remains on the response-loss and stop-supervision paths and is never refunded
by `lost_before_guest`. A message with dispatch count zero remains the separate
`never_dispatched` proof.

The in-process operator repair is
`factory.orchestration.factory_controls.settle_lost_attempt(task_id, node_key,
attempt, actor)`. Before calling it, an operator must confirm the exact node's
DBOS workflow is terminal. The function has no DBOS handle and cannot perform
that check itself. It then locks the factory control row and accepts only an
active, unpriced run with no newer attempt and the exact pinned workflow and
session ownership. The execution proof is re-read in the settlement
transaction, so a late guest binding, a new dispatcher, changed ownership, or
new execution evidence refuses the repair. Repeating the call after success
also refuses because the attempt is already terminal. This repairs one attempt
without cancelling or re-admitting the receipt, and it is deliberately
available while `factoryLostBeforeGuestSettlementEnabled` is off. It is an
operator action, not an HTTP endpoint.

An attempt whose workflow died mid-way has no session recorded on its run,
because `record_dispatch` binds one only at completion. The reconciler resolves
it by the deterministic `local_session_id`, `factory:<task>:<node>:<attempt>`,
under the same ownership checks `reconcile_completed_node` applies, and binds
it, so supervision can start. A repeated observation of unknown execution
records nothing: the first uncertain outcome stands until reconciliation makes
it terminal.

Missing provider usage consumes the entire reserved ceiling. This is
conservative admission accounting, not an interruptible dollar cap on a running
provider turn. Observed overruns prevent further admission. The exception is an
attempt whose own evidence proves it never reached a model POST, an
`invocation_phase` of `never_dispatched`, `not_invoked` or `lost_before_guest`
recorded on the outcome or on the typed proof attached under that phase: it
books at nothing on the `no_model_post` basis, in the graph and on the start
row alike, because charging it the ceiling retired its node on the first
failure (#6045). An attempt that may have reached
the model with an unknown cost, `guest_cessation_confirmed` after dispatch
among them, stays conservative and keeps its reservation.

A planner decision is one graph edit or one `plan` whose edits apply together
under a single expected revision, so a rejected edit rejects the whole plan and
the graph never holds half of one. Each edit becomes its own plan version under
the shared cause, applied in dependency order rather than in the order written,
and an edit may name a dependency by the key its author wrote where the server
can resolve that to a role-prefixed key without guessing. Review correction is the server's, not the
planner's: when a review returns `changes_requested` the reconciler appends
`correct_<n>` on the model that produced the reviewed head and `review_<n>` on
the configured independent reviewer, up to `max_review_rounds`. Those keys are
refused to a planner, and the rounds are counted from the version ledger, so
discarding or renaming a correction node cannot buy another one. A round that
fails is reopened by the engine for the same reason it was opened by it: the
planner is refused those keys and `swarm/graph.py` refuses discarding a node
that has run, so a failed `correct_<n>` would otherwise leave the task with no
task-local recovery path at all. The replacement round carries the same reviewed
head and findings, and the failed round still counts, so a round that keeps
failing spends the bound instead of looping inside it. A node that escalated is
deliberately not reopened, because escalation asked for the planner.

The same loop owns landing recovery. A delivered PR with a merge conflict or
confirmed queue ejection returns to the same task for bounded assessment and
correction. Conflict workers rebase onto the PR base; other ejections require
reading queue and check evidence before choosing a retry, rebase, code repair,
or escalation. Every round ends in an independent review of the resulting head.
`landing_recovery_round` records the request and graph round. Exhausting the
ordinary correction bound returns the evidence to the conductor's existing
assessment, funding, and escalation path.

Reopening cleans up after the round it replaces. The failed round's re-review
never ran and never can, so it is discarded in the same atomic edit, which
returns its attempt and its ceiling to the allowance; its correction stays,
because it has runs and the graph refuses discarding those. A correction can
also push and then die, so the replacement is briefed at the task branch head
read live at insertion, with the findings still attributed to the earlier head
the review inspected, and it falls back to that head when the branch cannot be
read. Once a later round supersedes it, a `correct_<n>` or `review_<n>` stops
being scanned as an open failure, or a task would be handed `node_failed` on a
node the planner may neither add nor discard at the moment the round that
replaced it delivered. When the bound is spent the deviation still names the
review, and it now also names the last correction and why it delivered nothing.

Each round inherits its timeouts from the nodes it repeats: the correction takes
the reviewed implementation node's `turn_timeout_seconds` and the re-review
takes the original review's, each clamped to the policy ceiling. The planner is
asked to size every node it adds to the work that node really does, roughly 900
to 1800 seconds for investigation, 3600 to 7200 for implementation and 3600 for
review, rather than leaving the policy maximum in place.

The correction brief states its completion contract: commit on the task branch,
push, confirm the pull request head moved, and write the declared JSON artifact.
A guest with no local test tooling records that in the artifact and pushes
anyway, because the required Linux CI that gates the work runs on the pull
request and not in the guest. A turn that ends with neither a push nor an
artifact fails the round.

The planner is
called back only for a named deviation: no plan applied yet, a node that failed
or escalated with no runnable retry, exhausted review rounds, or a settled
graph with no verified delivery.

Nodes that can start together are dispatched together, up to
`max_parallel_nodes`. That set is a wave: source-writing nodes that have never
run, whose dependencies are all already on the task branch, and that no
dependency path connects to each other. Each member works on
`factory/<task-id>-<node key>`. That is a sibling of the task branch, not a path
under it, because git cannot hold `refs/heads/factory/<task-id>` and a ref below
it at once. An `integrate` node merges those branches into the task branch,
resolves conflicts, runs the targeted checks and reports the integrated head;
review then examines that head and the correction rounds work on the task
branch. The planner may add the integrate node itself; otherwise the engine
inserts `integrate_<n>` over the wave and repoints its dependents at it, so every
branch the engine handed out is one the fan-in merges. A node takes a branch of
its own only once that fan-in exists, so a refused insertion asks the planner
rather than stranding work, and the branch a node first ran on is pinned for
every later attempt of it. `integrate_<n>` is reserved to the engine exactly as
`correct_<n>` and `review_<n>` are. While a wave is open it is the only source
of source-writing work that may start, so a node outside it waits its turn
rather than writing the task branch beside the wave. Extra concurrent nodes are
admitted only when the shared session pool has room, and a node the pool cannot
hold stays ready for the next tick rather than failing. At a limit of one none
of this applies and the lane is serial, a planner-authored integrate node
included.

The guest hydrates the existing task branch, or the base branch before the
task branch exists. Source changes belong in a dedicated linked worktree on the
branch its brief names. Transient typed artifacts must be written under
`/workspace/src/.factory/`, where the shim captures them. Current dispatch uses
validated complete added-file diffs; the existing whole-file channel is accepted
only when its stored path and validation metadata match the declared artifact.

Completion requires a non-draft PR on the exact task branch, a successful
implementation node, the latest independent review approving the current head
on a model the reviewer pool allows, and passing repository PR status checks. GitHub is read again to verify
the head and checks. Delivery evidence remains in the task audit.

The pull request body must close the task's issue. Every delivery node's
boundary states the required `Closes #<issue>` line and says to keep it on
every update to the pull request, engine correction rounds included, and the
completion gate reads the body back from GitHub and refuses a delivery that
does not carry a closing keyword. The refusal is named
`pr_missing_close_keyword`, so the planner reads what is wrong instead of a
generic validation failure and can ask for the body to be fixed. The gate
accepts every keyword GitHub acts on, `closes`, `fixes` and `resolves` in all
their forms, because refusing a body that says `Fixes #123` would fail a
delivery that does close its issue, and all three reference forms GitHub
honours: `#123`, `owner/repo#123` and the full issue URL. Only this task's own
issue in this task's own repository counts.

### Landing

#### Review publisher

The landing path contains a trusted `factory/review` check publisher. It is off
by default (`factory.reviewPublish.enabled: false`) and changes no behavior
while off. When enabled, it loads the task's durable receipt, admission policy
version, settlement PR and approved head, and latest assigned `review_*` run.
It accepts only a successfully validated approval from a session distinct from
every implementation, integration and correction session. A fresh GitHub read
must show the open, non-draft PR in the policy repository, on the exact
operator-authorized task branch and base branch, at that approved head. Missing,
failed, invalid, stale, self-authored, moved or superseded evidence fails closed.
A later valid `changes_requested` review of the same SHA publishes a failing
check to invalidate an earlier success.

The publisher uses only `FACTORY_REVIEW_PUBLISHER_TOKEN`, supplied through an
injected provider. It never falls back to `GITHUB_API_TOKEN`. A missing token
publishes nothing, audits `review_publish_skipped`, and prevents landing from
arming that pull request. The chart intentionally wires no Secret in this
change.

Activation still requires operator work outside this repository-only slice:

1. Deliver a broker-minted `review-publisher` token to the trusted monolith
   caller through the `bosun-review-publisher` SPIFFE grant described in
   `projects/embervm/tokenbroker/README.md`. Do not expose that grant to guests,
   implementers or the shared noded identity.
2. Canary a real check, then require `factory/review` on `main`, pinned to the
   Bosun numeric App ID alongside existing Linux CI. Do not use an any-App
   source or add an agent to a bypass list.
3. Verify an unreviewed or moved head cannot merge and a later rejecting review
   invalidates the same-SHA success before enabling merge behavior.
4. Activate merging separately. This change does not provision credentials,
   alter branch protection, enable the publisher in a deployment, or enable a
   merge queue.

Merge landing is off unless the policy sets `auto_merge` to `true`, and only
the value `true` counts: a policy written before the flag existed, or one
carrying anything else, lands nothing. Review publication may be canaried with
merge landing still off. Merge landing is the first factory step that writes
pull request and issue state, so the process needs a GitHub token with those
permissions before the merge flag is worth turning on.

With the flag on, a task that settled `succeeded` with pull request evidence
has its merge armed through the GitHub auto-merge mutation with the rebase
method, the equivalent of `gh pr merge --auto --rebase`, and the lane audits
`merge_armed`. Landing first checks that the pull request is still at the exact
approved head and has no computed merge conflict. Exactly one factory pull
request is armed at a time, because
this repository merges through the GitHub merge queue and an ejection cascades
across every candidate behind the one that failed. The holder is read from the
lane's own audits and from GitHub: before arming anything, up to five pages
of 50 open pull requests are listed and any pull request on a `factory/` branch with
auto-merge or a merge queue entry already set counts, so a pull request an operator armed by hand is
not raced. A holder check that cannot be read or exhausts five full pages arms nothing,
because not knowing
is not a licence. Every waiting delivery audits `merge_deferred`, once per
blocking pull request, and is armed on a later tick.

Selection is on landing state, never on recency: every non-advisory `succeeded`
receipt whose landing has not reached a terminal audit, oldest first. Terminal
is `merge_arm_refused`, which hands the pull request to a human, or
`issue_closed`, which is the last step of a successful landing. Taking the
newest receipts of any class instead let a burst of advisory settlements push an
armed but unmerged delivery out of the batch, which left it never observed and
the holder reading as absent. A delivery the lane has never touched is skipped
once it is a week old, so turning the flag on does not stampede over history,
but anything the lane has armed is followed to a terminal state whatever its
age.

The same reconcile tick owns PR retirement independently of `auto_merge`.
When a receipt settles `escalated`, `cancelled`, or `failed`, including the
deadline-expired escalation, its open same-repository `factory/` PR is
converted to draft. One marker-fenced comment names the receipt, settlement
reason, and the next action: re-admission adopts that branch. A succeeded task
is untouched. Before the comment and again before the draft mutation, the
server re-reads the PR and checks active branch ownership. If a successor task
has adopted it, that task owns readiness and the old settlement does nothing.

The paced sweep on that tick examines at most 20 open PRs from a persisted page
cursor. It never mutates a non-`factory/` head. An unowned factory PR is closed when one
of its closing issues is closed, or when that issue has another open factory PR
whose exact branch is owned by a running receipt. The comment names the
survivor when one exists and otherwise says explicitly that the issue is
already closed. The survivor is not modified. A prepared audit, a hidden
comment marker, fresh PR and owner reads, and the `factory_pr_retired` audit
make partial GitHub failures and repeated ticks safe. Cursor progress is
durable, so 20 permanently live older PRs cannot hide later stale candidates.

This is configured repository behavior, not an operational rollout claim. The
2026-09-19 observation of duplicate pairs 6198/6070, 6199/6075, 6209/6082,
and 6215/6078 is the replay fixture for the gap. This change does not assert
that a deployed reconciler has run the sweep against GitHub; rollout and its
first observed audits remain operational evidence.

Later ticks observe the armed pull request:

| What GitHub shows | What landing does |
|---|---|
| merged | audits `merged`, then closes the issue |
| closed, not merged | audits `merge_arm_refused` and stops |
| open, head moved off the armed SHA | turns auto-merge back off, audits `merge_arm_refused` with `head_moved` |
| open, computed merge conflict | disables auto-merge, then requests bounded recovery |
| open, neither auto-merge nor queue membership | requests assessment of the ejection |
| open, still armed or queued | waits |

Queue membership is checked independently of auto-merge. GitHub clearing
`autoMergeRequest` on queue admission is not an ejection. Failed or malformed
membership reads retain the slot.

Recovery preserves the same task, graph, PR, spent turns, and dollar accounting.
It admits at most two recovery episodes per task, each with a fresh one-hour
deadline, and respects current delivery capacity, task pauses, cancellation,
and unresolved execution. It does not reset the correction-round or spending
limits; any further funding uses the existing conductor assessment. A recovered
settlement starts a fresh landing epoch, and arming requires its approved SHA
to match GitHub. Exhausted recovery is left for a human with a warning.

One historical `merge_conflict` or `ejected_from_merge_queue` refusal is
reassessed per tick. Reopening supersedes that refusal without deleting history.
Closed PRs, changed heads, active queue entries, and operator refusals are not
blindly retried. A request that cannot acquire delivery capacity remains eligible
for a later tick; it does not bypass the concurrency limit.

A mutation GitHub refuses audits `merge_arm_refused` and is not retried; a
GitHub read or write that fails audits `landing_error` by exception type and
status, never by response body, at most once an hour per task, and the next tick
retries. `merge_armed` and `merge_ejected` are counted rather than fenced,
because a delivery can be armed, ejected and armed again; every other step
writes one row per task and that row is its fence.

Landing stops at the merge. Confirming that the chart version write-back landed
and that the new image is live is the verify node #6002 phase 4 still owes; the
`merged` audit carries a `rollout_verified` field that is null until that node
exists.

### A delivery pause is a decision request

The delivery planner's fifth action is `pause`, and it now leaves the lane
rather than sitting in it. A pause carries the one `question` a person must
answer and the same two to four `options` a refine escalation carries, with the
recommendation first, and the prompt asks for concrete ones: rescope to a named
surface, close as stale, split, defer, continue with a stated assumption.

Option one has to have effect `agent-ready`, which is the one that carries the
work on. That is stricter than the refine rule, where the recommendation may be
any of the four, and it is stricter for one reason: `resume_task` applies
option one without showing the operator the card, so a pause recommending a
close would turn pressing resume into closing the issue. The planner may still
offer close, split, defer and hold; it may not recommend them from inside a
task that has a branch and usually a pull request open. A pause missing its
question, its options, or that first effect is refused as decision feedback
with `pause_without_question`, `pause_options_invalid` or
`pause_recommendation_not_continue`, so the planner repairs it inside the task
it already has instead of settling a card nobody can safely act on.

**Why.** A pause is a decision request, so it leaves the lane. It used to leave
the receipt `admitted` with `task_paused` set, which held a delivery slot and
its accounting until an operator resumed or cancelled by hand. Worse, resuming
replayed the same pause: the planner's context is the issue body captured at
admission plus graph evidence, so an answer posted as a comment never reached
it. On 2026-09-12 that happened twice in one day, on #3824 and #3832. So the
pause settles instead. The receipt goes to a new state, `escalated`: the slot
and every reservation are free, no node runs, the graph and the accounting stay
exactly where they are for a decision to be read against, and the question
reaches a person as a card with buttons.

Settling writes the `needs-human` label first and the decision card second. A
card posted onto an issue intake can still pick up is the one ordering that
lets the lane re-admit the work while somebody is reading the question. The
card names the question, the planner's reason, where the task got to, the
numbered options, and the branch and pull request the attempt left behind. Both
writes are fenced, the label by being idempotent and the card by a hidden
marker naming the task, because settlement is re-reached on every tick until it
takes. One Discord warn carries the escalations link. Intake skips an escalated
issue twice over: it carries `needs-human`, which the default exclusion list
drops, and the sweep also excludes any issue holding an escalated receipt, for
the operator who takes that label off while the decision is still open.

**Deciding re-admits with direction.** An escalated delivery is decided through
the same endpoint and the same escape group as a refine escalation. An option
whose effect is `agent-ready`, which is what continue, rescope and assume all
are, returns the receipt to `queued` under its own identity and records the
choice, its detail and the operator's note as `direction_json`. The next
admission mints a new task with an empty graph, and that task's first planner
round carries the direction in its context as an `operator_direction` section:
untrusted text and evidence, never authority, naming the previous task, its
branch and its pull request so the work is carried on rather than started
again. Later rounds do not repeat it, because the plan it shaped and the
decision feedback under that are already the record of it. Reading it audits
`operator_direction_read`.

`close`, `defer`, `split` and the escape group behave exactly as they do for a
refine escalation, and then settle the receipt `cancelled`. Cancelled rather
than succeeded, because nothing was delivered and a succeeded delivery receipt
sits in the `delivered` exclusion intake keeps for good, which would take the
issue off the lane on a decision that never said to. A receipt the lane could
not admit again, a generation the policy has moved past most often, is settled
the same way with `blocked_by` on the resolution: an answered card whose work
is scheduled nowhere is the stuck state this replaced.

The `chat` action works on a delivery escalation too. It posts `Operator asks:`
on the issue and re-admits the same way, with the note as the direction and no
option applied. It does not resolve the escalation, so the card stays live and
an option can still land on a receipt that is already queued: that answer
replaces the direction the chat left rather than re-queueing a second time, and
a terminal answer settles the receipt `cancelled`. The branch and the previous
task come off the stored direction there, because a re-queued receipt no longer
names a task of its own. `resume_task` on an escalated receipt applies the recommended
option, because the first option is the recommendation and resume is choosing
it without reading the card; `stop` settles every escalated receipt
`cancelled`, since the reconciler only visits active tasks and would otherwise
leave a card waiting on a decision for a lane that is shut. `pause_task` and
`resume_task` are unchanged for a task that is still running.

A re-admission is a whole new task with a fresh graph, a fresh allowance and a
fresh `task_budget_usd`, because the escalated attempt's spend is history and
the new task has to be able to plan and deliver inside its own envelope. So the
receipt carries `previous_task_ids`, and the board shows what those attempts
cost as `previous_spend` beside the current task's own accounting rather than
folded into it: `turns_used` and `committed_cost_usd` are measured against this
task's allowance, and adding a previous attempt's spend would read as a task
over budget before a node had run. Nothing caps how many times one issue may be
re-admitted, so that total is the number to watch.

The receipt holds one escalation document at a time. A task that escalates a
second time supersedes the first, moving it into `history` with its resolution,
because the same receipt is re-admitted under its own identity and keeping the
answered document would leave the page showing options written before the
question was answered.

## Controls and uncertainty

`pause_admissions` allows already admitted work to finish. `pause_task` stops
new nodes for that task. `stop` durably fences factory admission and descendants,
including the shared pending-message sweep and transport creation/invoke retries.
The coordinator makes at most two recorded cancellation attempts per active node.
Other operator-owned sessions are outside this control scope.

A task pause written by the reconciler expires after two hours. The conductor
settles any uncertain starts, cancels the task with
`reconciler_pause_expired`, clears its paused flag, and releases its delivery
slot. Only a successful reconciler pause audit is eligible. A pause written by
an operator never expires automatically.

A network operation already in flight can remain uncertain after stop. Status
continues to show those reservations and cancellation requests; the stop flag
does not assert that every guest has ceased. Stop is terminal for this first
bounded lane. It cannot be reset by replaying an earlier enable request.

Agent workloads have a twelve-hour runtime backstop. The caller's result wait
and routine drainer observation wait exceed that ceiling. The CLI silence
backstop is eleven hours and fifty-five minutes: a quiet build is not by itself
proof of a stuck task. DAG node limits remain explicit immutable policy;
raising the runtime ceiling does not rewrite admitted attempts or grant retries.
Fresh sessions have a one-day lifetime, and an older session has only the time
remaining before its absolute expiry. Idle parked sessions still expire after
one hour.

Permit supervision settles an hour-old unbound `kg` or `project` permit when
its session is terminal and no durable binding evidence exists. For a bound
non-factory guest parked or banked on a departed node, it requests destruction
with the observed generation, at most twice, and keeps the permit uncertain
until the control plane reports cessation.

The stop path fences admission and requests DBOS workflow cancellation. An
uncertain attempt retains its capacity until exact control-plane evidence proves
that its owned guest ceased. A parked guest on a departed node is first asked to
destroy, then settled only after the later `destroyed` view. Issue #6091 tracks
the control-plane root cause that can otherwise leave this state stranded.

## Validation

New tests have explicit targets in `projects/monolith/BUILD`. File-backed
hermetic tests cover concurrent receipt deduplication and admission, immutable
policy and pins, rollback across both reservation ledgers, task and node limits,
latest-review evidence, and the complete issue/planner/work/review/PR sequence.
Transport tests cover stop during capacity retries and independent concurrent
contexts. Linux orchestrator CI and a real bounded operating trial remain the
delivery gates; local tests are advisory.


### Independent conductor watchdog

The factory owns task decisions and durable recovery state. Kubernetes owns
process recovery through the existing backend `/healthz` liveness probe, so
watchdog execution does not depend on the conductor, DBOS, a model grant, or
the agent session queue. The factory module supplies a process-local liveness
check through the framework's `register_liveness` hook. Public profiles do not
run private liveness checks.

The watchdog arms when the elected leader starts the conductor. A stopped
conductor task fails immediately; a running loop fails after 600 seconds
without a completed reconciliation pass. The existing kubelet probe then
requires six consecutive failures, checked every ten seconds, before replacing
the backend container. Healthy followers and disabled factories stay healthy.
Leadership resignation disarms the watchdog before runtime shutdown.

A completed pass counts as progress even when it returns an error, because
the reconciler can retry a dependency outage without a process restart. Agent
turn duration, quota waits, and paused admissions do not age this signal while
reconciliation continues. The probe performs no I/O and does not cancel work,
re-admit issues, settle uncertain outcomes, raise budgets, or make model calls.
After replacement, normal workflow recovery and reconciliation use the existing
receipt, attempt, session, and accounting identities. HTTP responsiveness alone
no longer hides a conductor that has stopped making progress.

### Reviewed reservation leases

With `FACTORY_RESERVATION_REVIEW_ENABLED`, every execution reservation starts
with a 30-minute work-review lease. The existing executor heartbeat remains an
ownership mechanism; it never renews the work-review lease. The factory begins
an Astra review five minutes before expiry and bounds the review to four minutes.
One durable review runs at a time, using reserved interactive headroom so a full
background pool cannot starve supervision.

The review sees the task objective, exact attempt, bounded progress, previous
review, and a fresh control-plane guest observation. Approval renews the exact
dispatch for 30 minutes. Repeated approval requires new substantive evidence.
Unknown outcomes cannot be approved. A stop, replan, or steering decision uses
the existing exact-attempt stop protocol. Steering reaches the next conductor
plan after cessation confirmation; it does not inject concurrent input into a
running model turn. Original results and unknown costs remain intact.

`/api/health` reports the critical `factory_reservations` component unhealthy
for overdue or blocked reviews, pending stops, held routine jobs, and active
factory attempts missing reservation coverage. `/healthz` remains process
liveness, so a blocked job does not restart the monolith repeatedly. Expiry
fences subsequent dispatch but never refunds capacity or proves guest cessation.

The permit observer reconciles held routine jobs only after fresh, exact guest
cessation evidence. It atomically settles the reservation and either rearms an
unprocessed job or retains an already-applied extraction. Missing guest identity
remains unhealthy and requires evidence; elapsed time is not a no-guest proof.

Non-graph execution sessions use a durable conditional stop intent and the same
unknown-outcome fence. The observer accepts only the reviewed invocation's
cessation proof; later queued user input is preserved. Never-started reservations
use positive local cancellation proof. An explicit routine stop parks the job
atomically and prevents ordinary retry from rearming it. Steering and replanning
carry guidance into the next routine attempt only after confirmed cessation.


### Work item pointer comments

`FACTORY_WORK_ITEM_POINTER_ENABLED` and `FACTORY_WORK_ITEM_BASE_URL` (default
`https://private.jomcgi.dev`) enable GitHub comments that link each work item
to its durable factory record. One comment per GitHub-sourced work item, created
once and edited on mint, transition and authority changes, never read back. The
comment includes a `<!-- work-item:ID -->` marker, the work item state, and a
link to the work item detail page. Pointer comments exist only for display and
reference; labels, changes and comments on the GitHub issue are not mirrored
back.

The sync runs at most 20 items per tick. A failed GitHub write triggers
exponential backoff: wait 2^n minutes (capped at 1440 minutes / 24 hours) after
the nth failure, then retry. A successful write clears the backoff counter. The
sync excludes closed work items and skips work items lacking a GitHub issue
number.

The `/factory/work-items/{id}` page ships work item details, edges, and recent
events. All edges show their source (manual, github_body, or decision) for
transparency about how each relationship was created.

### Autonomous correction continuation

`FACTORY_AUTONOMOUS_CONTINUATION_ENABLED` allows the engine to grant one final
correction and independent review when a genuine work-turn limit blocks that
pair. The grant names both one-attempt nodes, the source review and current PR
head. It commits with the graph edit and survives retries and restarts. Capacity
denials use the existing bounded no-execution accounting rule.

The task, branch, PR, original dollar budget, deadline, and spent history stay
in place. Other work cannot borrow the grant, and it cannot create a successor
grant. Approved delivery goes through the normal exact-head verification gate
without spending another planner turn. Failure or another negative review ends
the task with its PR and findings retained, without a human decision card.
Human escalation remains available for actual missing decisions or authority.

### Conductor funding decisions

`FACTORY_CONDUCTOR_FUNDING_ENABLED` supersedes the fixed final correction pair.
When a work allocation, review-round limit, or lease is exhausted, Astra judges
whether the objective is still useful, what progress has been made, and whether
the likely remaining cost is reasonable. Its typed decision can continue,
steer, or stop. There is no fixed extension count. Each approval records a
reason, concrete next plan, current-task dollar ceiling, additional work turns,
and a funding review horizon of at most 30 minutes. The horizon fences new work;
already-reserved workers retain their invocation deadlines and continue under
the existing 30-minute reservation supervisor. Original receipt policy and prior
charges remain intact; durable amendments supply the effective limits.

All committed factory node execution and funding decisions for the same repository issue count toward a
$200 automatic ceiling across tasks, generations, and advisory/delivery passes.
Shared reservation supervision remains separately accounted operational overhead.
Immutable admission audits retain task-to-receipt ownership after readmission;
the truncated predecessor list used for display is not the accounting ledger.
Reservations and unknown outcomes remain charged. Every new start rechecks the
aggregate under the factory control lock. A new task cannot reset that budget.

A funding review reserves one exact Astra dispatch with a $1 ceiling and a
five-minute deadline through the existing durable node executor. That dispatch
can assess an exhausted task allocation; other work remains fenced until the
decision commits. Global stop, cancellation, unresolved execution, shared
capacity, and the cumulative objective ceiling still apply. Failed or stale
reviews wait five minutes before another bounded assessment. A stop decision
retains the issue, PR, and findings without a human restart question. The
normal independent exact-head delivery review remains required.


### Conductor-owned reversible gates

Refine, investigate and planner artifacts carry a typed `gate` when a missing
parameter, unavailable live check or existing delivery target would otherwise
raise a decision card. A parameter classification of `reversible` includes its
proposed `value` and `reason`. The builder chooses it and comments
`Decided by the conductor: <value>, because <reason>; reversible` once.
`spending`, `prod_deletion` and `external_account` keep the human decision path.
A case-insensitive heuristic backstop also escalates restricted terms in the
gate's value or reason, even when the model labels the gate reversible.
A documented default does not authorize bucket creation, deletion or credentials.
Unclassified legacy questions retain their existing escalation path.

For `live_validation`, the conductor records a default-off or staged repository
`scope` and appends `live_checks` as unchecked lines on the issue. The PR body
states `Conductor rescope:` and retains references without closing keywords.
The delivery gate refuses a PR that closes pending operational acceptance, and
landing records `repository_delivery_complete` after merge without closing the
issue. Required Linux CI and independent exact-head approval still apply.
Refine decisions carry into delivery admission; the receipt preserves decisions
across restarts and every planner and worker receives the current scope.

Before its first node, a delivery task discovers open PRs closing its issue,
including operator re-posts. It adopts the oldest matching PR (preferring an
already granted match), records the branch and PR on the receipt, and directs
rebase onto main, repair and fresh independent review. Discovery failure or
truncation waits without starting a competing branch. A running branch owner
is named in an escalation; no second writer starts without that decision.
Only existing PR heads in this repository under `factory/` may be adopted.
Heads outside `factory/` escalate with the PR author's login. The base branch
and another repository's head are never adopted; fork and deleted-fork PRs
are excluded from discovery candidates.

Discord uses one durable human-needed fence per task and notification kind:
refine, escalation, intervention, deadline and landing. Supervision combines the attempts
known at the time into one summary. Later observations remain in the audit
instead of sending another message. Automatic stall recovery is audit-only.
A failed notification attempt is audited without consuming the fence, so it
can be retried, and does not block task settlement.
