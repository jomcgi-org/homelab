# EmberVM: bless the checkpoint-abort generation (fix perpetual volume quarantine)

## Problem

`jomcgi.dev/health` returned 503 because the ember demo-postgres stateful VM could not
wake: the control plane quarantined its volume (`:volume_quarantined`) on every wake, a
fail-closed loop with no auto-heal.

Root cause: the ADR-008 interruptible-bank ABORT path violates the R7 "control plane is
the sole issuer of a volume generation" model (ADR embervm/011, standing decision 4).
`Server.abortCheckpoint` (`projects/embervm/noded/server/stateful.go`) bumps the volume
generation with the legacy `volume.Manager.BumpGeneration` (a self-bump: advances
`genFile` past `blessedFile`, never touches the blessed marker) and returns an empty
`ResolveStatefulResponse{}`. From then on the node reports `generation_blessed:false` with
a generation above the CP's `blessed_generation`, and `StatefulStore.update_quarantine/4`
(`reported_gen > blessed_gen and not blessed_on_wire`) quarantines the volume. A
checkpoint-abort is a normal bank outcome (a parked connection needs the VM hot), so this
quarantines healthy volumes. `commitCheckpoint` is unaffected (it publishes at the
already-blessed `e.generation` and never bumps).

## Decision

Chosen: Option 2 (CP issues the abort generation up front), assessed and recommended by a
Fable design review over Option 1 (CP blesses after the fact). Option 1 was rejected
because blessing-by-response is the exact fence hole the wake path's op-log-before-dispatch
ordering exists to prevent: since the CP rolls on every chart bump and a roll kills the
in-flight resolve worker, Option 1 re-creates this quarantine outage on every deploy that
races an in-flight abort. Option 2 durably records the generation via its sole issuer
BEFORE any node-side state advances, so no CP crash, roll, or lost RPC response can
re-create the quarantine.

## Tasks

### Task 1: Proto — add `blessed_generation` to `ResolveStatefulRequest`

- `projects/embervm/proto/embervm/node/v1/node.proto`: add `uint64 blessed_generation = 5;`
  to `ResolveStatefulRequest` (fields 1-4 taken; `ResolveMode mode = 4` is last). Document
  it: the CP-issued generation the node records for an ABORT resume (mirrors
  `StartStatefulRequest.blessed_generation`); zero means the legacy self-bump lane (only
  `autoAbortCheckpoint`, see Task 5). COMMIT ignores it.
- Codegen is build-time only (nothing checked in); no generated files to touch. The
  cross-language round-trip test proves wire-correctness.

### Task 2: noded — abort records the CP-blessed generation

- `projects/embervm/noded/server/stateful.go`:
  - Thread `blessedGeneration uint64` into `abortCheckpoint` from
    `req.GetBlessedGeneration()` at the `ResolveStateful` RPC handler.
  - When `blessedGeneration > 0`: call `s.volumes.RecordBlessed(workload, blessedGeneration)`
    instead of `BumpGeneration`, so `genFile == blessedFile` and `GenerationBlessed` reports
    true. Order stays ADR-008 mandated: record → delete-temp → resume (the guest is PAUSED
    until `ResolveStatefulAbort`, so recording before resume is correct).
  - Return the recorded generation in `ResolveStatefulResponse.Generation`.
  - On `RecordBlessed` error, mirror today's `bumpErr` fallback: log and proceed on the
    current generation (the resulting unblessed flag then correctly signals genuine drift).
  - `blessedGeneration == 0` (autoAbortCheckpoint): keep `BumpGeneration` (self-bump). This
    is the residual fail-closed case, accepted per Task 5.

### Task 3: CP — bless before dispatching an abort; force-commit on append failure

- `projects/embervm/control/lib/embervm/stateful_sweeper.ex`:
  - When `decide_resolve` selects `:abort`, bless `next_blessed_generation` via
    `StatefulStore.bless_generation/3` (op-log-before-dispatch, matching the wake path's
    fence ordering) and thread the blessed generation into the dispatched
    `ResolveStatefulRequest`.
  - Amendment 1: if the bless op-log append fails, force `:commit` instead of dispatching an
    unblessed abort (commit invents no generation and is always ledger-safe; the parked
    caller relights, slightly colder, off the fresh bundle).
  - `apply_resolve(:abort, ...)`: the ledger is already advanced by the pre-dispatch bless,
    so the returned generation is a confirmation; log/verify, no second bless needed.

### Task 4: CP store — monotonicity guard on `:bless_generation`

- `projects/embervm/control/lib/embervm/stateful_store.ex`: Amendment 3 — the
  `:bless_generation` handler blindly overwrites `blessed_generation`; add a `>=` guard so a
  late/stale value can never regress the watermark. The wake path always blesses
  `next_blessed_generation` (strictly greater), so the guard is transparent there.

### Task 5: Runbook + ADR note for the `autoAbortCheckpoint` residual

- `autoAbortCheckpoint` (the resolve-timeout backstop) has no CP RPC, so it self-bumps with
  `blessedGeneration == 0` and can still quarantine. Accept this as CORRECT fail-closed
  behaviour (a resume the CP ledger genuinely never witnessed) rather than papering over it.
  Option 2's pre-dispatch bless already absorbs the common case (the CP pre-blessed before
  the node's timeout fired, so the report reads `G+1 == watermark G+1` and the strict-`>`
  quarantine predicate does not fire).
- Add a runbook entry: how to recognize a `:generation_quarantined` warning for a workload,
  and the break-glass recovery (bless the reported generation forward to clear it).
- Note the decision-4 scope extension (abort lane now issues a CP-blessed generation) in ADR
  008 and/or 011 as a short amendment.

### Task 6: Tests

- Proto round-trip test covers the new field automatically; extend if it asserts field sets.
- noded: `abortCheckpoint` with a nonzero blessed generation records via `RecordBlessed`
  (blessed marker advances, `GenerationBlessed` true) and returns it; with zero it
  self-bumps (unchanged).
- CP sweeper: `:abort` blesses `next_blessed_generation` before dispatch and threads it;
  bless-append failure forces `:commit`.
- CP store: `:bless_generation` refuses to regress below the current watermark.

### Task 7: Chart bump

- Both binaries (noded + control) change, so new images publish; bump the embervm chart with
  `bazel/tools/git/bump-chart.sh projects/embervm` in this PR (Chart.yaml +
  deploy/application.yaml together).

## Verification

- No local test loop: implement all tasks, commit, push, watch CI
  (`gh pr checks <n> --watch`). The cross-language proto round-trip test and the noded/CP
  unit tests run in CI.
- The live demo-postgres remains quarantined until a separate break-glass re-bless (the code
  fix prevents recurrence; it does not clear the existing quarantine).
