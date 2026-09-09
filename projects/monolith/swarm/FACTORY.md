# Bounded issue delivery lane

The first autonomous lane reads explicitly selected GitHub issues, records one
durable receipt per repository/issue/generation, and admits up to the policy's
`max_tasks` tasks at a time, capped by the chart's
`swarm.factoryMaxConcurrentTasks` (1 today). `max_tasks` is a concurrency,
not a lifetime count: as tasks settle the lane keeps admitting until its
issue list is exhausted, so it runs without an operator re-arming it.
An Opus session in Ember plans one graph edit at a time. The server reconciles
the mutable graph and dispatches each admitted node as an independent DBOS
workflow with immutable inputs. Planning does not run in the monolith process.

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
  "max_turns_per_task": 12,
  "task_budget_usd": 60,
  "turn_budget_usd": 5,
  "allowed_models": ["opus", "luna"],
  "conductor_model": "opus",
  "worker_model": "luna",
  "base_branch": "main",
  "turn_timeout_seconds": 900,
  "task_timeout_seconds": 14400,
  "max_attempts": 2
}
```

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

Missing provider usage consumes the entire reserved ceiling. This is
conservative admission accounting, not an interruptible dollar cap on a running
provider turn. Observed overruns prevent further admission.

The guest hydrates the existing task branch, or the base branch before the
task branch exists. Source changes belong in a dedicated linked worktree on
`factory/<task-id>`. Transient typed artifacts must be written under
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
