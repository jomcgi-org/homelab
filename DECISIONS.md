# EmberVM R0: deviations and judgment calls

A running log of where the implementation departs from, tightens, or resolves an
ambiguity in `docs/plans/2026-07-12-embervm-r0-tasks-spec-and-plan.md`, so the
spec and the code can be reconciled retrospectively. Each entry says WHAT changed,
WHY, and whether it was approved. Newest phase last.

Deviations here are intentional. Bugs are not deviations; they get fixed.

## Task 12: metering, audit, and quotas (chart 0.1.14)

### D12.1 Usage rides the existing `:succeeded`/`:failed` append (no `:metered` op, no flush timer)
- **Spec said:** "aggregate in ETS, flush to the op-log on interval and drain."
- **Did instead:** carry the billed usage in the `:succeeded`/`:failed` op payload
  and upsert a `(principal, day)` row in the `usage` projection INSIDE the same
  SQLite transaction as that op. No new op kind, no flush timer, no ETS
  pending/flush split. `Embervm.Metering` owns only a read-through quota cache
  (integer `cpu_ms` per `(principal, day)`), rebuilt from the projection on boot.
- **Why:** every task completion already makes one durable fsynced op-log append
  (the FSM requires it and it cannot be removed). Riding it satisfies the spec's
  real constraint ("MUST NOT add a blocking store write") maximally, adding zero
  writes, and eliminates three failure modes the interval-flush design carries:
  flush atomicity, mid-interval crash loss, and cache/durable divergence. The
  usage record is also per-task (carries `task_id`), finer than interval deltas.
- **Approved:** yes (design pause + Fable review).

### D12.2 Denials appended by cadence class, not "every denial"
- **Spec said:** "every denial (quota, cap, auth, stale-capacity) is an op-log
  append with principal and reason."
- **Did instead:** only REQUEST-scoped denials are appended, once each: quota
  (`:quota_enforced`), auth-forbidden 403 (`:denied`), and per-principal
  queue-depth 429 (`:denied`). DISPATCH-tick saturation conditions
  (`:cap`/`:stale_capacity`/`:no_capacity`/`:principal_share`) stay in-process
  counters exposed via `Dispatcher.stats/0`, NOT appended. Unauthenticated 401s
  are NOT appended.
- **Why:** saturation conditions are re-evaluated every ~5s drain tick while they
  persist; appending per tick would flood the log, and they have no principal to
  attribute (so they cannot satisfy "with principal and reason" anyway). 401s are
  reachable by any unauthenticated caller, so appending them turns unauthenticated
  HTTP spam into durable-write amplification against the single-writer op-log.
- **Approved:** yes.

### D12.3 Quota enforced at submit AND dispatch; over-quota tasks are parked, not failed
- **Spec said:** quota "denies dispatch," fail-closed.
- **Did instead:** a submit-time 429 (courtesy fast-fail, appends the audit
  record) PLUS the actual enforcement in the dispatcher's `fq_take`: an
  over-budget principal is skipped in the fair rotation and its task stays
  `queued`, unparking when the daily budget resets. No task is failed for quota
  (the FSM has no `queued -> failed` edge).
- **Why:** submit-only enforcement has a real hole (totals move only on success,
  so a principal at 99% can burst `queueDepthCap` submits that all pass the gate
  and, with no drop edge, all run: bound `= queueDepthCap x max_task_cost`). The
  dispatch-side skip bounds it to one in-flight share. Parking rather than failing
  respects the Task 11 FSM constraint.
- **Approved:** yes (Fable caught the submit-only hole).

### D12.4 Failed-with-usage is charged, not only success
- **Spec implied:** usage is captured from `AssignResponse` on the success path.
- **Did instead:** a guest 4xx/5xx returns a well-formed `AssignResponse` WITH
  usage (it did measured work), so it is billed and counts against quota.
  Transport/timeout failures report no usage and charge nothing.
- **Why:** otherwise a workload that burns CPU then returns 500, amplified by
  retries, is free. The fix is a few lines given D12.1's payload carry.
- **Approved:** yes.

### D12.5 Quota is opt-in (empty = off), asymmetric to the auth allow-list
- An empty budget map means quota is OFF (a principal with no configured budget is
  allowed), NOT deny-all. "Fail-closed" is scoped to a principal that HAS a
  budget: only then does an unreadable cache deny. This keeps a Metering crash
  from bricking dispatch on a cluster that never opted into quotas. The auth
  allow-list stays deny-all (a security gate); quota is a resource-abuse gate.
  CRD-based `Quota` objects remain the follow-on; v1 budgets are values-configured.
  A budget of exactly `0` is a HARD STOP (denies the principal entirely), consistent
  end to end: the values parser accepts `0`, the Helm default guard renders `0`, and
  the runtime gate (`used < budget`) denies at `0`. Omit a principal for unlimited.

### D12.6 Daily budget = UTC epoch-day
- The `(principal, day)` key uses `div(op.ts, 86_400_000)` on the op's wall-clock
  ms, so the daily reset is 00:00 UTC (~16:00-17:00 US Pacific). Documented in
  `chart/values.yaml`. Charged to the SUCCESS day (a task crossing midnight bills
  the day it completed).

### D12.7 `Auth.authenticate` surfaces the username on forbidden
- Return changed from `{:error, :forbidden}` to `{:error, {:forbidden, username}}`
  so the router can name the principal in the 403 audit append. The router still
  accepts a bare `:forbidden` (principal unknown) for reviewers/fakes that do not
  surface it.

### D12 known gaps accepted for R0 (documented, not fixed)
- The `usage` projection ACCUMULATES (the only projection that does), so it is not
  idempotent under op replay (the future `read_from` replica path); safe today
  because R0 projects each op exactly once. Commented at the projection.
- Over-budget parked tasks can outlive their `expires_at` (queued-task TTL is not
  enforced; `compact` only prunes terminal rows).
- `gb_seconds = peak_rss_mib x wall` under-bills vs a Firecracker VM's reserved
  `memMib`; the raw per-task `peak_rss_mib`/`cpu_ms`/`wall_ms` are stored in the op
  payload so the formula can be rebased later without losing history.
- A daemon that never populates `UsageStats` bills zero (proto3 defaults);
  mitigated by a log-once warning on all-zero success usage and a non-zero
  assertion in the metering test.
