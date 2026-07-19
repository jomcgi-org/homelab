# EmberVM R7 (Distribution) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or superpowers:executing-plans in a separate session) to implement this plan task-by-task.

**Goal:** EmberVM runs across the whole 4-node fleet: workload volumes live on Longhorn (replicated, snapshotted, backed up incrementally to S3), a single-writer fence (Longhorn attach exclusivity plus generation blessing) makes multi-node placement safe before the second warm node exists, warmth is vendor-keyed so AMD node-4 stays the warm tier while the Intel nodes serve as cold/failover capacity, and noded rolls are sequenced by the control plane in idle windows instead of by the scheduler.

**Architecture:** Four mechanisms. (1) Fencing first: the control plane becomes the sole issuer of volume generations (blessing), and Longhorn RWO attach converts placement decisions into physical facts, so a zombie node's writes fail instead of corrupting. (2) Volumes move to Longhorn: the CP creates/attaches Volume CRs (`spec.nodeID`), noded hands the resulting `/dev/longhorn/<name>` block device straight to Firecracker, and bank commits take Longhorn snapshots with incremental S3 backup, retiring the custom vol.img export. (3) Warmth artifacts (bases, session/serving/stateful bundles, group sets) stay on the R6 S3 store client but gain a vendor key; cross-vendor wake is a cold boot from the target vendor's base plus the portable volume. (4) noded becomes a DaemonSet with OnDelete semantics and the CP becomes its rollout controller: interlock checks, drain, delete, verify, next node, with a staleness bound.

**Tech stack:** Go (noded), Elixir (control plane), protobuf (additive only), Longhorn 1.8.1 (Volume CRs, snapshots, S3 backupstore), SeaweedFS S3, Helm, Kubernetes DaemonSet OnDelete.

**Design doc:** `docs/plans/2026-07-18-embervm-r7-distribution-design-seed.md` (decisions 1-8; decision 9, binpacking, is out of scope here)

---

## Standing decisions (do not relitigate)

1. **Heterogeneous fleet, vendor-bound warmth.** noded runs on all 4 nodes. Firecracker cross-vendor snapshot restore is unsupported, so memory snapshots never cross the AMD/Intel boundary: AMD node-4 is the warm tier, Intel nodes 1-3 are the cold/failover tier. Bases are built per vendor on-node. VOLUME data is fully portable; SESSION is vendor-pinned (the memory image IS the state); SERVING/task warmth and GROUP_SET rebuild anywhere; a cross-vendor STATEFUL wake is a fresh boot from the target vendor's base plus the mounted volume.
2. **Volumes are Longhorn-native.** RWO Volume CRs attached to a node via `spec.nodeID`, surfaced as `/dev/longhorn/<name>` on the host, handed to Firecracker as `path_on_host` (a block device). NOT pod PVC mounts (fixed at pod creation, wrong lifecycle). Durability tiers are StorageClasses: `embervm-ha` (2 replicas) and `embervm-scratch` (single replica, strict-local). Rejected and staying rejected: dm-thin/thin_delta, content-hash manifests, btrfs send, RWX (Longhorn RWX is NFS, no raw block; multi-writer block is corruption by design).
3. **Single-writer HA: the CP arbitrates, Longhorn attach enforces.** Election is free (every wake already goes through the CP's single-flight path). Deciding is not enforcing: from the CP's chair a dead node and a partitioned-but-writing node look identical, so Longhorn attach exclusivity is the fence that makes CP wrongness survivable. Distribution-for-availability is replicas of ONE volume, never snapshot-cloned PVCs (those are the forking tool).
4. **Generation blessing fences the warmth plane, and it lands before the second warm-capable node.** The CP becomes the sole issuer of volume generations; noded uses the blessed generation handed to it at writable attach and never self-bumps. An artifact whose generation the CP never blessed is quarantined, never exported. Trivial to enforce from day one, miserable to retrofit after a split brain.
5. **Attach latency by case:** lazy detach while asleep (same-node relight pays zero), `migratable: true` dual-attach handover for planned moves sequenced inside the existing drain window (~zero), overlapped fresh attach only on crash failover (~2-5s, fired as the first step of the wake plan). Replica locality via dataLocality keeps a replica near the likely failover node.
6. **noded lifecycle is CP-sequenced OnDelete.** ArgoCD still delivers the template (GitOps unchanged); the CP notices template drift and rolls one node at a time: interlocks (store healthy, no wake in flight, export backlog sane, idle-bank window), drain, confirm banked, delete pod, verify the new pod reports, next. Required guardrails, not optional: a staleness bound that force-rolls after N hours with an alert, and a CP-down fallback (max-generation-lag alert, manual pod delete escape hatch). ArgoCD showing Synced while pods lag the template is correct OnDelete behavior and gets a runbook note.
7. **The CP owns WHEN; k8s, Longhorn, and S3 own HOW.** The CP implements no storage mechanism and no replication; it patches CRs and sequences verbs. noded stays a dumb executor: it receives a device path, it does not talk to Longhorn.
8. **Spot semantics remain the v1 availability contract** (2-minute preemption bound, durability at bank granularity, ADR 009). The "Longhorn airlock" (replicated live volume for RPO ~0) is recorded, not planned; it must never be adopted without measuring data-path latency against the demo's headline numbers.
9. **Serving fan-out stays single-instance in R7.** Multi-node placement moves the one live instance freely; N-way EDS fan-out with read replicas is app-level (CNPG-style) future work. The xDS publisher must merely learn that the endpoint's pod IP is per-node.
10. **All proto changes are additive.** New fields and verbs only, no renumbering.
11. **Warmth store keys gain a vendor segment; volume keys retire.** Vendor-bound kinds (BASE, SESSION, SERVING, STATEFUL, GROUP_SET) key as `<kind>/<vendor>/<workload>/<ref>/<file>`. VOLUME export/restore via the S3 store is retired once Longhorn backup covers the volume class; the verbs remain in the proto for compatibility but the CP stops planning them for Longhorn-backed volumes.
12. **Entry criteria for closure drills:** the SeaweedFS `embervm` bucket must accept writes (no R6 export has ever succeeded) and the Longhorn validation spike (Task 3) must pass its go/no-go before Phase 2 merges. R5/R6 live-drill deferrals stay as recorded; R7 gates do not claim them.

## Settled forks

**Fork 1: noded topology across nodes.** (a) One Deployment per node rendered from a values map; (b) a DaemonSet over `homelab.io/firecracker: "true"` with `updateStrategy: OnDelete`. Chose (b): OnDelete is only expressible on DaemonSet/StatefulSet, and a Deployment-per-node reimplements node fan-out by hand while still rolling on template change (Recreate rolls immediately; there is no OnDelete for Deployments). Per-node differences (nvmeRoot, scratch layout, max VMs) move to a node-keyed JSON map (`values.noded.nodes`) mounted as env; noded resolves its own entry via the Downward-API node name it already receives (`EMBERVM_NODED_NODE`).

**Fork 2: who owns the generation ledger.** (a) Daemon keeps bumping the on-disk `gen` ledger, CP audits after the fact; (b) CP issues the generation at writable attach (`blessed_generation` on StartStateful), daemon records and reports it but never invents one. Chose (b): auditing after the fact cannot fence a split brain, which is the entire point (standing decision 4). The daemon-side ledger file survives as a cache of the last blessed value for adoption reporting.

**Fork 3: volume backend transition.** (a) Two permanent lanes (vol.img local + Longhorn); (b) one lane: every stateful volume is a Longhorn CR, cheap workloads use the `embervm-scratch` strict-local class, vol.img is removed after migration. Chose (b): two storage lanes double every test matrix and the strict-local class IS the cheap tier. Per-volume engine overhead is accepted at current scale; a ceiling (max volumes per node) is recorded in the workload admission check before "every session gets a volume" can ever be proposed.

**Fork 4: who talks to Longhorn.** (a) noded manipulates Volume CRs (it is privileged anyway); (b) only the CP patches CRs, noded receives a resolved device path in the boot request. Chose (b): standing decision 7, plus RBAC stays in one place (the CP ServiceAccount) and noded remains deployable without cluster credentials.

**Fork 5: how the CP watches Longhorn.** (a) A full informer over longhorn.io like WorkloadWatcher; (b) targeted get/patch plus a bounded poll-until-attached inside the wake worker. Chose (b) for v1: attach status is only interesting during an attach the CP itself initiated, and the wake worker already owns retries/timeouts. An informer is a recorded upgrade if poll load ever matters.

## Cross-cutting constraints

- Never break the hit/miss invariant: attach, blessing, and rollout actions are lifecycle actions; the request hot path is untouched.
- Fail closed on enforcement (unblessed generation, vendor mismatch, attach conflict), fail open on warmth (store unreachable degrades to cold boot, never refuses a wake whose state is local).
- The Longhorn attach wait happens inside the existing wake worker so single-flight, park semantics, and wake timeouts apply unchanged.
- Conventional Commits; every deploy-affecting PR bumps the embervm chart via `bazel/tools/git/bump-chart.sh projects/embervm`; Longhorn/platform changes bump `projects/platform/longhorn`; any `docs/**.md` change forces monolith + monolith-public bumps (repo_docs manifest).
- No em-dashes in any authored text.
- fakenode BUILD has `# gazelle:ignore`: new gRPC imports need manual `deps` edits.
- All tests run in CI only (push the branch, watch BuildBuddy); no local `bazel test`.
- One comprehensive code review per PR at the end of that PR's implementation, per repo cadence.

## Suggested PR partitioning

| PR | Tasks | Deploys |
| --- | --- | --- |
| PR-0 | This plan + Task 1 (ADR 011, ADR 001/003 edits) | docs only (monolith manifest bumps) |
| PR-1 | Task 2 (SeaweedFS embervm bucket fix + first successful export) + Task 3 (Longhorn validation spike, drill doc) | platform/ops + docs |
| PR-2 | Task 4 (proto: vendor, blessing, device path, rollout fields; fakenode) | no behavior change |
| PR-3 | Task 5 (noded vendor detection + vendor-keyed bases/artifacts) | noded |
| PR-4 | Task 6 (CP generation blessing ledger + quarantine) | control plane |
| PR-5 | Task 7 (Longhorn settings, StorageClasses, S3 backupstore) | platform |
| PR-6 | Task 8 (CP Longhorn volume manager + RBAC) | control plane |
| PR-7 | Task 9 (noded block-device volume lane) | noded |
| PR-8 | Task 10 (bank-commit snapshots + backup, volume migration, vol.img retirement) | both |
| PR-9 | Task 11 (noded DaemonSet OnDelete + per-node config) | noded chart |
| PR-10 | Task 12 (CP rollout controller + guardrails) | control plane |
| PR-11 | Task 13 (Intel node staged bring-up) | values/labels |
| PR-12 | Task 14 (vendor-aware placement, cross-node wake, planned moves, pre-warm) | control plane |
| PR-13 | Task 15 (per-node endpoint publish) + Task 16 (observability, alerts, closure) | both |

Dependency edges that matter: PR-4 (blessing) and PR-3 (vendor keys) land before PR-11/PR-13 make a second node schedulable; PR-1's go/no-go gates PR-5 onward; PR-9 (device lane) needs PR-2 and PR-6.

---

## Phase 0: Decision record and prerequisites

### Task 1: ADR embervm/010

**Why:** The seed is a findings capture; the commitments (Longhorn, fencing, CP-sequenced rolls, heterogeneous fleet) need a decision record before code claims them, and ADR 003's open placement question closes here.

**Files:**
- Create: `docs/decisions/embervm/011-distribution-longhorn-fencing-cp-rollouts.md`
- Modify: `docs/decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md` (R7 row: `Decided (ADR 009)` to `In progress (ADR 011)`)
- Modify: `docs/decisions/embervm/003-control-plane-managed-snapshot-distribution.md` (closing note: placement policy resolved by ADR 011, vendor-keyed)
- Modify: `docs/decisions/index.md`

**Specification:** Rationale only, no implementation detail (adr skill). Content: the snapshot-portability boundary and its consequence table, Longhorn over the custom diff pipeline (with the rejected alternatives and one-line reasons from the seed), single-writer HA with attach-as-fence and generation blessing, CP-sequenced OnDelete rollouts with the two guardrails, the WHEN/HOW division of labor, and the explicit statement that the "2nd FC node" hardware prerequisite is dissolved by the cold/failover tier. Record the binpacking rung (seed section 7) as exploratory future work.

**Acceptance:** `format` passes; index renders; ADR 001 table valid.

**Commit:** `docs(embervm): ADR 011 distribution via longhorn, fencing, cp-sequenced rollouts`

### Task 2: make the durability store actually writable

**Why:** "No writable volumes for collection:embervm": no R6 export has ever succeeded, so the seam every warmth artifact depends on has never worked end to end. Under R7 the same SeaweedFS also becomes the Longhorn backupstore target.

**Files:**
- Modify: `projects/platform/seaweedfs/` values as needed (collection/volume-growth settings; do NOT enable `s3.createBuckets`, it flips auth for every consumer)
- Create: `projects/embervm/docs/drills/store-export-drill.md`

**Specification:** Provision the `embervm` bucket out-of-band via `weed shell` (`s3.bucket.create`), diagnose the collection's "no writable volumes" state (likely volume growth/collection settings on the SeaweedFS master), and fix in values where a chart value exists. Then run one real export: bank the demo stateful workload, confirm `ExportArtifact` succeeds, `meta.json` present, restore round-trips on a scratch workload. Record the procedure and evidence in the drill doc.

**Acceptance:** One STATEFUL bundle export and restore verified against the live store; drill doc records commands and evidence.

**Commit:** `fix(platform): make seaweedfs embervm collection writable and verify the export seam`

### Task 3: Longhorn validation spike (go/no-go for Phase 2)

**Why:** The design assumes Longhorn 1.8.1 behaviors the repo has never exercised: CR-driven attach without a pod, `/dev/longhorn/<name>` block handoff, `migratable` dual-attach, replicated-class latency. Verify before building on them.

**Files:**
- Create: `projects/embervm/docs/drills/longhorn-attach-drill.md`

**Specification:** On a scratch Volume CR: (1) create/attach via `spec.nodeID` patch with no consuming pod, time attach/detach over 20 cycles; (2) confirm the device node appears and accepts dd-level I/O from the host path noded would use; (3) exercise `migratable: true` dual-attach handover between two Intel nodes; (4) compare pgbench-style insert/aggregate latency on a 2-replica volume vs node-4 local NVMe (validation 2 of the seed); (5) confirm attach exclusivity: second-node writable attach while attached elsewhere must fail. Record numbers. Go/no-go: attach p95 under 10s, handover under 2s effective, replicated latency within 2x local for the demo workload profile; misses reopen the design seed rather than proceeding.

**Acceptance:** Drill doc with recorded numbers and an explicit go/no-go verdict.

**Commit:** `docs(embervm): longhorn attach and latency validation drill`

## Phase 1: Fences before fleet

### Task 4: proto contract for vendor, blessing, and rollout

**Why:** Both sides build against the contract; landing it first keeps later PRs additive.

**Files:**
- Modify: `projects/embervm/proto/embervm/node/v1/node.proto`
- Modify: fakenode (manual deps; `# gazelle:ignore`)
- Regenerate: Elixir + Go codegen per the R0 flow

**Specification:**
- `NodeStatus` additions: `string cpu_vendor` (e.g. `amd`, `intel`, from CPUID), `string node_template_hash` (the pod-template hash the running noded was deployed from, for the rollout controller).
- `StartStatefulRequest` additions: `uint64 blessed_generation` (0 means legacy/self-bump, rejected once blessing ships), `string volume_device` (host block-device path; empty means legacy vol.img lane during transition).
- `Volume` status message additions: `bool generation_blessed`.
- `RestoreArtifactRequest` gains `string vendor` so the CP restores only vendor-matching warmth.
- fakenode honors all new fields (vendor configurable per fake node, blessing recorded, device path echoed in status).

**Acceptance:** CI green; generated code compiles both sides; no renumbering.

**Commit:** `feat(embervm): proto contract for vendor keying, generation blessing, and device volumes`

### Task 5: noded vendor detection and vendor-keyed warmth

**Why:** Base keys and snapshot refs currently carry arch but not vendor; a second vendor in the fleet would poison the warmth cache and attempt unsupported restores.

**Files:**
- Modify: `projects/embervm/noded/config/config.go` (detect vendor from `/proc/cpuinfo` `vendor_id`, override env `EMBERVM_NODED_CPU_VENDOR` for tests)
- Modify: `projects/embervm/noded/substrate/substrate.go` (`SnapshotRef.Vendor` alongside `Arch`; restore fails closed on mismatch)
- Modify: `projects/embervm/noded/server/server.go` (base keys gain the vendor dimension; NodeStatus reports `cpu_vendor`)
- Modify: `projects/embervm/noded/store/store.go` (vendor segment in keys for vendor-bound kinds, standing decision 11)

**Specification:** Vendor is stamped at snapshot creation and validated at every restore exactly like arch today (mismatch is FAILED_PRECONDITION, never a silent cold boot at this layer; the CP plans the cold boot). Store keys for BASE/SESSION/SERVING/STATEFUL/GROUP_SET become `<kind>/<vendor>/<workload>/<ref>/<file>`; existing un-vendored keys are treated as node-4's vendor (`amd`) by a one-time reconcile-side alias so nothing re-exports needlessly.

**Acceptance:** CI green; tests: restore of a mismatched-vendor ref fails closed; base key for same image differs across vendors; store key layout includes vendor; legacy key alias resolves.

**Commit:** `feat(embervm): vendor-keyed bases, snapshots, and store artifacts`

### Task 6: control-plane generation blessing

**Why:** Standing decision 4. Today the daemon self-bumps the volume generation at writable attach; the CP only reads it. That ownership must invert before any second node can hold a stale copy.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex` (plan_wake issues the next blessed generation; op-log `:generation_blessed` before the StartStateful is sent)
- Modify: `projects/embervm/control/lib/embervm/stateful_store.ex` (blessed-generation ledger projection; quarantine flag on volumes reporting unblessed generations)
- Modify: `projects/embervm/control/lib/embervm/stateful_sweeper.ex` (never export, and eager-evict, artifacts whose generation is unblessed; alert counter)
- Modify: `projects/embervm/noded/server/server.go` + `projects/embervm/noded/volume/volume.go` (writable attach takes `blessed_generation`, persists it to the ledger file with a blessed marker, export queue gated on blessed)

**Specification:** Blessing is written to the op-log BEFORE the boot request leaves the CP (a crash between the two leaves an unused blessed number, which is harmless; the reverse order is a fence hole). On adoption, a node reporting `generation > last blessed` for a volume marks the volume quarantined: wakes for it park with a logged reason, an alert fires, and resolution is a manual drill decision (bless-and-adopt or discard) recorded in the runbook; fail closed, no auto-heal in v1. `blessed_generation == 0` requests are rejected by noded once its chart env `EMBERVM_NODED_REQUIRE_BLESSING=true` (set in the same PR the CP starts blessing; ordering: CP first, then noded flag, both behind one chart version so a mixed state cannot persist past the roll).

**Acceptance:** CI green; tests: wake carries monotonic blessed generation and op-log precedes dispatch; unblessed report quarantines and parks; export of unblessed artifact is refused daemon-side and never planned CP-side.

**Commit:** `feat(embervm): control plane becomes the sole issuer of volume generations`

## Phase 2: Longhorn-native volumes

### Task 7: Longhorn platform settings and StorageClasses

**Why:** The volume tier needs its classes and the backupstore before the CP can consume them.

**Files:**
- Modify: `projects/platform/longhorn/values-prod.yaml` (S3 `backupTarget` to the SeaweedFS S3 endpoint, backup credentials secret if required by 1.8.1 even for anonymous, keep `defaultReplicaCount: 1`)
- Create: `projects/platform/longhorn/storageclass-embervm-ha.yaml` (2 replicas, `dataLocality: best-effort`)
- Create: `projects/platform/longhorn/storageclass-embervm-scratch.yaml` (1 replica, `dataLocality: strict-local`)
- Modify: `projects/platform/longhorn/Chart.yaml` etc. via bump script

**Specification:** Mirror `storageclass-gpu.yaml` shape. The HA class must NOT pin nodeSelector to node-4 (its whole point is a replica elsewhere); scratch keeps strict-local. Backup target `s3://embervm@us-east-1/longhorn/` against `seaweedfs-s3.seaweedfs.svc.cluster.local:8333` (SeaweedFS ignores region), secret seam left even if anonymous. Verify with a manual snapshot+backup of the Task 3 scratch volume.

**Acceptance:** helm template renders; ArgoCD syncs; one backup object visible in SeaweedFS under the longhorn prefix.

**Commit:** `feat(platform): longhorn embervm storage classes and seaweedfs backupstore`

### Task 8: CP Longhorn volume manager

**Why:** Fork 4/5: only the CP touches Longhorn; it needs a small client and the attach lifecycle.

**Files:**
- Create: `projects/embervm/control/lib/embervm/longhorn.ex` (+ test with a fake K8s HTTP server, same pattern as `Embervm.K8s` tests)
- Modify: `projects/embervm/control/lib/embervm/k8s.ex` (generic CR get/patch helpers if not already generic)
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex` (wake plan: ensure volume CR exists with the workload's class, patch `spec.nodeID` to the placement target, poll status until attached with the wake worker's existing timeout, resolve device path `/dev/longhorn/<name>` into `StartStatefulRequest.volume_device`)
- Modify: `projects/embervm/chart/templates/rbac.yaml` (ClusterRole: `longhorn.io` `volumes` get/list/watch/patch/create, `snapshots`/`backups` create/get/list for Task 10)
- Modify: `projects/embervm/chart/values.yaml` (workload `stateful.storageClassName`, default `embervm-scratch`)

**Specification:** Volume CR name is the deterministic VOLUME ArtifactRef name (workload-scoped). Lazy detach: banking does NOT detach; the volume stays attached to its node until a placement decision moves it (standing decision 5). Attach conflict (already attached elsewhere) inside a planned move uses the migratable handover sequenced by the drain path (Task 14); outside a planned move it is a fence event: log, alert, refuse. RBAC verbs must cover every call the code makes (get/list/watch/patch/create), verified against the manifest in review (missing verbs fail as silent 5xx-shaped Forbidden).

**Acceptance:** CI green; tests: ensure-create idempotent; attach poll resolves and times out through the wake worker; conflict refuses with the fence reason; RBAC covers all verbs used.

**Commit:** `feat(embervm): control-plane longhorn volume lifecycle with attach-as-lease`

### Task 9: noded block-device volume lane

**Why:** noded must boot stateful VMs from a handed block device instead of its own vol.img.

**Files:**
- Modify: `projects/embervm/noded/server/server.go` (StartStateful honors `volume_device`; existence + block-device + exclusivity checks; skip volume.Manager.Create)
- Modify: `projects/embervm/noded/fcvm/driver/driver.go` (drive config takes the device path as `path_on_host`; boot args unchanged, guest still sees `/dev/vdb|vdc`)
- Modify: `projects/embervm/noded/volume/volume.go` (generation ledger for device volumes keyed off the blessed generation; ledger file moves to `<SnapshotRoot>/volgen/<workload>` since there is no per-volume dir)

**Specification:** Guest contract unchanged: blank-device mkfs detection in guest-init keeps working because a fresh Longhorn volume reads as zeros. The vol.img lane stays functional behind `volume_device == ""` until Task 10 completes migration, then is removed in the same task (Fork 3: one lane at the end). NodeStatus volume facts derive from the ledger plus a stat of the device path (attached means present).

**Acceptance:** CI green; tests (fake device file): device lane boots with correct drive config, exclusivity check refuses double writable attach, generation facts reported; legacy lane untouched by the flag.

**Commit:** `feat(embervm): stateful volumes boot from handed block devices`

### Task 10: snapshots at bank commit, backup, and vol.img retirement

**Why:** The bank commit is the crash-consistent freeze point; Longhorn snapshot + incremental backup replaces the custom VOLUME export, and the transition ends with one volume lane.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/stateful_sweeper.ex` (after COMMIT: create Longhorn Snapshot CR named by blessed generation; trigger Backup CR on a per-class cadence knob)
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex` (stop planning VOLUME ExportArtifact/RestoreArtifact for device volumes; restore-on-miss for the volume class becomes "Longhorn restore from backup" only in the disaster drill, not the wake path, since replicas cover node loss)
- Modify: `projects/embervm/noded/*` (remove the vol.img lane, volume.Manager.Create, and VOLUME export enqueue once migration is verified)
- Create: `projects/embervm/docs/drills/volume-migration-drill.md` (demo-postgres: bank, copy vol.img into an attached Longhorn device with dd, bless next generation, wake on the device lane, verify rows)

**Specification:** Snapshot naming `gen-<blessed_generation>` makes retention auditable against the blessing ledger; retention mirrors bundle retention (keep last N generations, sweeper-driven Snapshot CR deletion). Backup cadence is a knob (`stateful.backupEveryNCommits`, default 1 for `embervm-ha`, 0 = never for scratch). The migration drill runs before the retirement commit; the retirement is its own commit within the PR so a revert is surgical.

**Acceptance:** CI green; drill evidence: demo-postgres serving from a Longhorn device with pre-migration rows intact; snapshot per commit visible; backup object in SeaweedFS; vol.img code deleted with no remaining references.

**Commit:** `feat(embervm): bank commits snapshot longhorn volumes and retire the vol.img lane`

## Phase 3: CP-sequenced rollouts

### Task 11: noded DaemonSet with OnDelete

**Why:** Fork 1. The Deployment cannot express "roll only when the CP says so", and the fleet needs node fan-out anyway.

**Files:**
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml` (becomes `noded-daemonset.yaml`: DaemonSet, `updateStrategy: {type: OnDelete}`, same nodeSelector, same privileged pod)
- Modify: `projects/embervm/chart/values.yaml` (`noded.nodes` map keyed by node name: `nvmeRoot`, `maxLiveVMs`, per-node overrides; flat defaults preserved)
- Modify: `projects/embervm/noded/config/config.go` (resolve per-node entry from `EMBERVM_NODED_NODE_CONFIG` JSON by node name; fall back to flat env)

**Specification:** hostPath mounts must stay identical on node-4 (paths come from the per-node map, node-4's entry reproduces today's values byte-for-byte so the DaemonSet adopts cleanly). Init containers (rootfs-builder) run per pod as today. The template hash lands in a pod label and is echoed in NodeStatus `node_template_hash` (Task 4) so the CP can see drift. Only node-4 carries the label at this point; the DaemonSet has one pod and behavior is unchanged except the strategy.

**Acceptance:** CI green; helm template diff on node-4 shows only kind/strategy changes; live: pod adopted without a VM-visible interruption outside one final scheduler-timed roll (the last one ever).

**Commit:** `feat(embervm): noded becomes an on-delete daemonset with per-node config`

### Task 12: CP rollout controller

**Why:** OnDelete without a controller is a frozen deploy; the CP owns WHEN.

**Files:**
- Create: `projects/embervm/control/lib/embervm/node_rollout.ex` (+ test)
- Modify: `projects/embervm/control/lib/embervm/application.ex` (supervise after DrainCoordinator)
- Modify: `projects/embervm/chart/templates/rbac.yaml` (`apps` daemonsets get/list/watch; `""` pods get/list/watch/delete, scoped by namespace Role not ClusterRole)
- Modify: `docs/runbooks/argocd-outofsync.md` (OnDelete note: Synced with lagging pods is correct; escape hatch is manual pod delete)

**Specification:** Loop: read the DaemonSet's `pod-template-generation`, compare per node against NodeStatus `node_template_hash`. For each stale node, serially: interlocks (store_reachable, no wake in flight for that node, export backlog below threshold, inside the idle-bank window per the sweeper's own idle facts), then reuse the R6 drain path (the controller triggers drain exactly as SIGTERM would by deleting the pod AFTER force-bank completes: sequence is DrainCoordinator force-bank via a new `drain_node_for_roll` entry point, confirm banked counts, delete pod, wait for the replacement to report the new hash and healthy bases, then next node). Guardrails: staleness bound `EMBERVM_ROLLOUT_FORCE_AFTER_HOURS` (default 6) forces the roll and fires an alert; a max-generation-lag alert covers CP-down. Cold tier rolls before warm tier (order nodes by vendor, intel first).

**Acceptance:** CI green; tests (fakenode x2): stale node rolls only when interlocks green; forcing deadline fires; roll order cold-first; pod delete only after banked confirmation.

**Commit:** `feat(embervm): cp-sequenced ondelete rollout controller with staleness bound`

## Phase 4: Fleet bring-up and placement

### Task 13: Intel node staged bring-up

**Why:** Validation 6: kvm-intel, scratch layout, and noisy-neighbor blast radius next to CP/etcd have never been exercised.

**Files:**
- Modify: `projects/embervm/chart/values.yaml` (`noded.nodes` entries for the first Intel node; conservative `maxLiveVMs`, CPU/mem requests sized to protect co-resident CP)
- Create: `projects/embervm/docs/drills/intel-bringup-drill.md`

**Specification:** One Intel node first (label `homelab.io/firecracker: "true"`), not all three. Drill: kvm-intel present, noded pod healthy, vendor-keyed base builds on-node, a task-class VM boots, a scratch stateful workload cold-boots with a Longhorn scratch volume attached to that node, and node pressure stays within the reserved requests during a 30-minute soak. Only after the drill passes do the remaining two nodes get labeled (same PR or follow-up commit, per drill outcome).

**Acceptance:** Drill evidence recorded; three Intel nodes labeled or the blocker documented.

**Commit:** `feat(embervm): stage intel nodes into the firecracker fleet`

### Task 14: vendor-aware placement, cross-node wake, planned moves, pre-warm

**Why:** The rung's headline: placement policy over the R6 verbs, a move is a copy never a rebuild, demand-driven warming.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/serving_placement.ex` (vendor-aware scoring: prefer nodes holding vendor-matching warmth, then warm-tier vendor, then any eligible)
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex` (plan_wake drops the hard volume anchor: placement chooses the target node, volume attaches to it; same-node stays the fast path via lazy detach; crash failover fires attach as the first overlapped step)
- Modify: `projects/embervm/control/lib/embervm/drain_coordinator.ex` (planned move: for each draining stateful instance with a healthy target, sequence bank, migratable dual-attach handover, relight on target inside the drain window)
- Modify: `projects/embervm/control/lib/embervm/stateful_sweeper.ex` + serving/session equivalents (pre-warm: per-workload `preWarm` knob pushes RestoreArtifact of vendor-matching warmth to a target node ahead of demand)

**Specification:** Placement order for a stateful wake: (1) node currently holding the attached volume and vendor-matching bundle (zero-cost relight); (2) any node with the volume replica and vendor-matching base (attach + cold boot); (3) any eligible node (attach + cold boot from its own vendor base). Cross-vendor is never a restore, always a cold boot (standing decision 1). SESSION never moves cross-vendor: a session whose vendor tier is gone parks with a clear reason (spot contract). Pre-warm is fire-and-forget through the existing export/restore verbs with the vendor field from Task 4.

**Acceptance:** CI green; tests: placement prefers warmth, falls back cold cross-vendor, never plans a cross-vendor restore; planned move test shows bank, handover, relight without a second cold boot; pre-warm restores appear on the target fakenode.

**Commit:** `feat(embervm): vendor-aware placement with copy-not-rebuild moves and pre-warm`

### Task 15: per-node endpoint publish

**Why:** Serving/stateful endpoints are DNAT rules on the hosting noded's pod IP; with multiple nodes the publisher must follow the instance.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/endpoint_publisher.ex` (endpoint address derives from the hosting node's noded pod IP, taken from node facts, instead of a single configured address)
- Modify: `projects/embervm/control/lib/embervm/tcp_activator.ex` / `activator_splices.ex` (activator fallback dials the hosting node)
- Modify: chart (per-node noded Service or headless Service so pod IPs are stable to discover; simplest: noded reports its pod IP in NodeStatus already via `EMBERVM_NODED_POD_IP`, publisher consumes that; no new Service)

**Specification:** NodeStatus-carried pod IP is the source of truth (it already exists for DNAT); xDS publishes the current instance's node address and re-publishes on placement change. Single instance per workload (standing decision 9), so this is an address swap, not fan-out.

**Acceptance:** CI green; tests: instance on fakenode B publishes B's address; move republishes; activator fallback follows.

**Commit:** `feat(embervm): endpoints follow the hosting node`

## Phase 5: Observability and closure

### Task 16: observability, alerts, closure gates

**Why:** Fleet claims need evidence; fence violations must page, not lurk.

**Files:**
- Modify: CP tracing (spans: `embervm.volume_attach` with wait time, `embervm.node_roll` per node with interlock outcomes, `embervm.placement_move`; attrs: vendor, target node, bytes)
- Modify: `projects/platform/signoz-addons/alerts/` (promote/add: generation quarantine fired, attach-conflict fence event, rollout staleness forced, backup failure, store still-unreachable; dry-run first per repo pattern)
- Modify: ADR 001 (R7 row to Shipped when gates pass), ADR 011 (consequences confirmed), this plan (gate results)

**Specification:** Run the gates below in a stable no-deploy window. Gates 1-5 are drillable on node-4 plus one Intel node; the 48h soak requires the full fleet labeled.

**Acceptance:** All gates green or explicitly deferred with reason; ADR statuses flipped; memory updated.

**Commit:** `docs(embervm): R7 distribution closure evidence`

---

## Explicitly out of scope

- Binpacking, multi-pod noded, in-place resize, TimesFM forecasting (seed section 7; future rung).
- N-way serving fan-out / read replicas (app-level, CNPG-style; standing decision 9).
- The Longhorn airlock (replicated live volume, RPO ~0) beyond its recorded-option status.
- Live migration of Firecracker memory (the storage half now exists via migratable; the memory half is the deferred zero-interruption tier).
- Longhorn v2 (SPDK) engine.
- R8 Consumers (fc-agentd retirement) and R9 Packaging.
- The R5/R6 deferred live-drill gates; they remain owned by their own plans.

## Open risks

| Risk | Mitigation |
| --- | --- |
| Longhorn attach latency or reliability worse than assumed | Task 3 spike is a hard go/no-go before any Phase 2 code |
| Per-volume engine overhead at scale | Admission ceiling recorded in Fork 3; scratch class single-replica |
| iSCSI data path slower than local NVMe for the demo | Task 3 measures; demo can stay on scratch strict-local class on node-4 (still Longhorn, still fenced) |
| Blessing rollout ordering leaves a mixed CP/noded state | Single chart version carries both sides plus the require-blessing flag (Task 6) |
| Rollout controller freezes deploys silently | Staleness bound with alert; CP-down max-lag alert; manual delete escape hatch in runbook |
| Intel nodes destabilize co-resident CP/etcd | Conservative requests, one node staged first, 30-minute soak before the rest |
| SeaweedFS remains flaky as backupstore | Task 2 fixes the collection first; backup-failure alert; store is warmth+backup, never the only copy of a blessed generation still referenced |

## Closure gates

| # | Gate | Evidence to record | Result |
| --- | --- | --- | --- |
| 1 | Fence drill: writable attach refused while attached elsewhere; unblessed generation quarantined and alerted | drill log + alert | |
| 2 | Bank commit produces Longhorn snapshot named by blessed generation; backup object lands in SeaweedFS | CR list + store listing | |
| 3 | demo-postgres on the Longhorn lane: pre-migration rows intact, relight latency within budget | drill log + psql | |
| 4 | CP-sequenced roll on node-4 lands in an idle window with zero visitor-visible impact; ArgoCD Synced throughout | roll span + demo page | |
| 5 | Staleness bound: interlocks held red force the roll at the bound and fire the alert | logs + alert | |
| 6 | Cross-node wake: node-4 cordoned, stateful wake lands on an Intel node, cold boot from vendor base + attached volume, rows intact | drill log | |
| 7 | Planned move via migratable handover during a drain window; no second cold boot | move span | |
| 8 | Pre-warm: warmth restored to a target node ahead of a wake; wake is a relight not a cold boot | span + logs | |
| 9 | Endpoint follows a placement move; activator fallback dials the new node | drill log | |
| 10 | 48h full-fleet soak: no fence events, no quarantines, no rollout freezes, no backup failures | SigNoz | |
