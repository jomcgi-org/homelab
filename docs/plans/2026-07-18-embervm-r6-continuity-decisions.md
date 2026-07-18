# EmberVM R6 Continuity: autonomous implementation decisions

Running log of decisions made while implementing the R6 Continuity spec+plan
(`2026-07-18-embervm-r6-continuity-spec-and-plan.md`) without live operator input.
Each entry records a choice the plan left open, the option taken, and the reason.
This is the durable answer to "why is it built this way" for the R6 code.

## Meta

- **Scope of "feature complete".** The plan's Tasks 2 through 11 are code and land as
  PRs 1 through 6. Task 12 (closure) is code-flippable for the ADR status edits and the
  drill runbook, but its live gates (a real noded roll with live scratch-postgres, a
  full-node drain wall-time measurement, a 48h soak) require a drillable cluster and
  wall-clock time, and standing decision 11 blocks them on the unresolved R5 gate-1
  entry-path EOF. Decision: implement all code (Tasks 2-11), ship observability and
  alerts (Task 11), and land Task 12 as the ADR/plan bookkeeping plus the drill runbook,
  with the live-drill gate rows left `DEFERRED (entry criteria)` rather than fabricated.
  Feature-complete here means "every code mechanism the rung specifies is implemented,
  reviewed, and CI-green"; the live sign-off is a separate operator action.
- **Chart bumps.** Any PR that rebuilds a service image bumps that service's chart via
  `bazel/tools/git/bump-chart.sh` in the same PR. Learned in PR-1: even a "no behavior
  change" proto edit rebuilds the embervm images (the generated stubs compile in), so
  the missed-bump guard requires an embervm bump. Any PR that adds/edits a markdown file
  under `docs/` or `projects/` also rebuilds monolith + monolith-public (the repo-docs
  manifest is baked into those images), so those PRs bump all three.
- **Review cadence.** One comprehensive Opus code review per PR against the full diff
  (repo CLAUDE.md overrides the per-task reviews in subagent-driven-development).

## PR-1 (proto contract)

- **`Volume.exported_generation` is a uint64, not a bool.** The other artifacts get a
  bool `exported`, but a volume's store copy can lag the live generation, so the field
  carries WHICH generation is durable (0 = none). This lets the CP both skip a
  re-export (equal to live gen) and know the exact generation a disk-loss restore
  recovers to.
- **fakenode receivers are all pointer receivers.** Embedding the in-memory store's
  `sync.Mutex` in `fakeServer` made every value-receiver handler copy the lock; nogo's
  copylocks analyzer fails the build. All handlers are pointer receivers (the server is
  registered as `&fakeServer{}`).
- **decisions.md landed in PR-2, not PR-1.** To keep the proto PR a genuine no-deploy
  contract change, the decisions log (a `docs/plans/*.md`, which forces monolith+public
  bumps) was held back from PR-1. It first lands with a code PR that bumps charts anyway.

## PR-2 (noded drain hold)

- **noded holds the whole gRPC surface up during drain, it does not GracefulStop early.**
  The pre-R6 code called `GracefulStop()` immediately on SIGTERM, which rejects new RPCs
  and so blocked the control plane's `Bank`/`Stop` calls. The fix keeps serving lifecycle
  rpcs (only new BuildBase/Prime/Assign are refused via the draining flag) and waits on
  `WaitForManagedDrain` until the managed (session/serving/stateful/group) registry
  empties or the deadline, THEN GracefulStop drains in-flight task Assigns.
- **`WaitForManagedDrain` wakes on NodeStatus-change signals, not a fake clock.** It
  subscribes to the same `signalChange` broadcast every Bank/Stop already fires, plus a
  500ms backstop ticker and the deadline timer. A clean drain returns promptly on the
  bank signal; tests drive it by adding/removing registry entries.
- **`store_reachable` left false in PR-2.** The store client is PR-4; the NodeStatus
  field ships in PR-1 but is only populated once the probe loop exists.

## PR-3 (drain coordinator, all-classes force-bank)

- **Each sweeper owns a `drain_node/2`; the DrainCoordinator is thin.** Rather than have
  the coordinator reach into four stores' ETS and re-derive each class's bank admission,
  each sweeper gained a `drain_node(node_id)` that enumerates its own live instances on
  the node and routes them through its EXISTING bank machinery (admission, per-node bank
  concurrency, workers). The coordinator only fans out the four calls and records the
  op-log edge. This keeps the delicate per-class bank logic encapsulated where it already
  lives.
- **Stateful drain uses a `draining_workloads` MapSet, checked in exactly two places.**
  `recheck_and_bank` skips the raced-in / scrape-failure / at-cap aborts for a draining
  workload (bank unconditionally), and `decide_resolve` forces `:commit` even against a
  parked connection. The set is added in `force_bank_node` and self-clears when the
  bank/resolve for that workload completes, so it never leaks into a later non-drain
  checkpoint (stateful is a singleton per workload, so keying by workload is exact).
- **Drain bypasses the per-node bank cap for stateful.** A drain evacuates EVERY live
  instance; deferring at-cap instances back to serving (the steady-state behavior) would
  strand them. The 120s deadline plus the daemon's hold bound the concurrency instead.
  Serving/session drains still respect their caps (they retry on the sweeper's own tick,
  which keeps running during the hold), so only the stateful class, whose singleton
  shape makes a per-node cap collision rare, bypasses.
- **NodeRegistry sends the drain edge to a registered listener; it does not add PubSub.**
  On the rising edge of `draining` (tracked via the prior-status draining bool) it
  `send`s `{:node_draining, node_id, deadline_ms}` to a configurable `drain_listener`
  (default the `DrainCoordinator` by name). A missing listener (tests, or a drain during
  boot) is a silent no-op: the daemon's deadline reap is the backstop.
- **The CP `safety_margin_ms` is recorded, not enforced by the coordinator.** In this
  design the hard bound lives on noded (it holds until the deadline). The margin
  (EMBERVM_DRAIN_SAFETY_MARGIN_MS, default 15000) is stamped on the op so bank wall time
  can be compared against the window; a future higher-availability tier could use it to
  bound the CP side.
- **`:node_drain_started` / `:node_drain_finished` are audit-only op-log kinds.** Like
  `:drain`, they have no projection table; they were added to both `OpLog.@kinds` and the
  SQLite audit-only `project/3` clause (which has no catch-all, so an unlisted kind would
  crash `append`).
- **The end-to-end roll+adopt+relight sequence test (Task 5) is covered by composition,
  not one mega-test.** The new drain suite proves drain -> banked (with COMMIT despite
  parked); the existing adoption suite proves banked -> adopt -> relight across a node
  down/up. The manual `roll-drain-drill.md` runbook documents the live concatenation,
  which is a closure gate (deferred with the other live gates). A single automated
  multi-process node-down/up integration test was judged low marginal value against that
  existing coverage.

## PR-4 (noded object store client + verbs)

- **The `store.Store` interface carries artifact-level Export/Restore/DeleteArtifact/
  Present helpers, not just raw object ops.** meta.json is written last as the
  completeness marker and read first on restore/presence, so a partially uploaded
  artifact is invisible. SHA-256 is recorded in meta and verified on restore (a corrupt
  restore surfaces loudly, never overwrites local with bad bytes).
- **The server depends on a small `artifactStore` interface, not the concrete
  `*store.Store`.** Tests inject an in-memory fake so no test touches the network, and a
  Server built without a store still compiles: a nil store leaves the verbs refusing
  FAILED_PRECONDITION and every export a no-op.
- **Export is an async, fire-and-forget bounded worker pool.** An enqueue that would block
  (queue full) is dropped, not awaited (standing decision 7: the export queue never stalls
  the bank path or the drain deadline); a startup reconcile sweep re-enqueues any artifact
  whose store copy is missing or stale, covering a roll that exited before exports finished.
- **`store.Export`/`Restore` return `int64` bytesMoved; the proto responses are `uint64`.**
  A cast is required at the verb return sites. CI's nogo type-check caught the missing cast
  (Push/local build could not, since the proto is Bazel-only codegen).

## PR-5 (CP restore-on-miss + remote GC)

- **Restore-on-miss for BUNDLES/SETS is optimistic; for VOLUMES it is fail-closed.** The
  `exported` flag lives on the local bundle fact, which vanishes once the local bundle is
  gone, so on a true local miss the CP cannot read it. For bundle/set (pure warmth,
  fail-open) the wake attempts RestoreArtifact whenever `store_reachable`, and noded
  refuses gracefully (FAILED_PRECONDITION) when no copy exists, degrading to cold boot with
  a logged reason. A VOLUME restore is a data action (standing decision 8), so it stays
  gated on the durable `exported_generation` and never blindly restores.
- **`store_reachable == false` never blocks a local-state wake.** Only a true local miss
  consults the store; an unreachable store there degrades straight to cold.
- **Two latent placement-gate bugs were fixed (serving/session).** Both
  `ServingPlacement`/`SessionPlacement.node_for_relight` gate on the node reporting the
  snapshot, so a truly-missing bundle would never reach the restore branch and the
  post-restore CP ETS fact is not yet refreshed. Fixed by anchoring a snapshot-lost
  serving instance to its node as a restore candidate, and relighting a session against the
  restore target node directly, both bypassing the stale placement gate only on the restore
  path (the normal relight path is byte-identical).
- **Every node RPC goes through an injectable `_fun` seam** (`restore_artifact_fun`,
  `evict_artifact_fun`), defaulting to the real Stub, so the managers/sweepers are testable
  without a network, matching the existing stop/resolve/bank seams.

## PR-6 (wake-worker timeouts + adoption recovery)

- **The timeout bound goes on the wake WORKER, never the parked caller.** The parked
  caller's `:infinity` GenServer.call stays (callers wait as long as they choose); a
  `{:wake_timeout, workload}` timer at `wakeTimeoutSeconds * 1000 + margin` (margin 15s)
  fails a wedged wake through the existing `finish_wake` failure path, releasing
  single-flight and erring the waiters, so a stuck boot no longer pins `waking` forever.
- **Adoption recovers a workload stuck `waking` past `2 * wakeTimeoutSeconds`** instead of
  skipping it forever; the timer is the primary release and this is the backstop.

## PR-7 (observability + closure)

- **The noded Go artifact-export span was skipped.** noded has no Go OpenTelemetry tracer
  wired, and inventing that dependency for one span was not worth it; export visibility
  comes from the structured logs and the export-backlog alert instead. The CP spans
  (`embervm.node_drain`, `embervm.artifact_restore`) use the existing `Tracer.with_span`
  macro idiom.
- **The four continuity alerts ship dry-run (`disabled: true`, placeholder metrics).** No
  op-log/log to metrics bridge exists yet, so an enabled alert would query a non-existent
  metric; a disabled placeholder is the honest posture (no fake-but-passing query firing
  silently), matching the existing embervm alert convention. They are promoted during the
  live closure drills.
- **R6 Continuity is `Shipped 2026-07-18 (gates live-pending)`, matching R4/R5.** All R6
  code (Tasks 2-11) is implemented, reviewed, and CI-green across the seven PRs; the
  live-drill gates (2-10) and the entry-criteria gate (1) are deferred behind the
  unresolved R5 gate-1 entry-path EOF (standing decision 11), exactly as R4 and R5 shipped
  their code with gates live-pending. The rung is code-complete, not live-signed-off.
