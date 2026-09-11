# Bounded issue delivery lane

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
and `intake` are the only optional fields; every other field is required.

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

`swarm.factoryMaxConcurrentTasks` bounds the sum. Delivery is served first and
keeps at least one slot under any ceiling, and advisory takes what is left, so
a ceiling set below the policy narrows advisory before it narrows delivery. At
a ceiling of 1 the advisory lane cannot open at all.

Admission and intake both work per lane. A full delivery lane no longer refuses
an advisory admission, and intake ranks candidates exactly as it did, delivery
before refine, but fills at most one candidate per lane per tick rather than
one candidate in total. A candidate whose lane is full is counted as
`lane_full` in the idle audit. The board shows each lane as used against limit.

### Reviewer fallback while the Claude window is spent

Every delivery task ends in an independent Opus review on the shared Claude
subscription, and that window is the one input the factory can exhaust:
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
`reviewer_fallback` and `reviewer_restored`, one row each, and the board renders
the state from that ledger without a broker call. An unknown reading, or one
older than an hour, never starts a fallback: it audits `quota_guard_unknown` at
most hourly and review stays where it is, because downgrading every review
whenever a broker read fails would turn one outage into two. It does not end a
fallback either, so a fallback entered at 95 percent does not snap back to Opus
the moment the broker goes down.

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

These must stay at or below what EmberVM will actually create for the session
workloads, `claudeRuntimeWorkload` and `piRuntimeWorkload` `cap` and
`session.maxSessions` in `projects/embervm/chart/values.yaml`. Above those, a
granted permit meets a `session_cap` 429 at guest create, which fails the turn
rather than making it wait.

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

`enabled` controls autonomous issue discovery. `labels` names delivery-ready
labels and may be empty, in which case no issue is a delivery candidate.
`exclude_labels` rejects an issue before selection. `max_per_day` is between 1
and 50, and `cooldown_hours` is between 1 and 168. `refine_enabled` allows an
otherwise eligible issue with none of the delivery labels to enter the refine
path. `close_enabled` allows a refine verdict to close an issue and
`max_closes_per_day` bounds how many it may close in a rolling 24 hours,
default 3. All three feature flags default off or, for the cap, bound a
capability that is itself off.

Intake sweeps GitHub at most once an hour while it is finding nothing, and
again immediately after any receipt settles. A tick that cannot read GitHub
audits `intake_error` rather than failing silently. The sweep reads open issues
and open pull requests oldest first, up to five pages of one hundred each; a
read that hits that cap records `truncated` on the audit, so a partial sweep
reads as partial.

Intake admits at most one issue per tick and never exceeds `max_per_day` over a
rolling 24 hours. A failed or cancelled receipt cannot be selected again until
its cooldown expires, whatever class it settled under. Assigned issues,
excluded labels, issues linked from an open pull request, and issues already
received in the current generation under the same task class are not
candidates. Scoping that last one to the class is what lets an issue a refine
pass moved to `agent-ready` be delivered in the same generation, with no
operator bumping `generation` to release work the lane itself made ready. The
receipt's identity carries the class for the same reason. Intake-created receipts become admissible only while
`intake.enabled` is true. Turning intake off therefore leaves the operator's
`issue_numbers` allowlist exactly as it was.

Delivery candidates rank before every refine candidate. Within either group,
`critical` ranks before `bug`, then the oldest issue ranks first. A refine
candidate carrying `critical` never outranks a delivery candidate without a
rank label.

Each intake receipt carries the task class that sets its verification mode,
implementer floor, and gate (ADR agents/038 decision 5):

| Class | Verification | Floor | Gate |
|---|---|---|---|
| `bug-fix`, `mechanical-refactor`, `docs` | machine-verified | the worker pool | independent Opus review plus required CI |
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
DAG. The node posts exactly one `## Agent brief` comment with `### Outcome`,
`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that order,
then reaches exactly one of four verdicts and acts on it:

| Verdict | What the node does | What settlement demands back from GitHub |
|---|---|---|
| `agent-ready` | applies `agent-ready` | the label, the brief, and the issue still open |
| `needs-human` | applies `needs-human`, ends the brief with `### Decision needed` carrying `recommend: deliver \| close \| split \| defer` and the one question a person must answer | the label, the brief, the issue still open, and one warn notification naming the issue, the recommendation and the question |
| `reject` | adds `### Why not` citing a file, pull request or recorded decision, applies `wontfix`, closes with reason `not_planned` | the label, the brief, and the issue closed |
| `stale` | adds `### Why stale`, applies `stale`, creating the label if the repository has none, closes with reason `not_planned` | the label, the brief, and the issue closed |

`reject` and `stale` are for a premise a reader can check: a decision recorded
in an ARCHITECTURE.md **Why.** paragraph, work already merged, a file or flag
that is gone. The prompt says that doubt resolves to `needs-human` with a
recommendation rather than to a close, because a wrong escalation costs a
minute and a wrong close costs the issue.

Closing is gated twice. It needs `close_enabled`, and it needs room under
`max_closes_per_day`, counted from the `intake_closed` audits of the last 24
hours. Both are read before the node runs, so the prompt offers three verdicts
rather than four when closing is unavailable, and again at settlement, so a cap
spent while the node was running still holds. A close verdict the lane may not
act on is downgraded to `needs-human`: the server then demands the
`needs-human` label and an open issue, exactly as it would for an escalation,
and records `refine_close_downgraded`. An issue carrying `critical` or
`security-finding`, or assigned to any milestone, is never closed and takes the
same downgrade. Every close that settles audits `intake_closed` with the
evidence cited.

The server settles from a re-read of the issue, never from the node artifact
alone. A verified `needs-human` result succeeds because the briefing and the
escalation both completed, and a verified close succeeds because the issue is
demonstrably closed with its reason on record. A mismatch fails the task and
the issue takes the cooldown. Two failed attempts settle the task failed and
apply nothing.

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
attempt, task-turn, deadline or budget bounds. Confirmed failed artifacts feed
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

An attempt whose workflow died mid-way has no session recorded on its run,
because `record_dispatch` binds one only at completion. The reconciler resolves
it by the deterministic `local_session_id`, `factory:<task>:<node>:<attempt>`,
under the same ownership checks `reconcile_completed_node` applies, and binds
it, so supervision can start. A repeated observation of unknown execution
records nothing: the first uncertain outcome stands until reconciliation makes
it terminal.

Missing provider usage consumes the entire reserved ceiling. This is
conservative admission accounting, not an interruptible dollar cap on a running
provider turn. Observed overruns prevent further admission.

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
the head and checks. Delivery evidence remains in the task audit. This lane
does not submit a merge or a deployment.

## Controls and uncertainty

`pause_admissions` allows already admitted work to finish. `pause_task` stops
new nodes for that task. `stop` durably fences factory admission and descendants,
including the shared pending-message sweep and transport creation/invoke retries.
The coordinator makes at most two recorded cancellation attempts per active node.
Other operator-owned sessions are outside this control scope.

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

The current stop path fences admission and requests DBOS workflow cancellation.
It does not yet guarantee termination of an external Ember guest. Issue #5922
tracks the bridge to the exact owned invocation, cessation confirmation, ongoing
run inspection and bounded resume. Until that is implemented, an uncertain
attempt retains its capacity and cannot be restarted merely because its observer
or workflow timed out.

## Validation

New tests have explicit targets in `projects/monolith/BUILD`. File-backed
hermetic tests cover concurrent receipt deduplication and admission, immutable
policy and pins, rollback across both reservation ledgers, task and node limits,
latest-review evidence, and the complete issue/planner/work/review/PR sequence.
Transport tests cover stop during capacity retries and independent concurrent
contexts. Linux orchestrator CI and a real bounded operating trial remain the
delivery gates; local tests are advisory.
