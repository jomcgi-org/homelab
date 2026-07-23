# EmberVM: auto-heal the checkpoint-abort quarantine (durable dispatch record)

Implements [ADR embervm/017](../decisions/embervm/017-checkpoint-abort-quarantine-auto-heal.md).

## Problem

After the abort-generation-blessing fix (`aa3df2cd5`), the only remaining trigger
of a stateful volume quarantine is `autoAbortCheckpoint`, noded's resolve-timeout
backstop (`projects/embervm/noded/server/stateful.go`). When the control plane
does not resolve an in-flight checkpoint in time, noded self-aborts: it
`BumpGeneration`s the volume (advances `genFile`, leaves the blessed marker
behind) and resumes the SAME paused VM. The node then reports
`generation_blessed:false` one past the blessed watermark, and
`StatefulStore.update_quarantine/4` quarantines the volume. Recovery is a manual
break-glass re-bless.

This case is positively benign (same `vm_id`, exactly `+1`) but the control plane
cannot prove it after the restart that triggered the auto-abort, because the
checkpoint-in-flight state is in-memory. ADR 017 lifts the deferral by making the
control plane durably record each checkpoint dispatch, so a recovered control
plane recognizes its own auto-aborted checkpoint and auto-blesses only the
provably self-inflicted `+1`. Everything else stays quarantined (fail-closed).

## Approach

A durable `checkpoint_dispatch` record `{workload, vm_id, generation}`, projected
into its own op-log table (mirrored across the SQLite and Postgres backends,
loaded into a store ETS table on boot). The control plane writes it when noded
confirms the VM is paused, clears it when the control plane drives the resolve,
and consults it in `update_quarantine` to auto-heal the matching `+1`.

Rejected alternatives (see ADR 017): a signature-only check (cannot fail closed
after the triggering restart); reusing the already-durable `:checkpointed`
instance row (couples correctness to volume-upsert vs instance-adoption ordering
within a NodeStatus). A new projection TABLE (not columns on `volume_blessing`)
is chosen because `CREATE TABLE IF NOT EXISTS` needs no migration for existing
op-log databases, whereas adding columns to a live table would.

## Tasks

### Task 1: Op-log kinds and dual-backend projection

- `projects/embervm/control/lib/embervm/op_log.ex`: add `:checkpoint_dispatched`
  and `:checkpoint_resolved` to the `@kinds` allow-list (near `:generation_blessed`,
  with a short doc comment explaining the pair: dispatched records an in-flight
  checkpoint's `{workload, vm_id, generation}`; resolved clears it).
- `projects/embervm/control/lib/embervm/op_log/sqlite.ex`:
  - New projection table `checkpoint_dispatch (workload TEXT PRIMARY KEY, vm_id
    TEXT NOT NULL, generation INTEGER NOT NULL, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL)`. One row per workload (the stop-serialization
    guard means one in-flight checkpoint per workload at a time), so a second
    dispatch UPSERTs.
  - `project/2` clause for `:checkpoint_dispatched`: UPSERT the row from
    `op.workload` + `op.payload.vm_id` + `op.payload.generation`.
  - `project/2` clause for `:checkpoint_resolved`: `DELETE FROM checkpoint_dispatch
    WHERE workload = ?`.
  - `load_checkpoint_dispatches/1` + `handle_call(:load_checkpoint_dispatches, ...)`
    + a `collect_checkpoint_dispatches/3` collector, mirroring
    `do_load_volume_blessing/1` / `collect_volume_blessing/3`. Returns rows as
    `%{workload, vm_id, generation}`.
- `projects/embervm/control/lib/embervm/op_log/postgres.ex`: the SAME table,
  `project/2` clauses, and `load_checkpoint_dispatches/1` for parity (this backend
  is what production runs; a missing clause here is a silent prod-only projection
  gap). Match the existing `volume_blessing` parity exactly.
- `projects/embervm/control/lib/embervm/op_log/compactor.ex`: confirm compaction
  never prunes an UNRESOLVED `checkpoint_dispatch` row (it must outlive the ops
  that created it, until its `:checkpoint_resolved`). If the compactor prunes
  projection tables by age, exclude this one or key its pruning on a matching
  resolved op. The record is tiny and short-lived, so the safe default is to
  leave it untouched by compaction.

### Task 2: Store — record, clear, and load the dispatch record

- `projects/embervm/control/lib/embervm/stateful_store.ex`:
  - Add a new ETS table (e.g. `:checkpoint_dispatch`) alongside `:volumes` /
    `:blessing`, created in `init/1` and rebuilt in `rebuild/1` from
    `op_log_mod.load_checkpoint_dispatches/1` (add the `load_...` to the `with`
    chain next to `load_volume_blessing`). Each row: `{workload, %{vm_id,
    generation}}`.
  - `record_checkpoint_dispatch/4 (store, workload, vm_id, generation)`: append a
    `:checkpoint_dispatched` op (write-through, like `bless_generation_append/3`)
    and, on success, UPSERT the ETS row. Payload `%{vm_id: vm_id, generation:
    generation}`. Principal `system:stateful:#{workload}` (match the blessing op).
  - `clear_checkpoint_dispatch/2 (store, workload)`: append a `:checkpoint_resolved`
    op and delete the ETS row. Idempotent (no row / no in-flight is a no-op that
    still appends a harmless resolved op, or short-circuits if absent, chosen for
    minimal op-log noise: short-circuit when the ETS row is absent).
  - A read helper `fetch_checkpoint_dispatch/2` returning `%{vm_id, generation}` or
    `nil`, used by `update_quarantine`.
  - Moduledoc: extend the "generation blessing and quarantine" section to describe
    the dispatch record and the auto-heal branch.

### Task 3: Store — auto-heal branch in `update_quarantine`

- `update_quarantine/4` (`stateful_store.ex`): when `quarantined?` would be true,
  BEFORE quarantining, consult `fetch_checkpoint_dispatch/2`:
  - Auto-heal iff a record exists for the workload AND `reported_gen ==
    record.generation + 1` AND the record's `vm_id` matches the workload's current
    sole live-or-checkpointed instance's `vm_id` (the resumed VM keeps its
    `vm_id`; a fresh second writer would be a different one). Look the instance up
    via the store's own instance table (the volume report carries no `vm_id`).
  - On a match: call `bless_generation_append(state, workload, reported_gen)` (the
    existing write-through path; it appends `:generation_blessed`, advances the
    watermark, and clears quarantine), then clear the dispatch record, and log an
    `event: :generation_auto_healed` structured line (workload, generation,
    previous blessed) so the heal is auditable and alertable, distinct from the
    `:generation_quarantined` warning.
  - On no match: quarantine exactly as today (the existing `:ets.insert` +
    `:generation_quarantined` warning path).
  - Keep the change inside the store GenServer process (direct `defp` calls, no
    `GenServer.call` to self).
- Note the monotonicity guard in `bless_generation` already refuses a regressing
  value, so a duplicate report that re-triggers the heal after the watermark
  advanced is a transparent no-op.

### Task 4: Sweeper — write the record on checkpoint, clear it on resolve

- `projects/embervm/control/lib/embervm/stateful_sweeper.ex`:
  - `finish_checkpoint/8` (the `{:ok, token, generation}` clause, where it already
    `mark_with(:checkpoint_ready, %{checkpoint_token, checkpoint_generation,
    vm_id})`): also call `StatefulStore.record_checkpoint_dispatch(store, workload,
    vm_id, generation)` with noded's reported checkpoint `generation` (the value
    the auto-abort will advance to `generation + 1`). This is the durable dispatch
    record. A record-append failure is best-effort logged, not fatal (the workload
    then falls back to the manual break-glass on a later auto-abort, correct
    fail-closed).
  - `apply_resolve/8` for BOTH `:commit` and `:abort` success clauses: call
    `StatefulStore.clear_checkpoint_dispatch(store, workload)` (the control plane
    drove this resolve, so its dispatch is resolved and must not linger to
    auto-heal a later unrelated `+1`). The `:error` clause that force-aborts
    (`apply_resolve(..., {:error, reason})` -> `:abort`) also clears via that abort
    path.
  - Confirm the clear also runs on the terminal/destroyed mid-checkpoint branch of
    `finish_checkpoint` (the `_ ->` release path), so a checkpoint that never
    reaches a normal resolve does not strand a dispatch record. If that path has no
    workload in scope, clear by whatever key is available or leave it for the
    compaction-safe short-circuit (document which).

### Task 5: Runbook update

- `docs/runbooks/embervm-stateful-generation-quarantine.md`: update Cause 2
  (`autoAbortCheckpoint` residual) to say the control plane now auto-heals this
  case on the next node report (matching a durable dispatch record), with the
  `:generation_auto_healed` log line as the signal. Keep the manual break-glass as
  the recovery for a quarantine that PERSISTS past the next report (the crash
  window where no dispatch record was durably written, or any genuinely ambiguous
  case). State plainly: a quarantine that does not clear itself within a node
  report interval is the real, rare, human-decision case.

### Task 6: Tests

- Store (`stateful_store_test.exs`):
  - `record_checkpoint_dispatch` then a would-quarantine report at `gen+1` with the
    matching `vm_id` auto-blesses (`quarantined?` false, watermark advanced) and
    clears the record.
  - A would-quarantine report at `gen+2`, or at `gen+1` with a DIFFERENT live
    `vm_id`, or with NO record, quarantines (fail-closed).
  - Auto-heal survives a store rebuild: seed the op-log with a
    `:checkpoint_dispatched` (no `:checkpoint_resolved`), rebuild the store, then a
    matching `gen+1` report auto-heals (proves the record is durable across a CP
    restart, the core ADR-017 property).
  - A `:checkpoint_resolved` op clears the record: after rebuild, the same `gen+1`
    report quarantines (no record to heal against).
  - Monotonicity: a duplicate auto-heal report after the watermark advanced is a
    transparent no-op.
- Op-log (both backends if a test seam exists, else the default backend):
  `checkpoint_dispatched` UPSERT then `load_checkpoint_dispatches` returns it;
  `checkpoint_resolved` removes it; the round-trip of `{workload, vm_id,
  generation}` is exact.
- Sweeper (`stateful_sweeper_test.exs`): `finish_checkpoint` records the dispatch
  (assert via an injected store recorder / the store API); `apply_resolve` for
  commit and abort clears it.

### Task 7: Chart bump

- Only the control binary changes (no noded change: the auto-abort self-bump lane
  is intentionally untouched). A new control image publishes, so bump the embervm
  chart with `bazel/tools/git/bump-chart.sh projects/embervm` in this PR
  (`Chart.yaml` + `deploy/application.yaml` together).

## Verification

- No local test loop: implement all tasks, commit, push, watch CI
  (`gh pr checks <n> --watch`). The op-log projection round-trip, the store
  auto-heal + rebuild-durability tests, and the sweeper record/clear tests run in
  CI; read failures via the BuildBuddy MCP tools and quote the assertion before
  hypothesizing.
- Post-merge, the live `demo-postgres` (or any workload) that hits a
  resolve-timeout auto-abort should clear its own quarantine on the next node
  report, emitting `:generation_auto_healed`, with no manual re-bless. A quarantine
  that persists is the fail-closed remainder and follows the runbook break-glass.

## Out of scope

- noded's `autoAbortCheckpoint` self-bump is deliberately unchanged: the residual
  it produces is exactly what this auto-heal recognizes. Removing the auto-abort
  (making checkpoints non-abortable without the control plane) is rejected in ADR
  017 (it trades a self-healing quarantine for a hard resource leak under CP
  outage).
- The `:generation_auto_healed` event is emitted for future alert wiring but this
  plan does not add an alert rule; that follows the existing quarantine-alert
  pattern (ADR 011 Task 16) if desired.
