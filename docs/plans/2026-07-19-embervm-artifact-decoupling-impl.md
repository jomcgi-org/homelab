# EmberVM Artifact-Decoupled noded Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or
> superpowers:executing-plans in a separate session) to implement this plan
> task-by-task. Per repo policy there is no local test loop: implementers write tests
> alongside code, self-review, commit, and all verification happens on the pushed
> branch's CI. One comprehensive code review per PR, at the end of that PR's tasks.

**Goal:** noded becomes a stateless executor per the design seed
(docs/plans/2026-07-19-embervm-artifact-decoupled-noded-design-seed.md): all artifacts
live in the S3 store keyed by the seed's taxonomy, the control plane distributes them
and owns placement, and noded rolls are zero-downtime (surge handover, 1m50s drain
budget, warmth preserved).

**Fleet reality (2026-07-19 second design session, ADR embervm/012):** the fleet is
all four existing nodes. node-1/2/3 (the Intel Alder Lake-S etcd masters, ~12.3GiB
allocatable / 12 vCPU each) join AMD Zen4 node-4 as Firecracker hosts; the
etcd-co-location risk is explicitly accepted (ADR 012). Consequences for this plan:
the remaining work targets 4 nodes, cpu_sku becomes mandatory-and-early (two vendor
pools are real on day one), the FC taint is never applied (label-only, honest
requests via CP-owned in-place resize), and an interim DaemonSet bridges noded onto
all 4 nodes until the CP-managed per-node Deployments land.

**Architecture:** Ten PRs. Each is independently shippable and carries its own chart
bump. PR-A (Phase 0) is SHIPPED. PR-C (pushed registry + fleet bridge) leads the
remaining work; PR-E (cpu_sku) and PR-I (CP-owned dynamic sizing) land before
placement packs the masters; PR-B/PR-D complete the artifact pipeline; PR-F is surge
rolls; PR-J is the HA control plane; PR-H (Phase 7) retires the interim DaemonSet;
PR-G (GC) is last.

**Amended 2026-07-19:** CP-owned in-place resize is dropped (ADR 013 section 7 as
amended). PR-I is SHELVED; draft PR #3715 closed unmerged. PR-H is reshaped: the
control plane manages size-class brick Deployments (not per-node Deployments),
including the brick-count controller and slot-based placement; it absorbs the
capacity role PR-I held. PR-F's instance identity is pod_uid (no CP-issued
instance_id handshake). PR-B (rootfs via S3 store) is the distribution enabler for
brick utilization and lands beside the brick capacity PR. Landing order: A (shipped),
C (shipped), then {generic-scratch, R0 PR-1/2, PR-E, demo-fix} in parallel, R0 PR-3,
brick-capacity (ex-H) with B and D beside it, then F, J, G. The R0 brick-contracts
plan's PR-1/2/3 are prerequisites of the brick-capacity PR.

**Landing order and dependencies (one line each):**

1. **PR-A** (Phase 0 quick wins): SHIPPED; the taint runbook it added is now a
   recorded option, never applied (ADR 012), and its tolerations are harmless no-ops.
2. **PR-C** (pushed registry, registry persistence, FC labels, DaemonSet bridge):
   no code dependency; deliberately first so the fleet exists and workload changes
   stop rolling noded. (C before B is fine: the registry carries locally-baked
   rootfs refs until the store kind exists.)
3. **PR-E** (cpu_sku mandatory-and-early + grandfather rule): can parallel PR-C;
   MUST be live before any snapshot placement targets a master (two vendor pools).
4. **PR-I** (CP-owned dynamic sizing via in-place resize): depends on PR-C (the
   DaemonSet puts noded on the masters); MUST be live before placement packs the
   masters, since honest requests are what replaces the taint.
5. **PR-B** (ROOTFS via store, delete init containers): depends on PR-A digest
   keying; the bake tax is now paid 4x per roll, so the win quadrupled.
6. **PR-D** (out-of-band builds, per-vendor builder pods): depends on PR-C (queue
   over the registry view) and PR-E (builds keyed per (base_key, cpu_sku) pool).
7. **PR-F** (surge rolls): depends on PR-C (readiness gate, instance identity);
   applies surge semantics to the interim DaemonSet (maxSurge is per-node there).
8. **PR-J** (HA control plane, multi-replica): gated on moving the op-log off the
   single-writer SQLite RWO volume (ADR embervm/007 is the path); can parallel F.
9. **PR-H** (CP-managed per-node Deployments): gates only on PR-C; replaces the
   interim DaemonSet and deletes the chart-owned noded workload resource.
10. **PR-G** (GC): last; needs the registry (C), sku keys (E), and store kinds (B)
    to know what "referenced" means.

**Tech Stack:** Go (noded: `projects/embervm/noded/`), Elixir (control plane:
`projects/embervm/control/lib/embervm/`), protobuf additive-only
(`projects/embervm/proto/embervm/node/v1/node.proto`), Helm
(`projects/embervm/chart/`), bazel/apko images, BuildBuddy CI.

**Resolved-question anchors from the seed:** drain budget 1m50s (110s); instance
identity is control-plane-issued; cpu_sku = (vendor, template) with a conservative
fleet-wide Firecracker CPU template; GC = TTL for reconstructible / ref-count for
durable; build queue and registry state in op-log rows; rootfs artifacts digest-keyed.

---

## PR-A: Phase 0 quick wins (bake cache, drain budget + preStop build drain, taint)

**SHIPPED.** Branch: `feat/embervm-phase0-quick-wins`. No proto changes, no new
components. Post-ADR-012 note: Task A3's taint is never applied (label-only fleet;
honest requests via PR-I replace the scheduling fence). The tolerations shipped in
A3 are harmless no-ops and stay; PR-C updates the README taint runbook to record
"option, not step".

### Task A1: Digest-keyed rootfs bake cache

**Files:**
- Modify: `projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml`
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml` (init container env
  and the guestImage/rootfs path coupling near line 188)
- Modify: `projects/embervm/chart/values.yaml` (rootfs naming if referenced)

**Steps:**
1. In the builder script, resolve the guest image ref to its digest first (crane is in
   the builder image; `crane digest "$GUEST_IMAGE"`). Output becomes
   `rootfs-<first 12 hex of digest>.ext4` instead of the tag-keyed name. This resolves
   the seed's last open question: digest-keyed, decided here.
2. Add the cache short-circuit at the top: if the target ext4 already exists on the
   NVMe mount, log `rootfs cache hit <digest>` and exit 0 before any pull/extract.
3. Update every place the deployment/env derives the rootfs path from the image ref so
   noded's config and the builder agree on the digest-keyed name (grep the chart for
   `rootfs-` and `GUEST_IMAGE`; the deployment comment near line 188 marks the
   coupling).
4. Leave a tag-to-digest migration breadcrumb: the script deletes stale tag-keyed
   `rootfs-*.ext4` files for its own image only after a successful digest-keyed bake
   (bounded cleanup, no orphan accumulation).
5. `helm template embervm projects/embervm/chart/ -f projects/embervm/deploy/values.yaml`
   renders clean; eyeball the init container env diff.
6. Commit: `feat(embervm): digest-keyed rootfs bake cache, init no-op on hit`.

### Task A2: Drain budget to 110s and preStop BuildBase drain

**Files:**
- Modify: `projects/embervm/chart/values.yaml` (`noded.drain.timeoutSeconds: 120` ->
  `110`, update the comment: 1m50s targets the 2m spot notice minus notification
  latency)
- Modify: `projects/embervm/noded/server/server.go` and the drain path (see
  `drain_test.go` for the existing shape): on drain, in-flight BuildBase work is
  finished if it fits the deadline, else cleanly aborted (VM torn down, status left
  re-queueable, no half-written snapshot; meta.json-last already guarantees the store
  side)
- Modify: `projects/embervm/control/lib/embervm/drain_coordinator.ex`: bank priority
  order inside the budget is durable banks first, serving banks second, builds
  finish-or-abort last
- Test: extend `projects/embervm/noded/server/drain_test.go` with an
  in-flight-build-aborts-cleanly case; grep the Go and Elixir test trees for `120`
  drain assertions and update them in the same commit (repo rule: bump config values
  and their test assertions together)

**Steps:**
1. Write the failing drain test (build in flight, drain fires, expect abort + clean
   status within deadline).
2. Implement the finish-or-abort branch in the noded drain path.
3. Reorder the coordinator's force-bank sequence to the priority order.
4. Flip the values to 110 and update assertions.
5. Commit: `feat(embervm): drain finishes-or-aborts builds inside 110s budget`.

### Task A3: FC node taint and tolerations

**Files:**
- Modify: `projects/embervm/chart/values.yaml` (add
  `embervm.jomcgi.dev/node=true:NoSchedule` to `noded.tolerations` defaults and the
  serving relay's tolerations)
- Modify: `projects/embervm/chart/templates/serving-envoy-daemonset.yaml` (tolerations
  block if not already values-driven)
- Modify: `projects/embervm/docs/` runbook or README: document the one-time manual
  node taint (`kubectl taint nodes node-4 embervm.jomcgi.dev/node=true:NoSchedule`).
  Node taints are node lifecycle config, the same class as joining the node itself,
  so this is a documented operator action, not a GitOps resource; the GitOps rule
  (never kubectl-mutate managed resources) is not violated. Joe applies the taint
  after the tolerations PR is live, so ordering is safe (tolerations first, taint
  second; never the reverse).

**Steps:**
1. Add tolerations to both templates via values; render with helm template.
2. Write the runbook note with the exact taint command and the ordering warning.
3. Commit: `feat(embervm): tolerate FC node taint on noded and serving relay`.

### Task A4: Chart bump and CI

1. `bazel/tools/git/bump-chart.sh projects/embervm`
2. Push branch, open PR, `gh pr checks --watch`, fix on red via
   `mcp__buildbuddy__get_invocation` -> `get_log`.
3. One end-of-PR code review, then rebase-merge; verify rollout: next noded roll
   should show init containers no-op (`rootfs cache hit`) and roll minutes faster.

---

## PR-B: Phase 1, ROOTFS artifacts through the store

Branch: `feat/embervm-rootfs-store`. Depends on PR-A (digest keying); lands after
PR-C/E/I in the resequenced order. Fleet note: with noded on all 4 nodes, the
init-container bake tax is paid 4x per roll (one bake per workload per node), so
deleting the init containers is a 4x-per-roll win, not a single-node one. rootfs is
arch-keyed and vendor-agnostic, so ONE baked artifact serves all four nodes.

### Task B1: ROOTFS artifact kind

**Files:**
- Modify: `projects/embervm/proto/embervm/node/v1/node.proto` (additive: `ROOTFS`
  artifact kind on the existing Export/Restore/Evict artifact enum)
- Modify: `projects/embervm/noded/store/store.go` (+`store_test.go`): key scheme
  `rootfs/<arch>/<image_digest>` following the Fork-3 layout, meta.json-last, checksum
- Regenerate proto codegen per `projects/embervm/proto` BUILD targets (see the R0
  proto-codegen pattern; both Go and Elixir sides)

Steps: failing store round-trip test for the new kind; implement key mapping; commit
`feat(embervm): ROOTFS artifact kind in the store`.

### Task B2: noded fetches rootfs on miss

**Files:**
- Modify: `projects/embervm/noded/server/store.go` / boot path that today assumes the
  baked ext4 exists: on local miss, `RestoreArtifact(ROOTFS, image_digest, arch)` from
  the store into the digest-keyed NVMe path, then proceed. Degraded rule: warm cache
  keeps serving when the store is down; only a true miss fails, loudly.
- Test: `projects/embervm/noded/server/store_test.go` miss-then-fetch case.

Commit: `feat(embervm): rootfs restore-on-miss from store`.

### Task B3: Bake Job and control-plane trigger; delete init containers

**Files:**
- Create: `projects/embervm/control/lib/embervm/rootfs_baker.ex`: watches the workload
  set (the CRD-derived registry the control plane already holds), and for any
  (image_digest, arch) missing from the store schedules a k8s Job (via the existing
  `k8s.ex` client) running the builder script image; the Job bakes and exports ROOTFS
  through the store client. Registry-entry-missing-from-store IS the work signal
  (pull-based; no CI webhook). Single writer per key: the baker serializes per
  (digest, arch) via an op-log claim row.
- Create: Job template + RBAC (`projects/embervm/chart/templates/` new
  `rootfs-bake-rbac.yaml`; Jobs need create/watch in the embervm namespace)
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml`: delete the seven
  rootfs-builder init containers and their volumes
- Test: `projects/embervm/control/test/` baker test (missing artifact -> Job spec
  emitted; present artifact -> no-op)

Commit sequence: baker + test; Job RBAC; init container deletion; chart bump; PR, CI
watch, end-of-PR review, merge. Verify: delete one cached ext4 on node-4 (operator
action), watch a bake Job appear and the artifact land in the store.

---

## PR-C: Phase 2, pushed registry + fleet bridge (FIRST of the remaining PRs)

Branch: `feat/embervm-pushed-registry`. No dependency on PR-B: registry entries
carry locally-baked rootfs refs until the store kind exists (the init containers
keep baking under the DaemonSet in the interim; PR-B deletes them). This PR is
first because it creates the fleet (noded on all 4 nodes) and detaches workload
changes from noded rolls.

### Task C1: Registry verbs

**Files:**
- Modify: `projects/embervm/proto/embervm/node/v1/node.proto` (additive):
  `SyncRegistry(entries)` (authoritative full set: workload, image_digest, rootfs ref,
  harness init, sizing), `RegisterWorkload`, `DeregisterWorkload` (incremental
  optimizations)
- Modify: `projects/embervm/noded/server/registry.go` (+ tests): registry becomes an
  in-memory table fed by the verbs; `SyncRegistry` converges to exactly the pushed set
  (drops anything missed); idempotent under replay

### Task C2: Replay on connect; delete EMBERVM_NODED_IMAGES

**Files:**
- Modify: `projects/embervm/control/lib/embervm/node_channel.ex` /
  `node_registry.ex`: on every daemon (re)connect, after the adoption handshake,
  push `SyncRegistry` from the control plane's workload view (op-log-backed)
- Modify: `projects/embervm/noded/config/`: remove the EMBERVM_NODED_IMAGES parse
  path (registry starts empty; readiness gates on first SyncRegistry)
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml` +
  `projects/embervm/chart/values.yaml`: delete the env plumbing
- Readiness: noded reports ready only after registry replay completes (this is the
  Phase 6 readiness gate landing early; traffic never reaches a pod with an empty
  registry)

### Task C3: Persist the last-synced registry to the NVMe boot cache

The never-warm-to-dead rule (ADR 012): a restarting noded whose control plane is
briefly down must serve warm workloads from its cache instead of refusing
everything.

**Files:**
- Modify: `projects/embervm/noded/server/registry.go` (+ tests): after every
  applied `SyncRegistry`, write the registry table atomically (tmp + rename) to
  `<nvmeRoot>/embervm-noded/registry.json` with a `stale: true` marker semantics
  field; on boot, load it if present, mark the in-memory table STALE, and serve
  warm-cache workloads from it while readiness stays gated on the FIRST live
  `SyncRegistry` of this connection (a stale registry serves existing warmth, it
  never admits new work); the live sync clears the stale mark and converges as in
  C1
- Modify: `projects/embervm/noded/config/config.go`: derive the registry cache path
  from the existing nvmeRoot config (no new env var needed if derived; add
  `EMBERVM_NODED_REGISTRY_CACHE` override for tests)

Tests: restart-serves-warm-from-stale-cache, live-sync-clears-stale,
corrupt-cache-file-boots-empty (never crash-loop on a bad cache).

### Task C4: FC labels on all 4 nodes and the interim DaemonSet bridge

The single node-pinned Deployment cannot reach the masters, and per-node Helm
templating was rejected in the seed; the bridge until PR-H is a DaemonSet over the
FC-labeled nodes. Accepted interim cost: DaemonSet rolling-update semantics until
PR-F flips it to surge (DaemonSet maxSurge is per-node, exactly the semantics
wanted) and PR-H replaces it entirely.

**Files:**
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml` -> a DaemonSet
  over `homelab.io/firecracker: "true"` (keep the file's pod template intact; only
  the workload kind, selector, and strategy change). The "SINGLE-NODE ONLY"
  ClusterIP caveat in values.yaml becomes real: add a HEADLESS noded Service and
  feed per-pod endpoints into the control plane's node list
  (`Embervm.Application.configured_nodes/0` seam) via EndpointSlice discovery, so
  the NodeRegistry dials each daemon individually instead of a ClusterIP fan-out
- Modify: `projects/embervm/chart/values.yaml`: retire the single `node.id: node-4`
  shape in favor of the discovered list (the registry already takes a list); keep
  an override list for tests/out-of-cluster daemons
- Modify: `projects/embervm/README.md`: document the one-time operator action
  `kubectl label nodes node-1 node-2 node-3 homelab.io/firecracker=true` (and
  `embervm.io/serving=true` where serving redundancy is wanted); rewrite the taint
  runbook section to "recorded option, not applied" per ADR 012
- Guard: until PR-E lands, the control plane must not place snapshot-restoring
  work on a node whose CpuVendor differs from the artifact's; the existing vendor
  check covers the hard boundary, and Intel-pool warmth simply does not exist yet

Tests: headless-discovery-feeds-node-list (Elixir), DaemonSet renders with the
same pod template (helm template eyeball).

Tests: reconnect-replays-registry (Elixir), converge-drops-stale-entry (Go). Chart
bump, CI, review, merge. Verify: add a test workload's image in values; confirm no
noded roll occurs and the registry push appears in noded logs; confirm 4 noded pods
Ready and all 4 adopted in the control plane's node view.

---

## PR-D: Phase 3, out-of-band base builds (build queue)

Branch: `feat/embervm-build-queue`. Depends on PR-C (queue over the registry view)
and PR-E (build keys). Fleet note: "separate builds" now means one build per
(base_key, cpu_sku), i.e. per vendor pool: an Intel base built once on any master
serves all three masters; node-4's AMD pool builds its own. Builds run in a
builder-role pod, off the serving path, so a serving roll never kills a build.

### Task D1: Queue rows and store-first check

**Files:**
- Modify: `projects/embervm/control/lib/embervm/op_log/` + `base_builder.ex`:
  BaseBuilder becomes a queue consumer over op-log rows keyed
  `(base_key, cpu_sku)`; admission enqueues instead of building inline; the consumer
  first checks the store, then schedules; single writer per key via the claim row;
  status transitions written to the op-log (durable, replayable)

### Task D2: Builder pod

**Files:**
- Modify: `projects/embervm/noded/cmd/` + `projects/embervm/noded/server/`: builder
  role flag (`EMBERVM_NODED_ROLE=builder`): same binary, serves only BuildBase +
  store export, no serving surface
- Create: `projects/embervm/chart/templates/noded-builder-deployment.yaml` (separate
  pod, same node pool via taint toleration, schedulable by SKU label once multi-node)
- Modify: `base_builder.ex`: dispatch builds to a builder-role daemon on a
  SKU-matching node with headroom (one builder claim per vendor pool: any master
  for Intel keys, node-4 for AMD keys, no longer inline with serving admission);
  serving Prime falls back to RestoreArtifact on miss

Tests: enqueue-dedups-on-key, store-hit-skips-build (Elixir); builder-role surface
test (Go). Chart bump, CI, review, merge. Verify: trigger a bazel-query warming and
roll noded mid-build; the build must survive the roll.

---

## PR-E: Phase 4, cpu_sku everywhere + CPU template (MOVED UP: mandatory and early)

Branch: `feat/embervm-cpusku-gate`. Can parallel PR-C; MUST be live before any
snapshot placement targets a master. Two vendor pools (Intel x3 sharing one
template, AMD x1) are real the moment the DaemonSet lands, so the sku key stops
being future-proofing and becomes the fleet's partition function.

**Files:**
- Modify: `projects/embervm/noded/fcvm/driver/`: pin the conservative fleet-wide
  Firecracker CPU template (T2-family; config value with the template name), stamp
  `(vendor, template)` into every snapshot's meta.json at cut time
- Modify: restore paths in `projects/embervm/noded/server/` (session, stateful,
  serving, group): refuse a `(vendor, template)` mismatch with the mismatch in the
  error string (loud, never a wedge); on local miss consult the store before failing
  (the R6 promise made uniform; audit each of the four wake/relight paths)
- Modify: `node.proto` NodeStatus: add `cpu_sku` (vendor + template in force)
- Modify: `projects/embervm/control/lib/embervm/` placement modules
  (`session_placement.ex`, `serving_placement.ex`): filter candidate nodes by
  snapshot cpu_sku compatibility

**Grandfather rule (critical, prevents data loss; ADR 012):** unstamped legacy
durable artifacts (session banks and stateful generations cut before stamping
existed) stay node-pinned and restorable WHERE CUT. A restore does not need the
template (the vCPU state is in the snapshot), so the mismatch gate must NEVER
refuse a legacy (unstamped) artifact on the node that created it; such artifacts
are simply never distributed cross-node. Implement as: missing sku stamp + artifact
node-pin == this node -> allow with a log line; missing stamp + any other node ->
refuse (never distribute); present stamp -> normal gate.

**Template validation on real silicon (part of this PR's verify step):** boot +
BuildBase + restore round-trip with the chosen conservative template on one Alder
Lake-S master (hybrid P/E part: confirm the guest sees a homogeneous 6P+0E-style
topology) and on Zen4 node-4, BEFORE the template name is hard-coded into the sku
key. A key that never booted on its own pool is a liability with a version number.

Tests: mismatch-fails-loudly (one per path), miss-consults-store, placement-filters,
legacy-unstamped-restores-on-cutting-node, legacy-unstamped-refused-elsewhere.
Note: changing the template invalidates existing base snapshots once; the build queue
(PR-D) rebuilds them keyed under the new sku, old keys age out via GC (PR-G). Chart
bump, CI, review, merge.

---

## PR-I: CP-owned dynamic per-node sizing (in-place resize) (NEW, cross-cutting)

**SHELVED (2026-07-19 amendment, #3715 closed unmerged); superseded by the brick
capacity PR.**

Branch: `feat/embervm-dynamic-sizing`. Depends on PR-C (the DaemonSet puts noded on
the masters); MUST be live before placement packs the masters, because honest
scheduler-visible requests are what replaces the FC taint (ADR 012). Replaces fixed
maxLiveVMs static sizing as the capacity model.

**Files:**
- Create: `projects/embervm/control/lib/embervm/node_sizer.ex`: the resize control
  loop. Desired envelope per noded pod = daemon baseline + sum of
  provisioned/committed guest memMib (and vcpus) on that node, plus bounded
  headroom; reconciled against the live pod via the `pods/resize` subresource
  (k8s 1.35 InPlacePodVerticalScaling) through the existing `k8s.ex` client,
  adjusting request AND limit together. Policy: **grow-eager** (the sizer grows the
  envelope BEFORE a placement commits, and the placement only proceeds once the
  kubelet accepts the resize) / **shrink-lazy** (shrink only when the released
  delta is large, never below live commitment; a memory limit decrease may require
  a restart, so shrink defers to the next natural roll rather than forcing one)
- Modify: `projects/embervm/control/lib/embervm/` placement modules
  (`session_placement.ex`, `serving_placement.ex`, and the task dispatcher's node
  choice): placement asks the sizer for capacity first; a resize the kubelet
  reports Infeasible/Deferred is a **placement refusal** for that node (try the
  next candidate, never overcommit past the accepted envelope)
- Modify: `projects/embervm/chart/templates/rbac.yaml`: `pods/resize` (patch) plus
  pods get/list/watch in the embervm namespace for the control plane SA
- Modify: `projects/embervm/chart/values.yaml`: `noded.resources` shrinks to the
  daemon-only baseline (the CP grows it live); delete the hand-computed 36Gi
  ceiling arithmetic comment block; `noded.maxLiveVMs` is demoted to the pure
  runaway backstop its comment always claimed (no longer a capacity model), raised
  or left generous accordingly
- Test assertions: grep the Go and Elixir test trees for `maxLiveVMs`/`36Gi`
  assumptions and update in the same commit (repo rule: config values and their
  test assertions move together)

Tests: grow-before-place ordering, infeasible-resize-refuses-placement,
shrink-deferred-below-threshold, envelope-arithmetic (baseline + guests +
headroom). Chart bump, CI, review, merge. Verify: place a guest on a master and
`kubectl get pod -o yaml` shows the noded pod's requests grown by the guest's
memMib; `kubectl describe node` shows the allocation honestly.

---

## PR-F: Phase 6, surge rolls (can land any time after PR-C)

Branch: `feat/embervm-surge-rolls`. Rolls are RARE by construction after PR-C
(workload/function changes never roll noded; only a noded binary/image change
does), so this PR makes the rare event cheap: fast-or-zero-downtime, per node.

**Files:**
- Modify: `node.proto` + adoption handshake: the control plane issues an
  `instance_id` at adoption; daemons never self-claim (placement authority flip)
- Modify: `projects/embervm/noded/` run-dir layout, vsock CID allocator, TAP naming:
  all keyed by instance_id so two instances on one node never collide; the artifact
  cache dirs stay shared and digest-keyed (concurrent readers safe)
- Modify: `projects/embervm/control/lib/embervm/node_registry.ex`: two instances on
  one node is a control-plane-created state (surge or builder); the newer instance
  becomes authoritative only after registry replay + adoption complete; xDS endpoint
  shift happens then (`endpoint_publisher.ex` / `serving_manager.ex`), old instance
  drains within the 110s budget
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml` (the interim
  DaemonSet after C4): updateStrategy `RollingUpdate` with `maxSurge: 1,
  maxUnavailable: 0` (DaemonSet maxSurge is per-node: the new pod overlaps the old
  on the SAME node, exactly the warm-handover semantics wanted); delete the "never
  run two" comment; readiness gate already landed in PR-C
- Preemption notice: SIGTERM/preemption reaches the control plane as an event (the
  existing draining NodeStatus flag published immediately on signal, verified, not
  polled)

Tests: CID/TAP-partition (two instances allocate disjointly), handover ordering
(new-ready-before-old-drains) in the Elixir suite. Chart bump, CI, review, merge.
Verify with a live roll: `kubectl get pods -w` shows overlap, serving traffic
uninterrupted, sessions pause-and-relight only.

---

## PR-J: HA control plane (multi-replica) (NEW)

Branch: `feat/embervm-cp-ha`. The fleet gives workloads node-level redundancy; the
control plane must stop being the last single point of failure. Gated on moving
the op-log off the single-writer SQLite RWO Longhorn volume (the chart's `opLog`
block documents that constraint: Recreate + 1 replica exists BECAUSE SQLite is
single-writer). ADR embervm/007 (sharded CP, pg op-log cells) is the recorded
path; this PR implements its minimum: a Postgres-backed op-log and 2 replicas.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/op_log/` storage backend: Postgres
  (CNPG cluster or the shared pg per ADR 007's cell design) behind the existing
  op-log API; migration path from the SQLite file (one-shot import on first boot
  against an empty pg schema)
- Modify: `projects/embervm/chart/templates/deployment.yaml` + `values.yaml`:
  `replicas: 2`, drop `Recreate`, drop the op-log PVC once the pg backend is
  authoritative (the oplog PVC was grandfathered by the component-rename plan;
  retire it here)
- Ownership: each noded gRPC stream lands on exactly one CP replica; NodeRegistry
  adoption (op-log-recorded, per ADR 007's cell/ownership rows) decides which
  replica owns which node, and a replica loss triggers re-adoption on the survivor
  (the same reconnect path noded already exercises)

Tests: two-replicas-single-owner-per-node, replica-loss-readopts,
oplog-pg-round-trip. Chart bump, CI, review, merge. Verify: kill one CP pod during
a live session invoke; the invoke retries onto the survivor without state loss.

---

## PR-G: GC

Branch: `feat/embervm-store-gc`. Last to land: GC needs the registry (PR-C), sku
keys (PR-E), and store kinds (PR-B) to know what "referenced" means.

**Files:**
- Create: `projects/embervm/control/lib/embervm/artifact_gc.ex` (cron via the
  existing `cron.ex`): reconstructible artifacts (ROOTFS, base snapshots) are pinned
  while registry-referenced, TTL'd at 1h unreferenced, with a bounded +n allowance
  for prolific snapshotters; durable artifacts (banks, generations) are ref-counted
  from the op-log and deleted only when the owner is gone, never TTL'd
- Lazy local eviction: durable artifacts evict from NVMe only under space pressure
  (seed resolved question 8), reconstructible evict freely after export-confirmed
- Evict-time re-HEAD (ADR 012): before deleting a local DURABLE artifact, the
  evictor re-confirms the S3 object still exists (HEAD at eviction time), never
  trusting a stale export record; a failed HEAD aborts the eviction loudly
  (noded store client: `projects/embervm/noded/store/` eviction path + test)

Tests: pinned-survives, unreferenced-expires, durable-never-ttls. Chart bump, CI,
review, merge.

---

## PR-H: Phase 7, control-plane-managed node deployments

**Reshaped by the 2026-07-19 amendment: size-class brick Deployments plus count
controller plus slot placement; see ADR 013 section 7 as amended.**

Branch: `feat/embervm-cp-node-deployments`. Gates only on PR-C (clean after PR-B's
init deletion); replaces the interim DaemonSet bridge from C4. Near-last to land
(before PR-G).

**Files:**
- Create: `projects/embervm/control/lib/embervm/node_deployer.ex`: reconciles one
  noded Deployment per registered node (FC-labeled pool) via `k8s.ex`; pod template
  is PR-F's, unchanged, parameterized only by (image digest, node name, nvmeRoot);
  desired digest comes from chart-delivered control-plane config (Git stays the
  source of truth; a chart bump drives every roll, the control plane is the actuator
  and can canary one node); ownerReferences chain to a control-plane-owned parent so
  GC reaps forgotten nodes
- Modify: `projects/embervm/chart/templates/rbac.yaml`: apps/deployments
  create/update/delete in the embervm namespace
- Delete: `projects/embervm/chart/templates/noded-deployment.yaml` (the interim
  DaemonSet; the control plane authors per-node Deployments now, and ArgoCD treats
  them as untracked foreign resources, no OutOfSync noise)
- Bootstrap asymmetry: the control plane's own Deployment stays chart-owned and
  Helm/ArgoCD-delivered forever, never managed by node_deployer, so a wedged fleet
  controller cannot take down the thing that repairs it. Note the scheduling half
  of the original asymmetry (CP schedulable off FC nodes) is gone by construction:
  with all four nodes FC-labeled and no taint (ADR 012), every node is an FC node,
  so the asymmetry is purely about ownership, not placement

Tests: reconcile-creates-per-node, digest-change-rolls-one-node-at-a-time,
owner-refs-set. Chart bump, CI, review, merge. Verify: bump the noded image, watch
the control plane roll all four nodes one at a time with surge semantics end to
end (canary one master first).

---

## Execution notes

- Every PR: Conventional Commits, chart bump in-PR, no local test runs, CI via
  BuildBuddy on push, one end-of-PR review, rebase merge, then verify the rollout
  live (ArgoCD sync + kubectl reads) before starting the next PR.
- Proto changes are additive-only throughout; regenerate codegen in the same commit
  as the .proto change (three _HEX_DEPS/codegen touchpoints on the Elixir side per
  the R0 pattern).
- Final landing order (ADR 012 resequencing; dependency one-liners in the header):
  **A (shipped), C, E, I, B, D, F, J, H, G.** E can parallel C; I gates the masters
  actually taking guest placements; F and J can parallel each other after their
  gates; H replaces the interim DaemonSet and gates only on C; G closes the loop.
- Fleet guardrail during the sequence: between C landing (noded on all 4 nodes) and
  E+I landing, the control plane must not place snapshot or bulk work on the
  masters; the masters idle as registered-but-unpacked capacity until the sku gate
  and the resize ledger are live.
