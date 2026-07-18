# EmberVM R6 (Continuity) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or superpowers:executing-plans in a separate session) to implement this plan task-by-task.

**Goal:** A routine noded or control-plane roll never loses data and never cold-boots a stateful workload: everything bankable is banked within a bounded 2-minute preemption window, banked state and volumes are durable off node behind a configurable S3-API endpoint, and the resilience gaps found in the R5 drills are closed.

**Architecture:** Three mechanisms. (1) Drain becomes active: on SIGTERM noded publishes a drain deadline through the existing `draining` NodeStatus flag, and the control plane responds by force-banking every live workload (stateful via the ADR 008 checkpoint/commit machinery, groups as bundle sets, sessions and serving via their bank verbs) before the pod exits. (2) Durability moves off node: three new node verbs (generalized from ADR 003's ExportBase/RestoreBase/EvictBase) copy banked bundles, bundle sets, and volume/generation pairs to an S3-API object store (SeaweedFS first backend) as async write-back after bank commit, with restore-on-miss in the wake paths. (3) The R5 wedge amplifiers (unbounded wake workers, adoption skipping `waking`, park overflow) get timeouts and self-recovery.

**Tech stack:** Go (noded, stdlib net/http S3 client, no SDK), Elixir (control plane), protobuf (additive only), Helm chart, SeaweedFS S3 gateway.

**Design doc:** `docs/plans/2026-07-18-embervm-r6-continuity-design.md`

---

## Standing decisions (do not relitigate)

1. **R6 Facade (etcd shim, virtual control planes, hard multi-tenancy) is deferred.** ADR embervm/009 demotes it from "Future ADR" to "Recorded" with a revival trigger (real demand for virtual control planes or external tenants). The ladder gains R6 Continuity, R7 Consumers, R8 Packaging.
2. **Availability contract v1 is spot-instance semantics with a 2-minute preemption bound.** A roll gives workloads up to 120 seconds of drain notice. The invariant is "state is always durable and a routine roll never loses data", not "rolls are seamless". This deliberately softens R4's "a long-lived connection is never severed" to "never severed except by preemption, with a 2-minute bound"; ADR 009 records the change. A higher-availability tier (live migration, overlapping noded generations) is future work, not v1.
3. **The control plane drives the drain; noded only holds the door.** noded has no op-log and no lifecycle authority. On SIGTERM it sets `draining`, publishes a deadline, and waits for its live-VM registry to empty (or the deadline); the CP observes `draining` on the existing WatchNode stream and runs the force-bank pass. This preserves the ADR 001 rule: the control plane acts exactly when a lifecycle action is needed.
4. **Force-bank overrides the idle predicate and the parked-connection abort.** During drain, stateful checkpoints resolve to COMMIT even if a connection is parked (spot semantics; the parked caller re-wakes against the new noded). The flap guard does not apply to drain-initiated banks.
5. **Durability contract is a configurable S3-API endpoint.** Endpoint, bucket, and optional credentials are chart values; SeaweedFS (`seaweedfs-s3.seaweedfs.svc.cluster.local:8333`, anonymous in-cluster) is the first backend. Client is stdlib `net/http` (PUT/GET/DELETE), matching the zip-lane fetch pattern; no aws-sdk-go, no SigV4 in v1 (seam left for creds later). Block-device replication is a recorded future optimization.
6. **Export happens only at bank commit, never against a live VM.** A bundle (or bundle set, or volume) is exported after the VM is down and the artifact is crash-consistent. Volume export ships the (vol.img, gen) pair atomically (gen sidecar written last) and is skipped when gen is unchanged since the last export.
7. **Export is async write-back and never blocks the bank path or the drain deadline.** A roll that exits before exports finish is safe: local disk still holds the artifacts and the next noded exports on reconcile. Restore-on-miss is the read path: wake prefers local disk, falls back to the store, cold-boots only if both miss.
8. **Eviction stays control-plane-driven and generation-safe.** EvictArtifact on the store follows the same rules as local eviction (superseded generation, banked TTL); the store is never the only copy of a volume generation that a banked bundle still pairs with.
9. **All proto changes are additive.** New verbs and NodeStatus fields only; no renumbering, no semantic change to existing verbs.
10. **Taps and bridges stay ephemeral.** They die with the pod netns and are re-created idempotently (existing behavior); continuity is about state, not network plumbing.
11. **Entry criteria: the R5 gate-1 entry-path EOF must be resolved before the closure drills run.** Continuity drills are unprovable while the baseline composite drill cannot pass. Gates 2-10 of R5 may proceed in parallel with R6 implementation but the R6 closure table depends on a drillable system.

## Settled forks

**Fork 1: who orchestrates drain.** (a) noded self-banks on SIGTERM; (b) CP reacts to the `draining` flag; (c) a k8s preStop hook calls a CP drain endpoint. Chose (b): noded lacks lifecycle authority and op-log access, and (c) adds an HTTP surface for what WatchNode already carries. The only noded change is holding shutdown until live VMs are gone or deadline.

**Fork 2: verb shape for off-node artifacts.** (a) ADR 003's base-only ExportBase/RestoreBase/EvictBase; (b) one typed verb family over all artifact kinds. Chose (b): `ExportArtifact`/`RestoreArtifact`/`EvictArtifact` with an `ArtifactRef {kind: BASE|SESSION|SERVING|STATEFUL|GROUP_SET|VOLUME, ref, workload}`. Bases are one kind; ADR 003's contract (idempotent per key, CP-driven, evict refuses while referenced) applies to every kind. This satisfies ADR 003 without a second verb family later.

**Fork 3: store layout.** Single bucket `embervm`, key scheme `<kind>/<workload>/<ref>/<file>` mirroring the on-disk layout under `EMBERVM_NODED_SNAPSHOT_ROOT`, plus a `meta.json` per artifact (sizes, generation, created-at) written last as the completeness marker. Mirroring disk keeps restore a pure copy and makes store GC auditable against local scans.

**Fork 4: where restore-on-miss hooks in.** (a) CP checks store state and issues RestoreArtifact before StartStateful; (b) noded transparently falls back to the store inside StartStateful. Chose (a): the CP already plans wakes from ETS facts (`plan_wake`), and node-reported store state keeps noded a dumb executor. NodeStatus gains a `store_reachable` boolean and artifacts gain a `remote` flag in the CP projection.

## Cross-cutting constraints

- Never break the hit/miss invariant: exports, restores, and drain actions are lifecycle actions; the request hot path is untouched.
- Fail closed on enforcement, fail open on warmth: a missing or unreachable store degrades to "bank locally, export later, cold-boot on true miss", never to refusing wakes for workloads whose state is local.
- Isolation: store keys are namespaced by workload; no artifact is ever restored into a different workload's lineage. No cross-principal sharing.
- Conventional Commits; every deploy-affecting PR bumps the embervm chart via `bazel/tools/git/bump-chart.sh projects/embervm`.
- No em-dashes in any authored text.
- fakenode BUILD has `# gazelle:ignore`: new gRPC imports need manual `deps` edits.
- All tests run in CI only (push the branch, watch BuildBuddy); no local `bazel test`.

## Suggested PR partitioning

| PR | Tasks | Deploys |
| --- | --- | --- |
| PR-0 | Design doc + this plan + Task 1 (ADR 009, ADR 001 ladder edit, index row) | docs only (monolith manifest bump) |
| PR-1 | Task 2 (proto: verbs, ArtifactRef, NodeStatus fields, fakenode) | no behavior change |
| PR-2 | Task 3 (noded drain hold + deadline publish + chart grace bump) | noded |
| PR-3 | Task 4 (CP DrainCoordinator force-bank pass) + Task 5 (post-roll relight/park drill hooks) | control plane |
| PR-4 | Task 6 (noded store client) + Task 7 (Export/Restore/Evict verbs + export-after-commit) | noded |
| PR-5 | Task 8 (CP export driver + restore-on-miss) + Task 9 (store GC/retention) | control plane |
| PR-6 | Task 10 (wake timeouts + adoption self-recovery) | control plane |
| PR-7 | Task 11 (observability + alerts) + Task 12 (closure: drills, gates, ADR status flip) | both |

---

## Phase 0: Decision record and contract

### Task 1: ADR embervm/009 and the ladder edit

**Why:** The roadmap change is a decision; it needs a record before code claims it.

**Files:**
- Create: `docs/decisions/embervm/009-roadmap-extension-continuity-before-tenancy.md`
- Modify: `docs/decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md` (roadmap table lines 139-147, R6 row line 147; status legend line 149 gains no new vocabulary)
- Modify: `docs/decisions/index.md` (EmberVM section, after the 008 row)

**Specification:** ADR 009 header matches the embervm convention (Author, Status: Accepted, Created). Content: context (ladder exhausted, R5 drill pain), decision (facade demoted to Recorded with revival trigger; new rungs R6 Continuity / R7 Consumers / R8 Packaging with first consumers and v1 invariants from the design doc), the availability-contract change (spot semantics, 2-minute bound, explicit softening of R4's never-severed invariant), the S3-API durability seam, and consequences. Rationale only, no implementation detail (per the adr skill). ADR 001 edit: R6 Facade row re-statused to `Recorded (deferred, ADR 009)`, three new rows appended with status `Decided (ADR 009)`, one pointer sentence after the R6 paragraph.

**Acceptance:** `format` passes (manifests regenerate); ADR renders in the docs index; ADR 001 table remains valid markdown.

**Commit:** `docs(embervm): ADR 009 roadmap extension, continuity before tenancy`

### Task 2: Proto contract for drain and artifacts

**Why:** Both sides build against the contract; landing it first keeps every later PR additive.

**Files:**
- Modify: `projects/embervm/proto/embervm/node/v1/node.proto`
- Modify: fakenode (manual deps; `# gazelle:ignore`)
- Regenerate: Elixir + Go codegen per the R0 codegen flow

**Specification:**
- `message ArtifactRef { ArtifactKind kind; string workload; string ref; }` with `enum ArtifactKind { BASE; SESSION; SERVING; STATEFUL; GROUP_SET; VOLUME; }`.
- New rpcs on NodeService: `ExportArtifact(ExportArtifactRequest) returns (ExportArtifactResponse)`, `RestoreArtifact`, `EvictArtifact` (evict takes `bool remote` to distinguish store eviction from the existing local `EvictSnapshot`). Requests carry `ArtifactRef`; responses carry outcome + bytes moved + generation where applicable. All idempotent per ref.
- NodeStatus additions: `int64 drain_deadline_unix_ms` (0 when not draining), `bool store_reachable`, and per-artifact `bool exported` on the existing bundle/set/volume status messages.
- fakenode implements the three verbs with an in-memory store map and honors `drain_deadline_unix_ms`.

**Acceptance:** CI green; generated code compiles on both sides; no existing field renumbered.

**Commit:** `feat(embervm): proto contract for drain deadline and artifact export/restore/evict`

## Phase 1: Bounded-preemption drain

### Task 3: noded holds the door until drained

**Why:** Today SIGTERM waits only for in-flight task Assigns (`cmd/main.go:232-251`); live session/serving/stateful/group VMs are killed with the pod.

**Files:**
- Modify: `projects/embervm/noded/cmd/main.go` (shutdown sequence)
- Modify: `projects/embervm/noded/server/server.go` (`SetDraining`, WatchNode publication)
- Modify: `projects/embervm/noded/config/config.go` (`DrainTimeout` default 60s to 120s)
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml` (`terminationGracePeriodSeconds: drain + 30`, so 150s)
- Modify: `projects/embervm/chart/values.yaml` (`noded.drain.timeoutSeconds: 120`)

**Specification:** On SIGTERM: compute `deadline = now + DrainTimeout`, publish it via NodeStatus (`drain_deadline_unix_ms`) and `signalChange()`. Keep serving ALL lifecycle rpcs while draining (the CP needs Stop/Resolve/StopGroupMember/Bank to work); continue rejecting only new BuildBase/Prime/Assign (existing behavior). Wait until the live-VM registry is empty OR the deadline passes, then reap whatever remains (existing `reap()`), then GracefulStop. Unit test with a fake clock: VMs emptied early ends the wait early; deadline reaps stragglers.

**Acceptance:** CI green; unit tests cover early-exit and deadline paths; chart renders with 150s grace.

**Commit:** `feat(embervm): noded drain publishes a deadline and holds shutdown for the bank pass`

### Task 4: control-plane force-bank on observed drain

**Why:** The CP already retracts capacity on `draining` (NodeRegistry); it must now actively evacuate state within the published deadline.

**Files:**
- Create: `projects/embervm/control/lib/embervm/drain_coordinator.ex` (+ test)
- Modify: `projects/embervm/control/lib/embervm/node_registry.ex` (emit a `{:node_draining, node_id, deadline_ms}` event once per drain edge)
- Modify: `projects/embervm/control/lib/embervm/stateful_sweeper.ex` (accept a `:drain` bank reason that bypasses the idle predicate and forces COMMIT in `decide_resolve/2`)
- Modify: `projects/embervm/control/lib/embervm/group_sweeper.ex`, `session` and `serving` sweeper equivalents (same drain-reason bypass)
- Modify: `projects/embervm/control/lib/embervm/application.ex` (supervise DrainCoordinator; env `EMBERVM_DRAIN_SAFETY_MARGIN_MS`, default 15000)

**Specification:** On the drain edge, DrainCoordinator snapshots the node's live instances from ETS projections and dispatches, concurrently with bounded concurrency (reuse `EMBERVM_STATEFUL_BANK_CONCURRENCY`): stateful checkpoint-then-COMMIT (drain reason: parked connections do not abort, flap guard skipped, aborts counter untouched); groups banked as a unit via the existing group bank path (skip the zero-splice idle clause); sessions and serving banked via their existing verbs. Everything must be issued before `deadline - safety_margin`; workloads that cannot finish are left for noded's deadline reap (their durable state is the volume or prior bundle; that is the spot contract). Wakes arriving for a draining node park (existing park machinery) and resolve after the new noded registers and adoption heals state. Op-log: append a `:node_drain_started` and `:node_drain_finished` op with per-class counts.

**Acceptance:** CI green. Tests (fakenode): a draining node with one stateful + one group + one session instance gets all three banked and op-logged before deadline; a parked connection during drain still yields COMMIT; a wake during drain parks and resolves post-restart (existing adoption tests extended).

**Commit:** `feat(embervm): drain coordinator force-banks all classes within the preemption window`

### Task 5: post-roll relight verification hooks

**Why:** Adoption already heals banked state after a restart; what is missing is proof that parked wakes from the drain window complete against the new noded.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex`, `group_wake_manager.ex` (tests only unless a gap is found)
- Create: drill script `projects/embervm/docs/drills/roll-drain-drill.md` (runbook: how to roll noded with live scratch-postgres and scratch-k8s and what to observe)

**Specification:** Extend the reconcile/adoption test harnesses with a full sequence test: live instance, drain edge, force-bank, node down, node up with rescanned bundles, adoption to `:banked`, parked wake resolves via relight, generation pairing intact. Any gap this test finds (for example parked callers dropped on node `:down`) is fixed in this task, scoped to the minimal change.

**Acceptance:** CI green; the sequence test passes; drill runbook reviewed.

**Commit:** `test(embervm): end-to-end drain, roll, adopt, relight sequence coverage`

## Phase 2: Off-node durability

### Task 6: noded object-store client

**Why:** The durability seam. stdlib HTTP against an S3-API endpoint, per standing decision 5.

**Files:**
- Create: `projects/embervm/noded/store/store.go` (+ test with httptest server)
- Modify: `projects/embervm/noded/config/config.go` (`EMBERVM_NODED_STORE_ENDPOINT`, `EMBERVM_NODED_STORE_BUCKET` default `embervm`, empty endpoint disables)
- Modify: `projects/embervm/chart/values.yaml` + noded deployment env (`noded.store.endpoint`, default the SeaweedFS S3 URL; `noded.store.bucket`)

**Specification:** `Store` interface: `Put(ctx, key, reader, size)`, `Get(ctx, key) (reader, size)`, `Delete(ctx, key)`, `Head(ctx, key)`. Keys per Fork 3; `meta.json` written last as completeness marker; Get of an artifact = fetch `meta.json` first, verify listed files exist, then stream. SHA-256 recorded in meta and verified on restore (matches the zip-lane discipline). Reachability probe on a timer feeds `store_reachable`. No SigV4; optional static-cred header seam behind config for later.

**Acceptance:** CI green; client tests cover put/get/delete/head, partial-write invisibility (no meta.json means not present), checksum mismatch fails restore.

**Commit:** `feat(embervm): S3-API object store client for noded artifacts`

### Task 7: Export/Restore/Evict verbs and export-after-commit

**Why:** Makes banked state durable off node without touching the bank hot path.

**Files:**
- Modify: `projects/embervm/noded/server/server.go` (three new rpc handlers; async export queue; reconcile-time export sweep)
- Modify: `projects/embervm/noded/substrate/substrate.go` (artifact enumeration helpers per kind)

**Specification:** ExportArtifact copies the artifact's files then meta.json to the store (idempotent: Head short-circuits on same checksum); RestoreArtifact does the inverse into the correct on-disk dir and re-registers via the existing reconcile helpers; EvictArtifact(remote=true) deletes store keys (meta.json first, making it invisible before partial deletion). After a successful bank commit (session/serving/stateful/group set) or volume generation change at commit, enqueue an async export (bounded queue, drops re-enqueue on reconcile). Volume export ships vol.img + gen, skipped if gen unchanged since last export (compare meta). On startup reconcile, enqueue exports for any local artifact whose store copy is missing or stale (covers "roll exited before exports finished"). NodeStatus `exported` flags feed the CP projection.

**Acceptance:** CI green; tests: bank commit triggers export; gen-unchanged volume skips; restore round-trips a stateful bundle + volume pair byte-identically; evict removes meta first; draining does not block on the export queue.

**Commit:** `feat(embervm): artifact export/restore/evict with async write-back after bank commit`

### Task 8: control-plane export driver and restore-on-miss

**Why:** The CP owns placement facts; it decides when a wake needs a restore (Fork 4).

**Files:**
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex` (`plan_wake`: local bundle missing but store copy exists and volume present, or volume itself missing but exported: plan `:restore_then_relight` / `:restore_volume_then_cold`)
- Modify: `projects/embervm/control/lib/embervm/group_wake_manager.ex` (same for complete exported sets)
- Modify: `projects/embervm/control/lib/embervm/session_manager.ex`, serving equivalent (same pattern, session/serving bundles)
- Modify: `projects/embervm/control/lib/embervm/node_capacity.ex` / store projections (ingest `exported` + `store_reachable`)
- Modify: op-log kinds: `:artifact_exported`, `:artifact_restored`, `:artifact_evicted_remote`

**Specification:** Wake planning order per class: local bundle (existing paths) > store restore then relight > cold boot. Volume miss with an exported (vol.img, gen) pair restores the volume before any boot; generation pairing rules unchanged (a restored bundle still relights only against its matching volume generation). `store_reachable == false` never blocks a local-state wake (fail-open warmth). Restore runs inside the existing wake worker so single-flight and park semantics apply unchanged.

**Acceptance:** CI green; tests: delete-local-bundle miss restores and relights; delete-local-volume miss restores volume then cold-boots at correct generation; unreachable store degrades to cold boot with a logged reason; op-log records the restore.

**Commit:** `feat(embervm): restore-on-miss wake planning against the artifact store`

### Task 9: store retention and GC

**Why:** Standing decision 8; the store must not grow without bound or strand generations.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/stateful_sweeper.ex` + group/session/serving sweepers (banked-TTL eviction also issues `EvictArtifact(remote)`)
- Modify: superseded-generation eviction paths (`eager_evict_broken_pairs` analog for remote copies)

**Specification:** Remote eviction mirrors local eviction triggers exactly: banked TTL expiry, superseded generation, workload deletion. Guard: never evict the remote volume copy while any bundle (local or remote) pairs with its generation. Store-side orphans (meta-less partials) are swept by a low-cadence reconcile listing.

**Acceptance:** CI green; tests: TTL expiry evicts both copies; generation guard holds; orphan sweep removes metaless keys only.

**Commit:** `feat(embervm): remote artifact retention mirrors local eviction rules`

## Phase 3: Resilience amplifiers, observability, closure

### Task 10: wake timeouts and adoption self-recovery

**Why:** The R5 drills showed a stuck member boot holds `waking` forever (`:infinity` chains), starving `adopt_one` and overflowing the park.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/group_wake_manager.ex` (wake worker bounded by `wakeTimeoutSeconds` + margin; timeout fails the wake, releases single-flight, evicts per existing casualty rules)
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex` (same bound on the wake worker)
- Modify: adoption: a workload stuck in `waking` past `2 * wakeTimeoutSeconds` is reconciled as a casualty instead of skipped
- Modify: park overflow: on `park_full`, log with the oldest-waiter age and shed newest (existing) but also fire the alert (Task 11)

**Specification:** The parked caller's `:infinity` GenServer.call stays (callers wait as long as they choose); the bound goes on the wake WORKER, so a wedged boot resolves to a failed wake, waiters get an error or re-park, and single-flight releases. Timeout math derives from the workload's `wakeTimeoutSeconds` (scratch-k8s: 180s).

**Acceptance:** CI green; tests: a never-ready member fails the group wake at the bound, single-flight releases, adoption recovers the workload to `:banked` on the next reconcile; a stateful wake worker timeout does the same.

**Commit:** `fix(embervm): bound wake workers and let adoption recover stuck waking workloads`

### Task 11: observability and alerts

**Why:** Continuity claims need evidence in SigNoz, and drains should be visible, not archaeological.

**Files:**
- Modify: CP tracing module (root spans: `ember.node_drain`, `ember.artifact_export`, `ember.artifact_restore`; attrs: deadline, per-class counts, bytes, outcome)
- Modify: alert definitions (pattern from R5's dry-run alerts): drain finished with unbanked workloads; export backlog age above threshold; store unreachable above threshold; park_full fired

**Specification:** Spans follow the R5 pattern (ctx across Task.async). Alerts ship dry-run first, promoted during closure.

**Acceptance:** CI green; spans visible in a live drain drill; alerts render.

**Commit:** `feat(embervm): drain and artifact spans plus continuity alerts`

### Task 12: Closure

**Why:** The rung is Shipped when the gates hold live, and the ADRs must say so.

**Files:**
- Modify: ADR 001 (R6 Continuity row to Shipped once gates pass), ADR 003 (note the verb family shipped via R6), this plan (gate results)

**Specification:** Run the gates below in a stable no-deploy window (the R5 drills taught this). Record evidence per gate.

**Acceptance:** All gates green or explicitly deferred with reason; ADR statuses flipped; memory updated.

**Commit:** `docs(embervm): R6 continuity closure evidence`

---

## Explicitly out of scope

- Virtual control planes, etcd facade, multi-tenancy (deferred by ADR 009).
- Live migration, overlapping noded generations, zero-interruption rolls (future higher-availability tier).
- Block-device snapshot/volume replication and non-S3 backends beyond the endpoint seam.
- Cross-node placement policy (ADR 003's open question stays open; R6 ships the verbs, not the scheduler).
- SigV4 signing and store authn beyond the config seam (SeaweedFS in-cluster is anonymous).
- fc-agentd migration (R7) and open-source extraction (R8).

## Open risks

| Risk | Mitigation |
| --- | --- |
| Volume exports are large (GBs) and slow on 1GbE | Async write-back off every deadline; gen-unchanged skip; alert on backlog age rather than blocking |
| Force-COMMIT during drain surprises a client mid-session | Spot contract is documented per workload class; parked callers re-wake cleanly; scratch-* consumers are retry-tolerant |
| Drain deadline too tight for many instances (bank concurrency) | Safety margin + concurrency knob; gate 4 measures a full-node drain wall time |
| Restore path doubles cold-wake latency on true miss | Restore is the warmth-recovery path, strictly better than data loss; measured in gate 6 |
| CP roll during a noded drain (double roll) | Op-log persists drain ops; new CP adopts from node facts as today; gate 8 drills it |

## R7 planning seed

R7 Consumers: migrate the goosecracker agent-thread tier (fc-agentd, ADRs agents/022/028) onto EmberVM sessions; retire the bespoke controller. The R6 durability verbs make session state node-loss-tolerant first, which is the property that tier was missing.

## Closure: live-drill gates

| # | Gate | Evidence to record | Result |
| --- | --- | --- | --- |
| 1 | Entry criteria: R5 gate-1 entry path passes (kubectl via 5410) | drill log | TODO |
| 2 | noded roll with live scratch-postgres: banked within window, relit on next wake, zero data loss (row written pre-roll survives) | op-log + psql | TODO |
| 3 | noded roll with live scratch-k8s group: set banked as a unit, relit, nodes Ready | op-log + kubectl | TODO |
| 4 | Full-node drain wall time under 120s with all classes live | drain span | TODO |
| 5 | Parked wake during drain resolves after roll without client error beyond retry | drill log | TODO |
| 6 | Local bundle + volume deleted, wake restores from store at correct generation | op-log + span | TODO |
| 7 | Store unreachable: local wakes unaffected, alert fires, exports resume on recovery | alert + logs | TODO |
| 8 | CP pod roll mid-drain: no wedge, adoption converges | logs | TODO |
| 9 | Wedged member boot: wake fails at bound, workload recovers to banked without CP restart | logs | TODO |
| 10 | 48h soak: no export backlog growth, no park_full, no channel wedges | SigNoz | TODO |
