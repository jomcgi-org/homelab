# Bounded issue delivery lane

The first autonomous lane reads explicitly selected GitHub issues, records one
durable receipt per repository/issue/generation, and admits up to the policy's
`max_tasks` tasks at a time, capped by the chart's
`swarm.factoryMaxConcurrentTasks` (1 today). `max_tasks` is a concurrency,
not a lifetime count: as tasks settle the lane keeps admitting until its
issue list is exhausted, so it runs without an operator re-arming it.
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
  "max_tasks": 1,
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
own and defaults to 2. Those, `reviewer_model`, `model_pools`, and `intake` are
the only optional fields; every other field is required.

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
path. Both feature flags default off.

Intake admits at most one issue per tick and never exceeds `max_per_day` over a
rolling 24 hours. A failed or cancelled receipt cannot be selected again until
its cooldown expires. Assigned issues, excluded labels, issues linked from an
open pull request, and issues already received in the current generation are
not candidates. Intake-created receipts become admissible only while
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

Delivery classes come from the issue's labels in this order:
`security-finding` and `needs-thought` select `judgment-analysis`, `bug` selects
`bug-fix`, `documentation` selects `docs`, and `todo` selects
`mechanical-refactor`. `security-finding` remains excluded by default. An
unclassified `agent-ready` issue defaults to `bug-fix`. A receipt written
before classes existed also reads as `bug-fix`, so operator-posted receipts are
unaffected.

The judgment floor is a capability constraint. A quota-walled Opus holds
judgment work instead of demoting it. The floor searches the worker pool first,
then the conductor pool, and falls back to the conductor model when neither
names an Opus-class member, so a policy whose pools carry no such model routes
judgment work to its strongest configured model and says so in the node's
stated reason. Configure an Opus-class worker pool member before enabling
intake on a repository whose issues carry `needs-thought`. Advisory classes other than `refine` have
no producer yet and park the task paused; phase 3 fills that hook.
Reviewer-driven class escalation and the verdict ledger are deliberately not
built in these phases (#3843).

A refine task runs one planner-class node with at most two attempts and has no
DAG. The node posts exactly one `## Agent brief` comment with `### Outcome`,
`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that order.
It applies `agent-ready` when no human decision remains. Otherwise it applies
`needs-human`, adds a final `### Question` section, and sends exactly one warn
notification. The server settles from a re-read of the issue label and comment,
never from the node artifact alone. A verified `needs-human` result succeeds
because the briefing and escalation completed. Two failed attempts settle the
task failed and apply nothing.

Before enabling `refine_enabled`, confirm the guest GitHub token has
`issues: write` on a fine-grained token, or `repo` on a classic one. Opening a
pull request needs `pull_requests: write` or `repo`. A fine-grained token
granted only `pull_requests: write` and `contents: write` can push and open pull
requests but receives 403 on refine comments and labels. That 403 becomes a
failed refine attempt rather than silent success because settlement re-reads
the issue. Set `FACTORY_EXECUTOR_LOGIN` to a non-empty GitHub login to require
the verified brief comment to come from that author.

The derived allowance is `max_attempts` summed over the live nodes that have
not succeeded, plus the work turns already spent, plus what the engine may still
insert on its own. Review rounds are reserved lazily, one round at a time: the
next round only, at the two turns it really costs, because the engine inserts
its correction and its re-review at one attempt each. A correction that fails is
a deviation the planner answers, not a turn to spend again. One attempt is a
deliberate trade: a correction or re-review that fails or stalls costs the
round and returns to the planner, which can re-add the work under the same
envelope, rather than retrying silently inside a round nobody sized for it.
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
cancellation request, or the turn timeout elapsed measured from dispatch. So a
strand or a stall caught early holds its reservation until that timeout passes
and self-heals there, rather than at the moment it is observed.

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
discarding or renaming a correction node cannot buy another one. The planner is
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
implementation node, the latest independent Opus review approving the current
head, and passing repository PR status checks. GitHub is read again to verify
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
