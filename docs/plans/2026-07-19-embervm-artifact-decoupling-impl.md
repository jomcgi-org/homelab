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

**Architecture:** Eight PRs mapping to the seed's phases. Each PR is independently
shippable and carries its own chart bump. PR-A (Phase 0) and PR-F (Phase 6, surge) have
no dependency on the store work and can land early; PR-B through PR-E build the
artifact pipeline (ROOTFS kind, pushed registry, build queue, restore-on-miss); PR-G is
GC; PR-H (Phase 7) retires the chart-owned noded Deployment last.

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

Branch: `feat/embervm-phase0-quick-wins`. No proto changes, no new components.

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

Branch: `feat/embervm-rootfs-store`. Depends on PR-A (digest keying).

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

## PR-C: Phase 2, pushed registry

Branch: `feat/embervm-pushed-registry`. Depends on PR-B.

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

Tests: reconnect-replays-registry (Elixir), converge-drops-stale-entry (Go). Chart
bump, CI, review, merge. Verify: add a test workload's image in values; confirm no
noded roll occurs and the registry push appears in noded logs.

---

## PR-D: Phase 3, out-of-band base builds (build queue)

Branch: `feat/embervm-build-queue`. Depends on PR-C.

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
  SKU-matching node with headroom (today: node-4's builder pod, no longer inline
  with serving admission); serving Prime falls back to RestoreArtifact on miss

Tests: enqueue-dedups-on-key, store-hit-skips-build (Elixir); builder-role surface
test (Go). Chart bump, CI, review, merge. Verify: trigger a bazel-query warming and
roll noded mid-build; the build must survive the roll.

---

## PR-E: Phase 4, cpu_sku everywhere + CPU template

Branch: `feat/embervm-cpusku-gate`. Independent of PR-D (can parallel it).

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

Tests: mismatch-fails-loudly (one per path), miss-consults-store, placement-filters.
Note: changing the template invalidates existing base snapshots once; the build queue
(PR-D) rebuilds them keyed under the new sku, old keys age out via GC (PR-G). Chart
bump, CI, review, merge.

---

## PR-F: Phase 6, surge rolls (can land any time after PR-C)

Branch: `feat/embervm-surge-rolls`.

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
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml`: `Recreate` ->
  `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0`; delete the "never run two"
  comment; readiness gate already landed in PR-C
- Preemption notice: SIGTERM/preemption reaches the control plane as an event (the
  existing draining NodeStatus flag published immediately on signal, verified, not
  polled)

Tests: CID/TAP-partition (two instances allocate disjointly), handover ordering
(new-ready-before-old-drains) in the Elixir suite. Chart bump, CI, review, merge.
Verify with a live roll: `kubectl get pods -w` shows overlap, serving traffic
uninterrupted, sessions pause-and-relight only.

---

## PR-G: GC

Branch: `feat/embervm-store-gc`.

**Files:**
- Create: `projects/embervm/control/lib/embervm/artifact_gc.ex` (cron via the
  existing `cron.ex`): reconstructible artifacts (ROOTFS, base snapshots) are pinned
  while registry-referenced, TTL'd at 1h unreferenced, with a bounded +n allowance
  for prolific snapshotters; durable artifacts (banks, generations) are ref-counted
  from the op-log and deleted only when the owner is gone, never TTL'd
- Lazy local eviction: durable artifacts evict from NVMe only under space pressure
  (seed resolved question 8), reconstructible evict freely after export-confirmed

Tests: pinned-survives, unreferenced-expires, durable-never-ttls. Chart bump, CI,
review, merge.

---

## PR-H: Phase 7, control-plane-managed node deployments

Branch: `feat/embervm-cp-node-deployments`. Depends on PR-C (clean after PR-B's init
deletion); last to land.

**Files:**
- Create: `projects/embervm/control/lib/embervm/node_deployer.ex`: reconciles one
  noded Deployment per registered node (tainted node pool) via `k8s.ex`; pod template
  is PR-F's, unchanged, parameterized only by (image digest, node name, nvmeRoot);
  desired digest comes from chart-delivered control-plane config (Git stays the
  source of truth; a chart bump drives every roll, the control plane is the actuator
  and can canary one node); ownerReferences chain to a control-plane-owned parent so
  GC reaps forgotten nodes
- Modify: `projects/embervm/chart/templates/rbac.yaml`: apps/deployments
  create/update/delete in the embervm namespace
- Delete: `projects/embervm/chart/templates/noded-deployment.yaml` (the control plane
  authors these now; ArgoCD treats them as untracked foreign resources, no OutOfSync
  noise)
- Bootstrap asymmetry: the control plane's own Deployment stays chart-owned and must
  not tolerate the FC taint

Tests: reconcile-creates-per-node, digest-change-rolls-one-node-at-a-time,
owner-refs-set. Chart bump, CI, review, merge. Verify: bump the noded image, watch
the control plane roll node-4 with surge semantics end to end.

---

## Execution notes

- Every PR: Conventional Commits, chart bump in-PR, no local test runs, CI via
  BuildBuddy on push, one end-of-PR review, rebase merge, then verify the rollout
  live (ArgoCD sync + kubectl reads) before starting the next PR.
- Proto changes are additive-only throughout; regenerate codegen in the same commit
  as the .proto change (three _HEX_DEPS/codegen touchpoints on the Elixir side per
  the R0 pattern).
- PR-A and PR-F deliver the user-visible wins (fast rolls, zero-downtime rolls);
  PR-B/C/D/E are the artifact pipeline; PR-G/H complete the steady state. Suggested
  order: A, C, F for wall-clock impact, then B, D, E, G, H. (C before B is fine: the
  pushed registry can carry baked-locally rootfs refs until the store kind exists.)
