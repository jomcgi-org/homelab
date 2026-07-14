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

## Task 14a: first live sandbox VM (chart 0.1.15)

The plan's Task 14 lands semgrep + sandbox side-by-side with node-4 rebalancing.
This is split: Task 14a gets ONE real python-sandbox microVM running end to end
(the headline: prove EmberVM boots a real Firecracker VM through the controller),
Task 14b does semgrep + the fc-invoke concurrency rebalance + finding-equality.

### D14a.1 Split Task 14 into 14a (live sandbox) and 14b (full side-by-side)
- **Why:** the gating risk is the first real Assign (rootfs provisioning, FC cold
  boot, snapshot, vsock), not breadth. Getting one sandbox VM live de-risks the
  whole chain with the smallest surface; semgrep + rebalancing is mechanical once
  it works. 14a keeps a small footprint (floor 1, cap 4) that fits node-4's
  headroom WITHOUT touching fc-invoke, so no coexistence-budget change is needed
  yet (14b does the 16->8 fc-invoke halving for the full-cap run).

### D14a.2 noded rootfs provisioning ported from fc-invoke (was missing)
- noded had a complete Firecracker driver but NO rootfs provisioning: `EMBERVM_
  NODED_IMAGES` pointed at a pre-baked ext4 that nothing produced. Added a
  rootfs-builder initContainer + ConfigMap (crane export + mkfs.ext4 onto the
  nvme scratch), ported from the proven fc-invoke pattern, reusing fc-invoke's
  rootfs-builder tool image. Idempotent via a `.guest-ref` marker.

### D14a.3 EMBERVM_NODED_IMAGES is DERIVED, not a hand-maintained map
- The guest image_ref must match in three places (rootfs-builder GUEST_IMAGE, the
  noded image table key, the Workload CR source.image.ref) or BuildBase fails
  FAILED_PRECONDITION. Rather than hand-sync a static `noded.images` map key to
  the Bazel-pinned ref, the deployment DERIVES the table from `noded.workloads` +
  each workload's `noded.<name>.guestImage`, so one pinned value flows to all
  three consumers. Explicit `noded.images` entries still merge as overrides.

### D14a.4 Guest image digest-pinned via Bazel, contract frozen
- `noded.sandbox.guestImage` and `noded.rootfsBuilder.image` are pinned by
  `helm_chart(images=)` from the fc-invoke guests' public `.info` providers (zero
  image changes, per the plan). The sandbox guest contract is used verbatim: vsock
  port 1027, `/shim/ready`, one python snippet per `/invoke`, `sandbox-guest-init`.

### D14a.6 helm_images_values collides on shared-prefix dotted keys (chart 0.1.16 fix)
- First live deploy (0.1.15) CrashLooped the rootfs-builder with `MANIFEST_UNKNOWN`
  pulling `sandbox/guest:latest`: the Bazel image pin did not apply, leaving the
  values default. ROOT CAUSE: `bazel/helm/images.bzl` emits one top-level YAML
  block per dotted image key, so three keys sharing a prefix (`noded.image`,
  `noded.sandbox.guestImage`, `noded.rootfsBuilder.image`) produce three duplicate
  `noded:` keys that collapse to the last on parse, dropping the guest-image pin
  AND clobbering `noded.image` itself back to `:latest`.
- Fix: give each Bazel-pinned image a DISTINCT top-level key (`sandbox.guestImage`,
  `rootfsBuilder.image`, flat top-level `workloads`), mirroring fc-invoke's proven
  layout. `noded.image` stays the sole `noded.*` pinned key. The underlying
  images.bzl limitation (silent clobber on shared-prefix keys) is a latent footgun
  worth a general fix (deep-merge the fragment) as a separate follow-up.

### D14a.5 Live-verify, not CI-verify
- CI cannot run a real VM (no KVM in RBE). 14a is verified live AFTER merge+sync:
  the init container bakes the ext4, the Workload goes Ready with a snapshotRef
  (BaseBuilder cold-booted + snapshotted a real VM), the PoolManager primes a
  floor VM, and a `/invoke` submit runs python end to end. Recorded here as the
  acceptance evidence once observed.

## D12 known gaps accepted for R0 (documented, not fixed)
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
