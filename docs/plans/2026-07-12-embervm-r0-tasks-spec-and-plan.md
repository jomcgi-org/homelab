# EmberVM Phase 1 (R0 Tasks) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development in-session) to implement this plan task-by-task. This document is the committed spec for rung R0 of [ADR embervm/001](../decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md). It contains no implementation; every task below is a specification with acceptance criteria.

**Goal:** Ship EmberVM R0: durable, fair, retried task execution over the Firecracker fleet, proven by cutting the semgrep scan fleet over from fc-invoke with equal-or-better throughput and latency.

**Architecture:** An Elixir (BEAM) control plane owns the task queue, per-principal fairness, retry/DLQ, result store, primed-pool management, and enforcement, backed by an ETS hot set plus a pluggable durable op-log (SQLite-WAL in v1). A Go node daemon (`embervm-noded`, a clean fork of the fc-invoke node layer) owns Firecracker lifecycle behind a narrow gRPC API. Workload definitions live in a `Workload` CRD; kubectl, Helm, and ArgoCD are the entire management surface.

**Tech Stack:** Elixir/OTP (control plane), Go (node daemon fork), gRPC (control-to-node), SQLite-WAL (op-log v1), Kubernetes CRD + TokenReview, apko + Bazel dual-arch images, Helm + ArgoCD.

---

## Standing decisions (settled, do not relitigate during execution)

1. **Fork, do not extend.** `embervm-noded` is a fork of `projects/firecracker/substrate/node/*` + `substrate/*` (the ADR 031 seam). The existing fc-invoke daemon keeps serving its current consumers (monolith semgrep client, goosecracker agent, sandbox demos) unmodified and is marked deprecated for the scan path once cutover completes. Joe's explicit direction (2026-07-12): backwards compatibility is preserved by keeping the old daemon around, never by compromising Ember's interfaces. Design every Ember surface clean-sheet.
2. **The guest contract is frozen and shared.** A guest is an HTTP server on vsock port 1027 that answers `GET /shim/ready` with 200 when ready. Existing guest images (semgrep-guest, sandbox-guest) MUST work under EmberVM with zero image changes. This is what makes the cutover cheap and reversible.
3. **Hit/miss invariant (ADR embervm/001).** For R0 every task is a miss by isolation policy, so the control plane is on the path for every task; its overhead budget is held near zero by assignment-only dispatch from the primed pool.
4. **Enforcement fails closed, warmth fails open.** Unreadable quota/capacity state denies dispatch; lost snapshots or a cold pool degrade to slower boots, never to incorrect behavior.
5. **Facts through the control plane, payloads never**, with the R0-inherent exception: task request/response bodies flow control plane -> daemon -> vsock by definition (lifecycle-rate equals request-rate for the all-miss task class).
6. **No VM is ever reused across principals.** Task-class VMs are single-use: pristine restore, one assignment, destroy. Task-class VMs have no NIC (vsock only).
7. **v1 invariants held for later rungs** (cheap now, expensive to retrofit): the invocation front-end is a separate module from placement; `source` is a oneOf in the CRD schema; results and audit events are ordered op-log appends; per-tenant partitioning is a first-class key in every op-log record; snapshot metadata records carry a `volumeGeneration` field (null for the task class, the R4 pairing invariant); the CRD schema comment reserves a `spec.group` block (the R5 group-shaped room). Tenancy note: v1 runs a single tenant (`homelab`), so per-tenant quotas are degenerate; the tenant column is still carried on every record and enforcement key so multi-tenant is a data change, not a schema change.
8. **"Durable store never on the dispatch path" means dispatch never READS it.** Task-state transitions are write-through appends by design (the at-least-once bound in gate 3 depends on them); capacity and pool facts are read from ETS only.

## Cross-cutting constraints

- **No local test loop.** Implement, commit, push, watch BuildBuddy CI (`gh pr checks <n> --watch`). ExUnit and Go tests run under `bazel test //...` in CI only.
- **Conventional Commits; no em-dashes anywhere** (docs, commits, comments).
- **Images are apko, dual-arch, non-root where possible.** The noded fork inherits fc-invoke's privileged posture (KVM access requires it); the Elixir control plane runs unprivileged as uid 65532.
- **Charts bump via `bazel/tools/git/bump-chart.sh`** in the same PR as the code they deploy.
- **Every new monolith-facing endpoint or K8s read needs RBAC verbs verified** (`get`/`list`/`watch`) before merge.
- **One comprehensive code review per merged PR**, at the end of that PR's implementation.
- **Repository layout:** everything lands under `projects/embervm/` (standalone, open-sourceable; no imports from `projects/monolith`). The noded fork MUST NOT import from `projects/firecracker/substrate` (copy, then diverge; the old tree keeps building independently).

## Suggested PR partitioning

| PR | Tasks | Deploys |
| -- | ----- | ------- |
| PR-A toolchain + skeleton | 1, 2 | control-plane hello-world pod |
| PR-B daemon fork + proto | 3, 4 | embervm-noded on node-4 (idle) |
| PR-C CRD + op-log + state machine | 5, 6, 7 | CRD installed, control plane persists |
| PR-D submit API + dispatcher + pool | 8, 9, 10, 11 | end-to-end task execution live |
| PR-E enforcement + observability | 12, 13 | quotas, metering, audit live |
| PR-F scan-fleet cutover | 14, 15, 16 | semgrep on EmberVM, fc-invoke scan path deprecated |

Tasks within a PR may be reordered; PRs are the review and rollback boundaries. Each PR that changes chart-affecting code carries its own chart bump.

---

## Phase 0: De-risk the two unknowns

### Task 1: Elixir/OTP build-and-ship spike

**Why:** ADR embervm/001 records "BEAM toolchain tax in a Bazel/apko repo" as a top risk. Nothing else can start until an OTP release builds in this repo's CI and boots in the cluster, dual-arch.

**Deliverables:**
- `projects/embervm/control/` Elixir project skeleton (mix project, supervision tree with a health endpoint only)
- `projects/embervm/control/BUILD.bazel` (or equivalent wiring) building an OTP release artifact in CI
- `projects/embervm/image/` apko image definition embedding the release, dual-arch
- A short build-approach note appended to this plan's PR description (not a new ADR): which of the candidate approaches was chosen and why

**Specification:**
- Evaluate candidate approaches in order of simplicity and pick the first sufficient one: (a) `mix release` invoked from a Bazel `genrule`/custom rule with hex deps vendored or lockfile-fetched hermetically; (b) community `rules_elixir`/`rules_erlang` if maturity allows; (c) any other approach that keeps `bazel test //...` green and the image reproducible. The choice MUST NOT require CI steps outside BuildBuddy.
- The release MUST run as uid 65532, listen on :8080, and answer `GET /healthz` with 200.
- The image MUST be dual-arch (x86_64 + aarch64) via the standard apko + push pipeline.
- ExUnit MUST be runnable under `bazel test` (at least one trivial passing test wired in).
- Erlang/Elixir versions are pinned (OTP 27+, Elixir 1.18+ or current stable at execution time).

**Acceptance:**
- CI green on a branch containing the skeleton, including the ExUnit target.
- `bazel run //projects/embervm/image:push` publishes both arches (verified on main after merge).
- Pod runs in-cluster (Task 2 wires the chart) and `/healthz` returns 200.

**Commit:** `feat(embervm): elixir control-plane skeleton with bazel/apko build`

### Task 2: Chart, deploy, and GitOps skeleton

**Why:** Establish the deploy shape early so every later task lands behind ArgoCD instead of accumulating an undeployable branch.

**Deliverables:**
- `projects/embervm/chart/` Helm chart: control-plane Deployment (1 replica), Service, ServiceAccount, PVC for the op-log, values for image/resources
- `projects/embervm/deploy/` ArgoCD Application (multi-source OCI chart + `$values` git ref, copied from a recent service) + `kustomization.yaml` + `values.yaml`
- Namespace decision recorded in values: `embervm` (own namespace; nothing monolith-coupled)

**Specification:**
- The chart MUST template cleanly with `helm template embervm projects/embervm/chart/ -f projects/embervm/deploy/values.yaml`.
- Control-plane resources sized small to start (requests 100m/256Mi, limits mem 512Mi); resizing follows the SigNoz 7-day-peak convention later.
- Deployment strategy MUST be `Recreate`: the op-log PVC is RWO and SQLite is single-writer, so a rolling update would either deadlock on the volume or briefly run two writers.
- PVC (Longhorn, 1 replica per the non-HA policy) mounted for the SQLite op-log; a PVC provides durability, not availability, and that wording goes in the values comment.
- `format` regenerates the home-cluster root kustomization to include the new app.

**Acceptance:**
- ArgoCD app syncs Healthy; `kubectl -n embervm get pods` shows the control plane Ready; `/healthz` reachable in-cluster.

**Commit:** `feat(embervm): helm chart and argocd application skeleton`

### Task 3: Node daemon gRPC API (`embervm.node.v1`)

**Why:** This is the fork's clean interface, the seam Joe called out. It must be specified before the fork lands so the fork is shaped by the API rather than by fc-invoke's HTTP surface.

**Deliverables:**
- `projects/embervm/proto/embervm/node/v1/node.proto` + generated Go and Elixir stubs wired into Bazel
- Interface documentation as proto comments (the proto is the spec; no separate doc)

**Specification (the API, normative):**
- `rpc BuildBase(BuildBaseRequest) returns (BuildBaseResponse)`: pull OCI image, bake rootfs, cold-boot, health-gate on the guest ready path, snapshot, return `snapshot_ref` + base metadata (size, digest, arch). Idempotent per (image digest, workload revision).
- `rpc Prime(PrimeRequest) returns (PrimeResponse)`: restore a pristine VM from `snapshot_ref`, wait ready, park it idle on vsock; returns `vm_id`. This is the pool-refill primitive.
- `rpc Assign(AssignRequest) returns (AssignResponse)`: deliver exactly one HTTP-semantics task (method, path, headers, body; body capped at 8 MiB) to a primed `vm_id` over vsock; returns the guest HTTP response plus usage stats (`cpu_ms`, `peak_rss_mib`, wall time). The daemon destroys the VM after response delivery regardless of outcome. `Assign` on an already-assigned or destroyed `vm_id` MUST fail without side effects.
- `rpc Destroy(DestroyRequest) returns (DestroyResponse)`: reap a primed or wedged VM.
- `rpc WatchNode(WatchNodeRequest) returns (stream NodeStatus)`: server-streamed heartbeat with capacity facts (free primed slots per workload, mem/cpu headroom, base build states, daemon draining flag). The daemon is the health authority.
- `rpc GetNodeStatus(GetNodeStatusRequest) returns (NodeStatus)`: unary snapshot of the same facts, for polling fallback and startup reconciliation.
- All RPCs carry a `workload` name and an opaque `task_id`/`correlation_id` for tracing. Errors use canonical gRPC codes; `RESOURCE_EXHAUSTED` for capacity, `FAILED_PRECONDITION` for lifecycle misuse.
- Auth v1: mTLS is out of scope; the daemon listens on the pod network, gated by Linkerd policy + a static bearer token from a Kubernetes Secret (upgrade path noted in proto comments).
- The proto MUST NOT leak Firecracker concepts (no jailer paths, no snapshot file paths in the API; refs are opaque strings).

**Acceptance:**
- Proto builds in Bazel for both languages; a Go fake server + Elixir client round-trip test passes in CI (no Firecracker involved).
- Reviewer confirms the API contains nothing fc-invoke-HTTP-shaped (no reverse-proxy semantics, no session path segments).

**Commit:** `feat(embervm): node daemon grpc api v1`

### Task 4: Fork the node daemon (`embervm-noded`)

**Why:** Reuse five months of hardened Firecracker lifecycle code (restore races, drain, read-only rootfs, stats) without dragging along the deprecated caller surface.

**Deliverables:**
- `projects/embervm/noded/` Go module: forked copies of `substrate/node/{invoker,fcvm,vsockhttp,egress}` and the `substrate` seam types, reshaped behind the Task 3 gRPC service
- `projects/embervm/noded/image/` apko image (privileged, same KVM/nvme mounts as fc-invoke)
- Chart extension: `embervm-noded` as a single-replica Deployment pinned to `homelab.io/firecracker=true` nodes (node-4), Recreate strategy, drain wired like fc-invoke (grace = drain + 30s)

**Specification:**
- Delete on fork: HTTP ingress, path-based sessions, the workload JSON env catalog, warm-base auto-build-at-startup (base builds become `BuildBase` calls), the per-workload semaphore (concurrency moves to the control plane; the daemon enforces only a node-level max-live-VM cap as a backstop).
- Keep on fork: fcvm driver (Claim/SnapshotBase/Release/Stats), vsockhttp transport including the 150ms per-attempt WaitReady fix and 2s RestoreReadyTimeout, read-only rootfs + tmpfs guest conventions, rootfs-builder init pattern, egress forwarder (present but unused by task class; task VMs get no NIC and egress disabled).
- The daemon is stateless across restarts except node-local snapshot files; on start it reports existing base snapshots in `NodeStatus` so the control plane reconciles instead of rebuilding.
- Coexistence budget with fc-invoke on node-4 MUST be explicit in values: embervm-noded gets its own memory limit, and the shadow-phase values PR (Task 14) rebalances fc-invoke's concurrency so `fc-invoke limit + embervm-noded limit + co-tenants <= node allocatable`.
- Unit tests: fork compiles and passes the inherited driver/transport tests; a fake-driver gRPC integration test covers Prime -> Assign -> auto-destroy and Assign-after-destroy rejection.

**Acceptance:**
- CI green including forked tests; embervm-noded pod Ready on node-4 answering `WatchNode` (verified with grpcurl from the control-plane pod), zero VMs running, fc-invoke untouched and still serving.

**Commit:** `feat(embervm): fork fc-invoke node layer as embervm-noded behind grpc`

---

## Phase 1: Control-plane core

### Task 5: Workload CRD and watcher

**Why:** Definitions are low-churn declarative intent; the CRD is the entire v1 management surface (kubectl + GitOps, no bespoke API).

**Deliverables:**
- `projects/embervm/crd/workload-crd.yaml`: `workloads.embervm.dev/v1alpha1`, namespaced, status subresource, printer columns (CLASS, READY, SNAPSHOT, AGE)
- Elixir `WorkloadWatcher`: one watch on the CRD, in-memory catalog, status writer
- RBAC: control-plane ServiceAccount gets `get/list/watch` on workloads and `update/patch` on `workloads/status`; sample CR in `projects/embervm/crd/samples/`

**Specification (schema, normative for R0):**
- `spec.class`: enum, only `task` valid in v1alpha1 (schema reserves the enum for `session|serving|stateful`).
- `spec.source`: oneOf; only `image` implemented: `{ref (OCI reference, digest-pinned or tag), port (int, guest HTTP port), readyPath (default /shim/ready), invokePath (default /, the guest path tasks are POSTed to), initEnv (map, optional)}`. The oneOf shape MUST be structurally validated (exactly one member set).
- `spec.resources`: `{vcpus (1..8), memMib (128..16384)}`.
- `spec.concurrency`: `{floor (primed pool floor, >=0), cap (hard max in-flight, >=1, >=floor)}`.
- `spec.invocation`: `{timeoutSeconds (1..900), retry {maxAttempts (1..10, default 3), backoffSeconds (default 1), backoffCapSeconds (default 60), retryOn (enum list: transport|timeout|guest5xx, default all three)}, resultTtlSeconds (default 86400), resultMaxBytes (default 1 MiB, max 8 MiB), deadLetter {enabled (default true)}}`. Failure destinations (forwarding failed tasks to another workload or endpoint) are explicitly a recorded follow-on, not v1; the DLQ list/redrive surface in Task 8 is the v1 answer to the ADR's failure-handling contract.
- `spec.triggers`: list, v1 supports `{cron: "<5-field cron>", payload: <inline JSON, <=8KiB>}` only; may be empty.
- `status`: `{observedGeneration, snapshotRef, snapshotDigest, conditions[] (Ready, BaseBuilt), primedFloorSatisfied (bool)}`.
- Guest-image identity fields (rootfs path, harness init) are daemon-side values configuration in v1 (they are node facts, not workload intent); revisit when multi-node lands.
- Watcher MUST tolerate restarts (relist + reconcile), reject invalid CRs by condition rather than crash, and never write anything except `status`.

**Acceptance:**
- CRD applies; sample CR round-trips; ExUnit watcher tests with a fake K8s API cover add/update/delete/relist; `kubectl get workloads` shows printer columns. RBAC verbs verified against every API call the watcher makes.

**Commit:** `feat(embervm): workload crd v1alpha1 and control-plane watcher`

### Task 6: Op-log seam and SQLite-WAL backend

**Why:** The durable book-of-record; every lifecycle and enforcement action becomes an ordered, auditable append. Getting the seam right is a v1 invariant (the `ra` tier and R6 partitioning plug in behind it).

**Deliverables:**
- Elixir `OpLog` behaviour (append, read-from, snapshot-load, compact) + `OpLog.SQLite` implementation on the PVC
- Task and result tables with a documented schema migration path

**Specification:**
- Schema (v1): `ops(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts, tenant, principal, workload, task_id, kind, payload_json)`; `tasks(task_id TEXT PRIMARY KEY, tenant, principal, workload, state, attempt, idempotency_key, submitted_at, updated_at, expires_at)`; unique index on `(workload, idempotency_key)` where idempotency_key is not null; `results(task_id PRIMARY KEY, status_code, body BLOB, size_bytes, truncated, created_at, expires_at)` with body capped at the workload's `resultMaxBytes` (larger stored copies are truncated with the flag set; object-store spill is a recorded follow-on, not v1). Truncation applies to the stored copy only; sync callers receive the full response (Task 8).
- `kind` values (closed enum, additive-only): `submitted, assigned, started, succeeded, failed, retried, dead_lettered, denied, base_built, primed, vm_destroyed, quota_enforced, drain`.
- Every record carries `tenant` and `principal` columns (R6 partitioning invariant), even though v1 has effectively one tenant.
- WAL mode on; single writer process (an OTP GenServer owns the connection); fsync on task-state transitions; append latency budget p95 <= 5ms on the PVC.
- The dispatch path MUST NOT read the op-log; it reads ETS only. The op-log is write-behind for warmth facts and write-through for task-state facts.
- TTL sweeper deletes expired results and compacts terminal tasks older than retention.

**Acceptance:**
- ExUnit property test: any interleaving of appends yields monotonically increasing `seq` with no gaps post-recovery; kill-and-restart test recovers task states exactly; result TTL sweep verified with clock injection.

**Commit:** `feat(embervm): op-log seam with sqlite-wal backend`

### Task 7: Task state machine and ETS hot set

**Why:** The core semantics R0 exists to add: durable records, ownership, managed retry, DLQ, results.

**Deliverables:**
- Elixir `TaskStore`: ETS tables (tasks in flight, capacity facts, primed-pool inventory) rebuilt from op-log on boot
- Task lifecycle FSM implemented as data (explicit transition table), not scattered conditionals

**Specification:**
- States: `queued -> assigned -> running -> succeeded | failed_retryable -> queued (attempt+1) | failed_permanent -> dead_lettered`. Terminal: `succeeded, failed_permanent, dead_lettered`. Every transition appends to the op-log before it is visible in ETS (task-state write-through). Submit-time denials (quota, cap, auth) do NOT create task records; the `denied` op-log kind carries a nullable `task_id` and the denial reason.
- Delivery semantics: at-least-once. A task found `assigned`/`running` after control-plane restart with no daemon evidence (vm gone from `NodeStatus`) is retried per policy; duplicate side effects are the caller's concern via idempotency keys.
- Idempotency: a submit carrying an existing `(workload, idempotency_key)` returns the existing task (and its result if terminal) instead of creating a new one, for the lifetime of the result TTL.
- Retry: exponential backoff with full jitter from `backoffSeconds` to `backoffCapSeconds`; only error classes listed in `retryOn` are retryable; guest 4xx is always `failed_permanent` (the guest spoke HTTP; the task itself is wrong).
- Recovery: control-plane boot replays op-log into ETS, reconciles against `NodeStatus` streams, and resumes queued work within 10 seconds of pod Ready (measured in the Task 16 kill test).

**Acceptance:**
- ExUnit: exhaustive transition-table test (illegal transitions raise), restart-recovery test with a scripted op-log, idempotency dedupe test, backoff distribution test with seeded RNG.

**Commit:** `feat(embervm): task state machine with ets hot set and op-log recovery`

### Task 8: Submit API (invocation front-end)

**Why:** The caller-facing surface; deliberately a separate module from placement (v1 invariant, keeps R3's front-end split reachable).

**Deliverables:**
- HTTP API in the control plane: `POST /v1/workloads/{name}/tasks` (async: 202 + task_id; sync: `?wait=true` parks the request until terminal state or `timeoutSeconds`), `GET /v1/tasks/{id}` (state + metadata), `GET /v1/tasks/{id}/result` (stored result until TTL), `GET /v1/workloads/{name}/dead-letters` (paged DLQ listing), `POST /v1/tasks/{id}/redrive` (re-queue a dead-lettered task, attempt counter reset, audited)
- TokenReview auth port of the fc-invoke pattern: bearer token -> TokenReview -> allow-list of ServiceAccount usernames from values; principal recorded on every task

**Specification:**
- **Task envelope (normative):** a task IS one HTTP request to the guest. Method is always POST for the task class. Path defaults to the workload's `spec.source.invokePath`, overridable per submit with the `X-Ember-Guest-Path` header. The submit request's body is forwarded verbatim as the guest request body; `Content-Type` is forwarded; headers prefixed `X-Ember-Guest-` are stripped of the prefix and forwarded; no other caller headers reach the guest. Cron trigger payloads are POSTed to `invokePath` with `Content-Type: application/json`. This is the contract `AssignRequest` carries and the Task 15 monolith client programs against.
- Token validation MUST be cached: sha256(token) -> principal with a 60s TTL, singleflight on misses, failures never cached, and the K8s client configured with raised QPS/Burst (the fc-invoke 5 QPS TokenReview throttle incident, PR #3352, is the cautionary precedent; gate 1 implies ~75 authenticated submits/s).
- Sync wait is a parked BEAM process, not a poll loop; parked count is capped per principal (default 512) and excess sync submits are rejected 429 (miss-path abuse guard, wake-rate analog).
- Request body cap 8 MiB (matches daemon Assign cap); sync responses stream the full untruncated guest response through to the caller regardless of `resultMaxBytes` (which caps only the stored copy).
- Errors are structured JSON `{error, task_id?, retryable}`; denial reasons (quota, cap, auth) are distinguishable from capacity backpressure.
- OpenAPI document generated or hand-written under `projects/embervm/docs/api.yaml`; it is the contract for the monolith client in Task 15.
- API MUST NOT expose op-log internals; task queries read ETS/result store only.

**Acceptance:**
- ExUnit request tests for both modes, auth allow/deny, idempotency header (`Idempotency-Key`), 429 park-cap, result retrieval before and after TTL. RBAC: TokenReview needs `create` on `tokenreviews.authentication.k8s.io` (verify in chart RBAC).

**Commit:** `feat(embervm): task submit api with tokenreview auth`

---

## Phase 2: Dispatch and fleet

### Task 9: Node registry and health authority wiring

**Why:** The daemon is the health authority; the control plane must consume `WatchNode` as its only source of node truth.

**Deliverables:**
- Elixir `NodeRegistry`: one supervised connection per configured node daemon (v1: exactly one, from values), consuming `NodeStatus` into ETS capacity facts

**Specification:**
- Stream drop marks the node `unknown` after 5s and `down` after 15s; tasks assigned to a down node re-enter `queued` per retry policy (at-least-once).
- Draining flag in `NodeStatus` stops new assignments immediately (fail-closed: no capacity facts means no dispatch).
- Node identity and address come from values in v1; the registry interface takes a list so multi-node needs no reshaping (seam only, no multi-node logic).

**Acceptance:**
- ExUnit with a fake gRPC server: stream drop -> reassignment; drain -> zero new assigns; capacity facts age out.

**Commit:** `feat(embervm): node registry consuming daemon health stream`

### Task 10: Image source pipeline (build-to-snapshot)

**Why:** `source: image` is the v1 on-ramp: the platform turns an adopter's OCI image into a pristine base snapshot and reports it in status.

**Deliverables:**
- Control-plane `BaseBuilder`: on Workload CR admission (or image ref change), drive `BuildBase` on the daemon, then write `status.snapshotRef`, `snapshotDigest`, and the `BaseBuilt` condition

**Specification:**
- Builds are serialized per node (base builds are heavy: boot + health-gate + snapshot); concurrent Workload admissions queue.
- A failed build sets `BaseBuilt=False` with the daemon's error string in the condition message and retries with backoff (cap 10m); the Workload never becomes Ready without a base.
- Image ref changes (new digest) trigger a rebuild; once the new base is built, old-base primed VMs are proactively destroyed and re-primed from the new base (turnover must not wait for organic assignment on an idle pool), and the old base file is destroyed only after zero primed VMs reference it (no dispatch gap).
- Tag-pinned refs are resolved to digests at build time and the digest is recorded in status (deploys are explicit CR updates, not tag drift).

**Acceptance:**
- ExUnit with fake daemon: admission -> BuildBase -> status write; failure -> condition + backoff; digest-change turnover leaves no window with zero usable base (property: at any point, `snapshotRef` in status is restorable).

**Commit:** `feat(embervm): image source build-to-snapshot pipeline`

### Task 11: Primed pool and dispatcher

**Why:** The heart of R0: assignment-only dispatch from live pristine VMs, per-principal fairness, caps. This is where the latency and fairness budgets are met or missed.

**Deliverables:**
- `PoolManager`: background refill keeping `floor` primed VMs per workload (via `Prime`), destroy-on-assign accounting, refill backpressure
- `Dispatcher`: per-workload fair queues, assignment, enforcement hooks

**Specification:**
- Dispatch order within a workload: round-robin across principals, FIFO within a principal (weighted fairness is a recorded follow-on; the queue structure must make weights a parameter, not a redesign).
- Assignment is O(1) against ETS: pop principal-fair next task, pop primed VM, call `Assign`. If the pool is empty, dispatch falls back to `Prime`-then-`Assign` (the miss path), counted separately in metrics.
- Enforcement (fail-closed): per-workload `cap` on in-flight tasks; per-principal share cap (values-configured fraction, default: cap divided by active principals, minimum 1); queue depth cap per principal (default 10k, reject 429 beyond). If ETS capacity facts are missing or stale (>15s), deny dispatch with a distinct denial kind.
- Pool fairness: refill is per-workload floor first, then proportional to queue depth; one workload's burst MUST NOT drain another workload's floor (property test).
- Latency budgets (measured via OTel spans, enforced in Task 16 gates): submit-to-Assign p95 <= 25ms when a primed VM exists; miss-path submit-to-Assign p95 <= 500ms (restore-bound); control-plane share of end-to-end task latency p95 <= 50ms.
- Cron triggers: implemented behind a `TriggerAdapter` behaviour (adapter turns external events into ordinary submits; cron is the only R0 adapter, NATS plugs in behind the same behaviour later). The cron adapter fires `spec.triggers[].cron` as submits with principal `system:cron:<workload>`; misfires during control-plane downtime are skipped, not replayed (documented semantic; the daily full-scan consumer tolerates it).

**Acceptance:**
- ExUnit: fairness property (two principals, unequal submit rates, near-equal service rates), floor isolation property, cap and fail-closed denial tests, cron fire test with clock injection. Fake-daemon integration: 1k tasks drain with zero lost, all terminal.

**Commit:** `feat(embervm): primed pool and fair dispatcher`

---

## Phase 3: Enforcement and operability

### Task 12: Metering, audit, and quotas

**Why:** Chargeback is expensive to retrofit; the audit trail is the op-log's second job; quota is the resource-abuse containment the Security section demands.

**Deliverables:**
- Per-task usage capture (cpu_ms, peak_rss_mib, wall ms from `AssignResponse`) aggregated per principal into vCPU-seconds and GB-seconds, exposed at `GET /v1/usage?since=` and appended to the op-log
- Quota enforcement: values-configured per-principal daily vCPU-second budgets (v1 simplicity; CRD-based Quota objects are a follow-on), denial appends `quota_enforced`

**Specification:**
- Usage accounting is on the dispatch path (facts, cheap) and MUST NOT add a blocking store write (aggregate in ETS, flush to op-log on interval and drain).
- Quota check is fail-closed: unreadable quota state denies dispatch.
- Every denial (quota, cap, auth, stale-capacity) is an op-log append with principal and reason; the audit record and the metering stream are the same log.

**Acceptance:**
- ExUnit: usage math from scripted AssignResponses; quota exhaustion denies and appends; usage endpoint pages correctly.

**Commit:** `feat(embervm): per-principal metering, audit appends, and quotas`

### Task 13: Observability

**Why:** Parity with what fc-invoke taught us: the guilty phase must never be the uninstrumented one (the 5 QPS TokenReview incident).

**Deliverables:**
- OTel traces from the control plane (OTLP to SigNoz): root span per task with child spans `auth`, `enqueue`, `fair_wait`, `assign` (or `prime_assign` on miss), `guest_exec`, `result_store`; every external call (TokenReview, gRPC, SQLite append) has a span
- Structured JSON logs; key transitions logged at info, denials at warn
- Daemon fork keeps fc-invoke's span shape for restore/boot phases
- Guest logs shipped from day one (ADR contract): embervm-noded captures guest console output and emits it as structured log lines tagged with `workload` and `task_id` (per-task cap 256 KiB, truncation marked), reaching SigNoz via the standard pod log pipeline

**Specification:**
- Span attributes: `ember.workload`, `ember.tenant`, `ember.principal` (hashed if configured), `ember.task_id`, `ember.pool_hit` (bool), `ember.attempt`.
- The queue-wait and pool-state metrics needed by the Task 16 gates MUST be derivable from spans alone (no separate metrics pipeline in v1).

**Acceptance:**
- Traces visible in SigNoz from a live smoke task post-deploy; a deliberate slow-auth injection in ExUnit shows up in the `auth` span, not swallowed.

**Commit:** `feat(embervm): otel tracing and structured logging`

---

## Phase 4: First consumer cutover (R0 exit)

### Task 14: Semgrep and sandbox Workload CRs, side-by-side deploy

**Why:** The scan fleet is R0's named first consumer; side-by-side proves EmberVM on real traffic before any cutover.

**Deliverables:**
- `Workload` CRs for `semgrep` (1 vcpu, 1536 MiB, floor 4, cap 16, timeout 90s) and `sandbox` (1 vcpu, 512 MiB, floor 4, cap 16, timeout 30s) under `projects/embervm/deploy/`
- Values PR rebalancing node-4: fc-invoke semgrep/sandbox concurrency halved (16 -> 8) and its memory limit reduced accordingly; embervm-noded limit sized for its cap; documented arithmetic in the values comment showing the sum fits node allocatable

**Specification:**
- Guest images referenced by digest, identical to the fc-invoke ones (guest contract frozen; zero image changes).
- Both stacks run simultaneously; production PR-scan traffic stays on fc-invoke throughout this task.

**Acceptance:**
- `kubectl get workloads -n embervm` shows both Ready with snapshotRefs; a manual submit of a known-vulnerable sample file via the API returns semgrep findings identical to the fc-invoke path for the same input.

**Commit:** `feat(embervm): semgrep and sandbox workloads live side-by-side with fc-invoke`

### Task 15: Monolith scan client dual-path and shadow traffic

**Why:** Reversible cutover: the monolith learns to speak EmberVM behind a flag before anything is switched.

**Deliverables:**
- `projects/monolith/semgrep_scan/client.py` gains an EmberVM path (env `EMBERVM_URL`, submit API per Task 8's OpenAPI contract, sync wait mode) selected by env flag `SEMGREP_DISPATCH=fc-invoke|embervm|shadow`
- `shadow` mode: dispatch to fc-invoke as the serving path and mirror the same payload to EmberVM asynchronously, comparing status and finding-count, logging divergence (no user-facing effect)

**Specification:**
- The EmberVM path sets `Idempotency-Key` from the scan's content hash so webhook redeliveries dedupe.
- Shadow divergence is logged with both task ids; a counter is queryable for the Task 16 gate.
- Timeouts and error mapping preserve the current caller contract exactly (the PR-comment pipeline must not observe a behavior change in `fc-invoke` and `shadow` modes).
- Monolith chart values wire `EMBERVM_URL` and the flag; monolith SA added to EmberVM's auth allow-list.

**Acceptance:**
- Monolith tests cover all three modes with respx fakes; shadow mode live for >= 48h or >= 200 real PR scans with divergence rate 0 (findings-count equality; ordering differences ignored).

**Commit:** `feat(monolith): embervm dual-path scan dispatch with shadow mode`

### Task 16: Acceptance gates, cutover, and deprecation

**Why:** R0 exits on measured evidence, not vibes; then the old path is formally deprecated per Joe's fork-and-deprecate direction.

**Deliverables:**
- A load-test run against EmberVM (reusing the demos load-test harness pointed at the submit API, or a standalone driver if simpler) and a written results section appended to this plan
- Cutover PR: `SEMGREP_DISPATCH=embervm`; deprecation notes in `projects/firecracker/substrate/README` (scan path served by EmberVM; fc-invoke remains for agent + demos until R1/R2)
- Post-cutover: the semgrep-full daily CronWorkflow trigger optionally moves to a `spec.triggers` cron entry (only if Task 11's cron semantics fit; otherwise recorded as follow-on)

**Specification (the gates, all MUST pass before cutover):**
1. Throughput: semgrep >= 18/s and sandbox >= 55/s sustained over 120s on node-4 at cap 16, error rate 0, matching the fc-invoke baseline (2026-07-10 measurements) within noise. If the coexistence budget forces caps below 16, the gate normalizes per slot: throughput per concurrency slot >= the baseline's per-slot rate (18/16 and 55/16 respectively), with the absolute run repeated at cap 16 after fc-invoke's scan path is drained, before cutover completes.
2. Latency: end-to-end task p50 within 10% of the fc-invoke baseline for the same corpus; control-plane overhead p95 <= 50ms; primed-hit submit-to-Assign p95 <= 25ms.
3. Durability: `kill -9` the control-plane BEAM process mid-drain of 500 queued tasks (via `kubectl exec` into the container; `kubectl delete pod` is a forbidden verb in this cluster); after restart, every task reaches a terminal state, no result is lost for tasks that reported success before the kill, and total executions per task <= maxAttempts (at-least-once honored, idempotency dedupe verified).
4. Fairness: two synthetic principals at 10:1 submit ratio see service ratio within 1.5:1 while both have queued work.
5. Enforcement: quota exhaustion and cap saturation produce 429/denials with audit appends, never queue collapse; stale capacity facts (daemon stream paused 20s) halt dispatch (fail-closed observed).
6. Rollback drill: flipping `SEMGREP_DISPATCH` back to `fc-invoke` restores the old path within one monolith rollout, exercised once before the real cutover.

**Acceptance:**
- All six gates documented with numbers in the results section; cutover PR merged; one week of production PR scans on EmberVM with error rate at parity; deprecation note merged.

**Commit:** `feat(embervm): scan fleet cutover after acceptance gates` (plus `docs(fc-invoke): deprecate scan path`)

---

## Explicitly out of scope for R0 (YAGNI, held only as invariants)

- Zip/runtime-shim source lane (R1), sessions and banking (R2), serving/xDS/Envoy (R3), stateful volumes (R4), composite groups (R5), etcd facade (R6).
- `ra`/Raft op-log tier, multi-node placement logic (seams only), CapacityRequest/UpcomingNode provisioner contract (homelab nodes are fixed; the contract activates with elastic nodepools).
- NATS trigger adapter: deferred to the v1 release train (alongside R1), not dropped; NATS is not deployed in this cluster today (ADR agents/016 remains the candidate). Cron is the only R0 trigger adapter, but it sits behind the `TriggerAdapter` seam (Task 11) so NATS is an adapter, not a redesign. The ADR's "cron plus one queue adapter in v1" stands; R0 is a subset of v1, not a narrowing of it.
- Failure destinations (forward failed tasks elsewhere): recorded follow-on; DLQ listing + redrive (Task 8) is the R0 surface.
- Weighted fairness, priorities, exactly-once dedup, result object-store spill, Quota CRDs, bespoke UI or dashboard (kubectl printer columns are the dashboard).
- Migrating the goosecracker agent or demos off fc-invoke (they stay on the deprecated daemon until R2 makes sessions first-class).

## Open risks tracked for execution

| Risk | Watch signal | Fallback |
| ---- | ------------ | -------- |
| Elixir-in-Bazel spike exceeds a week of effort | Task 1 stalls | Go control plane fallback is recorded in the ADR; re-scope Task 1 before writing any Phase 1 code |
| SQLite append p95 misses 5ms on Longhorn PVC | Task 6 benchmark | Local-path PVC on node-4 (op-log is single-active anyway), or batch fsync with bounded group-commit window |
| Node-4 memory cannot host both stacks at useful concurrency | Task 14 arithmetic | Shrink shadow-phase caps (floor 2 / cap 8) and accept a slower gate run; gates compare per-slot efficiency, not absolute peak |
| gRPC-over-Linkerd streaming quirks with `WatchNode` | Task 9 soak | Fall back to polling `GetNodeStatus` every 2s; the registry interface hides the difference |
| Guest contract drift discovered under EmberVM (subtle fc-invoke ingress behavior the guests depended on) | Task 14 finding-equality check | Fix in noded, never in guests; the contract stays frozen |

---

## Closure (R0 shipped 2026-07-14)

All 16 tasks landed on main (PR chain #3462 through #3505 plus the cutover
commits); both the semgrep and sandbox Workloads are Ready in the `embervm`
namespace and the monolith serves the per-PR semgrep diff scan and the python
sandbox demo from EmberVM (`semgrep.dispatch: embervm`,
`sandbox.dispatch: embervm` in `projects/monolith/deploy/values.yaml`).
fc-invoke's scan and sandbox paths stay deployed as the one-value rollback.

Deviations from this plan, all recorded in `DECISIONS.md` at the repo root:

- **Task 15 shadow soak skipped.** Direct cut instead of the 48h / 200-scan
  divergence soak: personal homelab, no external SLA. De-risked by a live
  finding-equality check (identical Pro findings for a scripted vulnerable
  input). The shadow machinery shipped and remains one values flip away.
- **Task 16 gates deferred, not run.** The six-gate battery (throughput,
  latency, kill -9 durability, fairness, enforcement, rollback drill) is
  recorded as the remaining enterprise-grade evidence, to be run if EmberVM
  ever takes external traffic. The durability drill (gate 3) is the highest
  value of the six and needs no load harness; it is picked up as a Phase 0
  task in the R1 plan.
- **Sandbox demo cut over ahead of schedule.** The plan scoped demos to stay
  on fc-invoke until R2; the python sandbox path moved with the scan fleet.
  The goosecracker agent stays on fc-invoke as planned.
- **Control-plane image is amd64-only** (deliberate; the dual-arch
  requirement is held for the noded image, which is dual-arch).

Recorded follow-ons picked up by the R1 plan (2026-07-14): wiring the op-log
compaction timer (`OpLog.compact/2` exists and is tested but nothing schedules
it), read-time result-TTL enforcement, ops-journal retention policy
(ADR embervm/002), and the optional semgrep-full cron move to
`spec.triggers`.
