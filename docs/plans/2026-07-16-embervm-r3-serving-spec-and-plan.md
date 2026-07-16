# EmberVM R3 (Serving) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or superpowers:executing-plans in a separate session) to implement this plan task-by-task. This document is the committed spec for rung R3 of [ADR embervm/001](../decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md), bounded by [ADR embervm/003](../decisions/embervm/003-control-plane-managed-snapshot-distribution.md) (snapshot residency constrains where a serving workload can wake) and shaped by [ADR networking/001](../decisions/networking/001-cloudflare-envoy-gateway.md) (Envoy Gateway is already the cluster ingress; R3 programs Envoy below it, never around it). Every task is a specification with acceptance criteria; no implementation lives here.

**Goal:** Ship EmberVM R3: warm request serving at fleet scale. A serving-class workload is a tenant web API running in one or more long-lived microVMs, each handling many requests. The rung's headline property is the ADR 001 hit/miss invariant made real: **a request to a live serving VM travels edge Envoy to node Envoy to VM and never touches the control plane or the node daemon**; the control plane is on the path only for a miss (no live endpoint), where it parks the request, wakes or creates a VM, publishes the endpoint via xDS, proxies the parked request activator-style, and exits. The named first consumer is the `og-image` FaaS function re-registered as a serving-class workload: real public request traffic (link unfurlers fetching Open Graph images), stateless, and a measurable before/after against its task-class fresh-VM-per-request shape.

**Architecture:** The node contract gains serving verbs and network facts: a serving VM boots with a tap NIC and a node-routable IP, the daemon reports `{ip, port, health}` per serving VM in `NodeStatus` and runs the local health probe (the daemon is the health authority, ADR 001 data plane). The control plane gains a `ServingStore` (instances, endpoints, lifecycle FSM projected from new op-log records), an `EndpointPublisher` that projects endpoint facts into xDS snapshots, and an `Activator` module owning the miss path. xDS itself is served by a small Go sidecar built on `go-control-plane` living in the control-plane pod (Elixir decides, Go serves; the facts/payloads and keep-BEAM-out-of-proxies rules applied to the config plane). The data plane is one new Envoy tier: a per-node **ember Envoy DaemonSet** on serving-labeled nodes that fans into local VM IPs, fronted by the existing Envoy Gateway, which needs only one static, GitOps-managed route per serving hostname pointing at the node tier's stable Service. Bank/wake churn is therefore a node-local xDS update; Envoy Gateway's configuration never changes at runtime.

**Tech Stack:** Elixir/OTP control plane and Go noded (existing), `go-control-plane` (SotW ADS, gRPC) in a Go sidecar, Envoy (upstream image) as the node tier, Envoy Gateway + cloudflared as the existing edge (ADR networking/001), gRPC serving verbs on `embervm.node.v1`, SQLite op-log projections (ADR 002 discipline), Kubernetes Workload CRD (`class: serving` + `spec.serving` block), Helm + ArgoCD.

---

## Standing decisions (settled, do not relitigate during execution)

1. **The control plane is off the hit path, provably.** No per-request code path in the control plane, noded, or any TokenReview runs for a hit. The closure gate for this is destructive: stop the control plane while serving traffic flows and the traffic keeps flowing. Any design choice during execution that would put a per-request call into the control plane (auth callouts, ext_authz to ember, per-request metering RPCs) is wrong by definition and must come back to this table.
2. **Endpoint model: one Envoy cluster per serving workload, VM endpoints via EDS.** Cluster name `serve|<workload>`; its ClusterLoadAssignment is the set of live serving VM `ip:port` endpoints for that workload; Envoy load-balances across them (round robin v1). Routing is per-workload (host or host+path prefix to cluster), not per-VM and not per-session. Per-session xDS publication is explicitly NOT this rung (see out of scope: the session-token auth check has no home on an Envoy-direct path yet, and agent-turn traffic is too low-rate to care).
3. **Miss signal: the activator is the fallback endpoint of an empty cluster.** When a workload has zero live endpoints, the control plane publishes ITSELF (the activator listener) as the cluster's sole endpoint. Envoy therefore always has a healthy endpoint and needs no aggregate-cluster or 503-retry choreography; a request arriving at the activator IS the miss signal by construction. On wake, the CLA swaps to the real VM endpoints; stragglers that raced the swap are simply proxied by the activator (lifecycle-rate, bounded by the xDS propagation window). Each route sets a `x-ember-workload: <name>` request header so the activator resolves the target without parsing hosts.
4. **The node daemon is the health authority; Envoy active health checks are node-tier only and deferred.** noded probes each local serving VM's declared health path on a values-configured cadence and reports health in `NodeStatus`; the control plane ejects unhealthy endpoints from EDS. This is the ADR 001 "orchestrator-driven EDS for membership and lifecycle" rule. Envoy-side active health checks at the node tier are a recorded follow-on for faster data-path ejection between status sweeps, not v1.
5. **Drain-before-bank is mandatory and ordered.** Banking a serving VM proceeds: remove the endpoint from EDS, wait the propagation-plus-drain interval (`drainSeconds`, default 5), then `Bank`. If the removal empties the cluster, the same EDS update installs the activator endpoint, so a request arriving mid-bank parks and wakes rather than 503ing. A wake mid-bank waits for the bank to complete, then relights (the R2 no-cancel rule).
6. **Serving VMs have a NIC; task VMs still do not.** The serving class gets a tap device with an IP from a per-node serving subnet, routable from pods on that node. This changes nothing for the task class (vsock-only, no NIC remains the isolation statement) and follows the ADR 001 serving row: the code is the tenant's own app, its users are the requests. Egress from serving VMs stays deny-by-default at the tap (ingress-only NIC in v1); brokered egress is the recorded follow-on.
7. **Serving-path auth belongs to the route, not to ember.** ADR 001's serving row makes requests the tenant app's own users; who may reach a hostname is governed where it is today: Cloudflare Access and Envoy Gateway policy on the edge route (public-tier checklist applies to any public hostname). Ember's management API auth (TokenReview) governs lifecycle, never requests. No per-request ember auth exists on the hit path (see decision 1).
8. **Runtime state never becomes Kubernetes objects.** The control plane writes no HTTPRoutes, no EndpointSlices, no Backends at runtime. The Envoy Gateway route per serving hostname is static, GitOps-managed YAML in the chart; everything that churns (endpoints, activator fallback) lives in the xDS snapshot served from memory. This is ADR 001's off-etcd thesis applied to the config plane and is the reason Option A below is rejected.
9. **Scale-to-zero rides R2 machinery, with Envoy stats as the idle signal.** Serving VMs idle-bank exactly like sessions (idle timer, bank to node-local snapshot, relight on demand), but the control plane cannot see requests it is not on the path of, so idleness is derived from a low-cadence scrape of the node Envoy's per-cluster `upstream_rq_total` counters: no delta across `idleBankSeconds` means idle. The scrape is a sweep, level-triggered, and entirely off the hit path. Request-level metering via Envoy's gRPC access-log service (ALS) is the recorded richer follow-on; v1 usage is VM live-seconds (from lifecycle ops) plus request-count deltas (from the same scrape, appended as `serving_stats` ops).
10. **Snapshot residency bounds placement, as in R2.** Banked serving snapshots are node-local NVMe; a workload wakes only on a node holding its snapshot (or cold-creates from the pristine base where the base is warm). ADR 003's Restore/Export distribution loop is the recorded lever that later frees this constraint; R3 must consume residency facts through the same placement seam (`node_for_relight`-shaped) so distribution arrives without redesign. v1 runs one serving node, so the constraint is theoretical, but the module boundary is reviewer-checked now.
11. **The node reports, the control plane adopts.** Serving VMs (`serving_vms` with endpoint and health) join `NodeStatus`, and boot/sweep reconciliation rebinds live serving VMs, heals limbo states, and re-derives the full xDS snapshot from adopted facts. A control-plane restart must converge the xDS snapshot to reality without touching any VM (the #3517 lesson, third application).
12. **Additive proto only; `Assign` and the task and session contracts stay frozen.**

### Fork 1: who serves xDS (settled choice)

How live endpoints become Envoy configuration, ranked:

- **Option A: program Envoy Gateway via its CRDs (HTTPRoute/Backend/EndpointSlice writes).** Simplest to write, reuses the deployed EG control plane wholesale. Rejected on the founding thesis: every bank/wake would be a Kubernetes API write plus an EG reconcile, putting high-churn execution state back into etcd with seconds-scale publication latency, and EG exposes no seam for the activator-as-fallback-endpoint miss signal. EG stays exactly what ADR networking/001 made it: the stable edge.
- **Option B: a bespoke ADS server inside the Elixir control plane.** Single runtime, no new container. Rejected on toolchain cost versus value: the Envoy xDS proto tree (envoy api, xds, udpa, validate, google apis) is a large vendored-proto surface, and the hermetic-hex Elixir codegen lane has already proven expensive for far smaller trees (the OTEL saga). The control plane would be hand-rolling protocol mechanics `go-control-plane` has battle-tested for years.
- **Option C (chosen): a small Go xDS sidecar on `go-control-plane`, decisions stay in Elixir.** A second container in the control-plane pod serving SotW ADS (CDS/RDS/EDS; LDS static in the Envoy bootstrap) from a `SnapshotCache`, fed by the Elixir control plane over a localhost-only HTTP snapshot API (full desired state per Envoy node-id, level-triggered, re-pushed on boot and on every change; the sidecar holds no logic and no durable state). This is the ADR 001 language rule applied to the config plane: Elixir coordinates facts, Go serves the protocol; it reuses the reference library, keeps the proto surface out of the Elixir toolchain, and shares the control-plane pod's lifecycle so a restart of either half converges by a full re-push. The sidecar is deliberately dumb enough that replacing it later (with an Elixir ADS or anything else) is a contained swap behind the snapshot API.

### Fork 2: tier layout and endpoint reachability (settled choice)

Where the new Envoy tier sits, given VM tap IPs are only cheaply reachable on their own node, ranked:

- **Option A: a cluster-level ember edge Envoy fleet routing straight to VM IPs.** The literal "edge tier first, node tier deferred" reading of ADR 001. Rejected for v1 on a networking fact: VM tap IPs live on per-node bridges and are not cluster-routable today (making them so is a CNI/routing project, plausibly post-Cilium); a cluster-level fleet would need exactly that before serving its first request.
- **Option B (chosen): the existing Envoy Gateway is the edge tier; build the per-node tier now.** An **ember node Envoy** DaemonSet on nodes labeled `embervm.io/serving=true`, hostNetwork-adjacent to its VMs (same node, so tap IPs are locally routable), taking ADS from the control-plane sidecar and fanning into local VM endpoints. Envoy Gateway fronts it through one static route per serving hostname whose backend is the node tier's stable per-node Service. This IS the two-tier layout from ADR 001, with the already-deployed EG playing the edge role: the edge knows only stable per-node addresses, endpoint churn is node-local xDS, and no probe amplification reaches the edge. v1 has one serving node so EG-to-node routing is trivial; when the fleet grows, the recorded seam is either node-affine routing hints at EG or a true ember edge tier consuming a second (cluster-scoped) xDS snapshot from the same sidecar, and nothing in v1's naming (`serve|<workload>` clusters, node-scoped snapshot keys) precludes either.
- **Option C: reuse the node daemon as the serving proxy.** Rejected outright: ADR 001 forbids it in one sentence (serving traffic never traverses the daemon), and it puts payload routing in the component whose compromise blast radius is supposed to exclude request traffic.

## Cross-cutting constraints

- **No local test loop.** Implement, commit, push, watch BuildBuddy CI (`gh pr checks <n> --watch`). ExUnit, Go, and pytest targets run under `bazel test //...` in CI only.
- **Conventional Commits; no em-dashes anywhere.**
- **Charts bump via `bazel/tools/git/bump-chart.sh`** in the same PR as the code they deploy. Docs-manifest regeneration (this plan, ADR touches) forces monolith AND monolith-public chart bumps.
- **Public tier checklist** (`docs/runbooks/public-tier-checklist.md`) applies before any serving hostname is publicly exposed (Task 11's flip step).
- **RBAC verbs verified per task** for every new K8s API call before merge (the node Envoy DaemonSet and its ConfigMap are chart-managed, not runtime-written, so the expected new runtime RBAC is zero; the reviewer confirms that stays true).
- **Op-log schema changes use the guarded ALTER-after-DDL migration pattern.**
- **Additive proto only:** new RPCs and fields; existing verbs and messages untouched.
- **New Go module deps (`go-control-plane`, Envoy protos) enter through bzlmod/gazelle;** verify BCR/module availability before writing code that imports them.
- **Repository layout:** control plane under `projects/embervm/control/`, xDS sidecar under `projects/embervm/xds/` (new Go binary + apko image, dual-arch), noded under `projects/embervm/noded/`, node Envoy chart pieces under `projects/embervm/chart/`, edge route under the serving hostname's owning chart.
- **One comprehensive code review per merged PR.**

## Suggested PR partitioning

| PR | Tasks | Deploys |
| -- | ----- | ------- |
| PR-0 docs (this branch) | this plan | manifests + monolith/public chart bumps |
| PR-1 contract | 1, 2, 3 | additive proto + op-log schema + CRD; no behavior change |
| PR-2 noded serving | 4 | serving verbs, tap networking, health probe live on noded, unused |
| PR-3 xDS tier | 5, 6 | xds sidecar + node Envoy DaemonSet serving a static hello cluster |
| PR-4 serving core | 7, 8 | endpoint publication + activator; hit and miss paths live end to end |
| PR-5 lifecycle economics | 9 | idle-to-bank, scale-to-zero, drain ordering live |
| PR-6 operability | 10 | serving spans, publication-latency metric, alerts |
| PR-7 consumer | 11 | og-image serving workload live, gated flip |
| PR-8 closure | 12 | R3 marked shipped in ADR 001 |

---

## Phase 0: Contract and schema foundations

### Task 1: Node contract serving verbs and endpoint facts

**Why:** Serving needs a VM that is created-or-relit WITH a network endpoint, stays alive across requests it receives directly, and is health-probed locally. The daemon "creates the tap, reports the endpoint, and is thenceforth uninvolved" (ADR 001); the contract encodes exactly that.

**Deliverables:**
- Additive RPCs on `embervm.node.v1.NodeService`, proto comments as the spec:
  - `rpc StartServing(StartServingRequest) returns (StartServingResponse)`: bring up a serving VM either fresh from a pristine base ref or relit from a serving snapshot ref (one request field, oneOf source), attach a tap NIC with an IP from the node's serving subnet, wait for guest HTTP readiness on `{port, health_path}` via the tap (not vsock), and return `{vm_id, ip, port}`. `FAILED_PRECONDITION` on unknown refs; readiness timeout destroys the VM and returns the error (no half-alive endpoints).
  - `rpc StopServing(StopServingRequest) returns (StopServingResponse)`: with `mode = BANK` (pause, snapshot to the sessions-style bundle dir under a `serving/` prefix, destroy, return `{snapshot_ref, size_bytes}`) or `mode = DESTROY` (no snapshot). Refuses `BANK` if a bank of the same vm_id is already in flight.
  - Serving snapshots reuse `EvictSnapshot` (R2 Task 1) unchanged.
- `NodeStatus` additions (wire-compatible): `serving_vms: [{vm_id, workload, ip, port, healthy, last_probe_unix_ms}]`, `serving_snapshots: [{snapshot_ref, workload, size_bytes, created_at_unix_ms}]`, `serving_subnet_cidr`.
- Health-probe contract in proto comments: the daemon probes `GET {health_path}` on each live serving VM every `probe_interval` (daemon flag, default 5s), flips `healthy` after `unhealthy_threshold` consecutive failures (default 3) and back after one success; the control plane consumes the fact, the daemon never acts on it.
- fakenode serves the new verbs and status fields.

**Specification:**
- Refs stay opaque; no file paths or netlink details cross the seam. IP assignment, tap naming, and subnet management are daemon-internal; the contract carries only the resulting `{ip, port}` fact.
- Serving VMs count against `max_live_vms` like every other VM and are excluded from `primed_vm_ids`.
- `StartServing` carries `workload`, `Trace`, and the resource shape (vcpu/memMib) exactly as `Prime` does; the guest wire contract is unchanged (listen on the declared port, answer the health path; the ADR 001 image-source contract verbatim).
- Go+Elixir stubs regenerate through the existing pure-genrule codegen; fake-server round-trip tests per verb in CI.

**Acceptance:** CI green; reviewer confirms additivity, that task/session verbs are untouched, and that nothing Envoy-specific leaks into the node contract (the daemon must not know xDS exists).

**Commit:** `feat(embervm): serving verbs, tap endpoints, and health facts on the node contract`

### Task 2: Op-log serving records and projection

**Why:** Serving lifecycle must be durable, ordered, and auditable like tasks and sessions, and endpoint publication events belong in the audit stream (a published endpoint is an enforcement-adjacent fact: it decides who traffic reaches).

**Deliverables:**
- Additive op kinds: `serving_started, serving_published, serving_unpublished, serving_banked, serving_relit, serving_evicted, serving_destroyed, serving_failed, serving_stats`.
- New projection table: `serving_instances(instance_id TEXT PRIMARY KEY, tenant, principal, workload, state, node_id, vm_id, ip, port, base_snapshot_ref, base_digest, generation INTEGER, snapshot_ref, snapshot_size_bytes, created_at, last_active_at, updated_at, terminal_reason)`, plus a nullable `serving_instance_id` column on `ops` (guarded ALTER-after-DDL).
- `serving_stats` ops carry `{workload, rq_delta, window_ms}` from the Task 9 scrape; usage upserts the `(principal, day)` projection in the same transaction (live-seconds accrue from `serving_started`/`serving_banked`/`serving_destroyed` timestamps, request counts from stats deltas).
- Retention per ADR 002: terminal instances prune on the hourly sweep past retention; non-terminal instances pin their ops.

**Specification:**
- State transitions are write-through appends before ETS visibility, the standing task/session discipline. The xDS snapshot is NEVER derived from the durable store on the publish path; it is projected from ETS facts, and the op is the audit record.
- `serving_published`/`serving_unpublished` carry `{instance_id, ip, port, reason}` where reason is one of `started|relit|healthy` and `drain|unhealthy|banked|destroyed|failed`; this makes the endpoint-lifetime story reconstructable from the log alone.
- ExUnit: projection rebuild equivalence from a scripted op sequence; retention never prunes a non-terminal instance; the usage upsert matches the D12.1 pattern.

**Acceptance:** CI green; kill-and-restart test extended to rebuild serving state exactly.

**Commit:** `feat(embervm): serving instance records, endpoint audit ops, and usage in the op-log`

### Task 3: Workload CRD `class: serving` and the serving spec block

**Why:** The definition surface. Serving needs the endpoint contract (port, health path, hostname) and the elasticity knobs (min/max instances, idle policy) declared, not inferred.

**Deliverables:**
- CRD: `class: serving` accepted by the watcher, requiring `spec.serving`: `{port (required), healthPath (default "/healthz"), host (required; the serving hostname this workload owns), minInstances (default 0; 0 means scale-to-zero), maxInstances (default 2), idleBankSeconds (default 300, min 30; only meaningful when minInstances is 0 or instances exceed min), drainSeconds (default 5), maxLifetimeSeconds (default 86400; the version-convergence bound, as sessions)}`. `spec.concurrency` reads: `cap` = maxInstances alias guard (must agree), `floor` unused for serving (minInstances is the floor concept).
- `status.serving: {live, banked, published}` counts + a `SERVING` printer column.
- Validation posture from R0/R2: wrong-class spec blocks get `Ready=False` with a precise condition; duplicate `host` across serving workloads is condition-rejected (one hostname, one workload, v1).
- Base refcounting from R2 Task 3 extends to serving instances (an instance pins its birth base until terminal; `maxLifetimeSeconds` bounds it).
- Sample serving CR under `projects/embervm/crd/samples/`.

**Specification:**
- Serving instances ride their birth base exactly like sessions (pinned digest, TTL-bounded convergence); a deploy converges a serving workload by natural instance turnover, and `maxLifetimeSeconds` is the guarantee. A rolling `bank-then-relight-on-new-base`? No: relight restores old memory by definition; convergence is destroy-and-recreate at TTL or on explicit operator action (a `DELETE /v1/serving/{workload}/instances` management call is included for forced rolls).
- ExUnit: watcher parse/validation tests including the duplicate-host rejection.

**Acceptance:** CI green; sample CR round-trips; `kubectl get workloads` shows serving columns.

**Commit:** `feat(embervm): serving workload class and serving spec block`

---

## Phase 1: The data plane (xDS tier bring-up)

### Task 4: noded serving implementation

**Why:** Tap networking, direct-traffic VMs, and the local health authority are new daemon mechanics; everything above depends on a VM that is genuinely IP-reachable.

**Deliverables:**
- Serving subnet management: a per-node bridge + CIDR (daemon flags, default a /24 from RFC1918 space reserved in values), tap-per-VM attached to the bridge, IP allocation from the range, teardown on destroy. Guest network config delivered via the existing MMDS/boot-args seam (static IP matching the allocation).
- Ingress-only posture: nftables rules on the bridge deny all VM-originated forwarding (established/related return traffic allowed); documented as the v1 egress stance (standing decision 6).
- `StartServing` both source modes: fresh (restore pristine base, attach NIC, hydrate if zip-lane, health-gate via tap) and relight (restore serving snapshot; the tap IP is re-allocated and MAY differ across relights, which is why the endpoint is re-reported and republished every wake; guests must not persist their own IP).
- `StopServing` bank mode reusing the R2 snapshot bundle mechanics (digest-versioned rootfs rules from D-R2.7 apply unchanged); destroy mode.
- Health probe loop per live serving VM per the Task 1 contract; results in `NodeStatus`.
- Inventory rescan on daemon start (serving snapshots dir), the adoption source of truth.

**Specification:**
- A daemon restart kills live serving VMs (Firecracker children); the control plane resolves each affected instance from post-restart inventory to `banked` (snapshot exists) or `failed`, and the xDS snapshot converges (Task 8's adoption). This is acceptable v1 availability; multi-instance workloads (maxInstances >= 2) are the mitigation an operator chooses.
- Bank ordering guard: `StopServing(BANK)` does not itself drain Envoy (the daemon does not know Envoy exists); the control plane guarantees drain-before-bank (standing decision 5) and the daemon just refuses concurrent banks.
- Go tests: fake-driver coverage for start-fresh, start-relight, IP allocation/reuse, health flip thresholds, bank round-trip, nftables rule generation (table-driven, rules as data), inventory rescan.

**Acceptance:** CI green; on deployed noded, a grpcurl `StartServing` of the sandbox guest (as a stand-in HTTP server) yields an `{ip, port}` that answers HTTP from a pod on that node (curl from a debug pod, documented with output in the PR description); `StopServing(BANK)` then `StartServing(relight)` round-trips.

**Commit:** `feat(embervm): serving VM networking, lifecycle, and health probing in noded`

### Task 5: The xDS sidecar (go-control-plane)

**Why:** Fork 1's chosen mechanism: the protocol server, deliberately logic-free.

**Deliverables:**
- New Go binary `projects/embervm/xds/`: SotW ADS gRPC server on `go-control-plane` (`SnapshotCache`, ADS mode), plus a localhost-only HTTP snapshot API: `PUT /snapshot/{envoy_node_id}` accepting the full desired state as JSON `{version, clusters: [{name, endpoints: [{ip, port}], connect_timeout_ms}], routes: [{host, path_prefix, cluster, request_headers: {…}}]}` which the sidecar translates into CDS/RDS/EDS resources and swaps atomically; `GET /snapshot/{envoy_node_id}` returns what is currently served (the debug surface); `GET /healthz`.
- Version strings are caller-supplied monotonic (the control plane's own counter), so a re-push after restart with a higher version always converges Envoy.
- apko image (dual-arch, non-root uid 65532), added as a second container in the control-plane pod: ADS gRPC on a pod port exposed via the existing embervm Service; snapshot API bound to 127.0.0.1 only.
- Chart wiring: container, ports, probes; `helm template` rendered to verify.

**Specification:**
- The sidecar holds NO durable state and makes NO decisions: an empty cache at boot serves nothing until the first `PUT`, and the control plane's boot sequence re-pushes before marking itself ready (Task 7). If the control plane container is down, Envoy keeps its last-ACKed config (xDS is eventually consistent by design), which is precisely what keeps hits flowing through a control-plane outage.
- Listener config is NOT served via xDS: the node Envoy's listener + HTTP connection manager are static bootstrap (Task 6), with RDS/CDS/EDS dynamic. This keeps the sidecar's resource surface to three types.
- Go tests: snapshot translation (JSON in, expected go-control-plane resources out, table-driven), version monotonicity, an in-process Envoy-less ADS smoke via the library's test harness, malformed-input 400s.

**Acceptance:** CI green; deployed sidecar answers `GET /snapshot/...` and serves ADS (grpcurl reflection or a curl of the admin surface documented in the PR).

**Commit:** `feat(embervm): go-control-plane xds sidecar with a level-triggered snapshot api`

### Task 6: Node Envoy tier and the static edge route

**Why:** Fork 2's chosen layout: the per-node fan-in tier, fronted by the existing Envoy Gateway.

**Deliverables:**
- Node Envoy DaemonSet in the embervm chart: upstream Envoy image, nodeSelector `embervm.io/serving: "true"` (label node-4 via the chart's docs, applied out-of-band as node labels are), static bootstrap ConfigMap: one HTTP listener on a fixed pod port, dynamic RDS/CDS/EDS over ADS pointing at the control-plane Service's xDS port, node-id set to the Kubernetes node name (downward API), admin endpoint bound to localhost + a stats port exposed for the Task 9 scrape.
- A stable `embervm-serving` Service selecting the DaemonSet (the per-node stable address the edge sees; v1 one node, ClusterIP suffices).
- The static edge route: an HTTPRoute on the existing private Envoy Gateway for the first serving hostname (Task 11 owns the real consumer's host; this task ships a `serving-hello.<private domain>` drill host) with backend `embervm-serving`. GitOps-managed, never runtime-written (standing decision 8).
- Bring-up proof without the control plane's serving core: a hand-pushed snapshot (curl the sidecar's PUT from the control-plane pod) containing one cluster pointing at a Task 4 drill VM, and a route for the drill host.

**Specification:**
- The DaemonSet runs with pod networking (not hostNetwork); VM tap IPs are node-local routable from pods via the Task 4 bridge, which the acceptance proves. If the CNI blocks pod-to-bridge traffic, the recorded fallback is hostNetwork for the DaemonSet; decide from evidence, not preemptively.
- Envoy resource sizing per the repo convention (CPU requests-not-limits, mem requests=limits), small (this is a homelab node tier, not the EG fleet).
- No Envoy config beyond the bootstrap lives in the chart: everything routable arrives via ADS.

**Acceptance:** CI green; end-to-end drill documented in the PR: curl `serving-hello.<private domain>` from outside the cluster traverses cloudflared, Envoy Gateway, node Envoy, and lands on a live serving VM, with the response body proving the guest answered. Config-dump from the node Envoy shows the ADS-delivered cluster.

**Commit:** `feat(embervm): per-node serving envoy tier behind the envoy gateway edge`

---

## Phase 2: Serving core (publication and the activator)

### Task 7: ServingStore, lifecycle FSM, and the EndpointPublisher

**Why:** The control-plane brain for the hit path's supporting cast: which instances exist, which endpoints are published, and the single projection from facts to xDS.

**Deliverables:**
- `Embervm.ServingStore`: ETS hot set (instances by id, endpoints by workload, per-workload counts) rebuilt from the projection on boot; write-through transitions. FSM states: `starting -> serving -> draining -> banking -> banked -> relighting -> serving ...`; terminal `expired, evicted, destroyed, failed`, illegal transitions raise.
- `Embervm.EndpointPublisher`: the ONLY writer to the xDS sidecar. Holds the desired snapshot as a pure function of ETS facts: for each serving workload, cluster `serve|<name>` with its healthy published endpoints, or the activator endpoint when none; routes from the workload catalog (`host`, header injection `x-ember-workload`). Debounced (50ms) coalescing, monotonic version counter persisted nowhere (restarts re-push at a higher epoch-prefixed version), full-state PUT per change and on boot before readiness.
- Health ejection: `NodeStatus` health flips unpublish/republish endpoints (with `serving_unpublished` reason `unhealthy` ops).
- `GET /v1/serving/{workload}` management API: instances, states, published endpoints, generation (the operator read surface).

**Specification:**
- Publisher input is facts, never the durable store; a projection rebuild followed by a publish must be byte-identical to the pre-restart snapshot (property test: scripted op sequences, rebuild, compare snapshots).
- The publisher never blocks lifecycle: a sidecar PUT failure retries with backoff and raises a loud log + (Task 10) alert; endpoints keep serving on Envoy's last-ACKed config meanwhile, which is safe by decision 1.
- Reviewer-checked boundary: no module other than EndpointPublisher references the sidecar API; the activator and lifecycle modules mutate facts and let publication follow.
- ExUnit: FSM exhaustive transitions, publisher pure-function tests (facts in, snapshot JSON out, including the empty-cluster activator swap in both directions), debounce coalescing, boot re-push ordering.

**Acceptance:** CI green.

**Commit:** `feat(embervm): serving store, lifecycle fsm, and the xds endpoint publisher`

### Task 8: The activator (miss path) and restart adoption

**Why:** The rung's second headline: a miss parks, wakes, publishes, proxies, and detaches, all at lifecycle rate.

**Deliverables:**
- `Embervm.Activator`: an HTTP listener (new route on the existing router, but front-end only) receiving requests Envoy routed to the fallback endpoint. Flow per request: resolve workload from `x-ember-workload`; park the caller (BEAM process, capped per principal by the existing park caps); coalesce concurrent misses for the same workload behind one wake (single-flight per workload); consult placement (residency fact for relight, warm-base node for cold create, through the ServingPlacement seam per standing decision 10); issue `StartServing`; on ready, transition to `serving`, publish (activator endpoint leaves the CLA in the same update), then proxy the parked request(s) to the VM over its `ip:port` with the original method/path/headers/body and stream the response back; subsequent requests arrive via Envoy directly.
- Wake-rate limits per principal (values-configured, default 30/min) and a parked-request cap per workload (default 64): excess misses get 429/503 with machine-readable reasons and audit ops, the ADR 001 asymmetric-cost guard.
- Wake failure (start error, readiness timeout): parked callers get 503 with a reason, instance marked `failed`, `serving_failed` appended; the activator endpoint stays published so the next request retries the wake (subject to the rate limit).
- Restart adoption: boot/sweep reconciliation over `serving_vms` + `serving_snapshots` rebinds live instances, heals `banking`/`relighting`/`draining` limbo from node facts, marks vanished instances `failed`, evicts orphaned snapshots, and re-derives + re-pushes the snapshot. A control-plane restart with live serving VMs must republish exactly the same endpoints without touching any VM.
- Scale-up beyond one: if a workload is at `minInstances > 0` or receives a miss while live instances exist but all are draining, the activator may start an additional instance up to `maxInstances`; v1 has NO load-based autoscaling between 1 and max (recorded out of scope), only miss-driven starts and idle-driven banks.

**Specification:**
- The proxy hop is lifecycle-rate by construction and must be implemented with streaming (no full-body buffering beyond the existing envelope caps); response headers carry through (the R1 header-carry rules).
- Single-flight correctness: N concurrent misses for one workload produce exactly one `StartServing` and N proxied responses (property test with fakenode latency injection).
- The activator never appears in the snapshot for a workload with healthy published endpoints (publisher invariant test).
- ExUnit: miss round-trip against fakenode (park, wake, publish, proxy), single-flight, rate-limit 429, wake-failure 503 + retry-ability, adoption matrix (restart during each non-terminal state converges correctly and republishes), straggler proxy (request arrives at activator while endpoints exist: proxied, not errored).

**Acceptance:** CI green; live drill in the PR description: with the drill workload banked, a single curl to the drill host returns the correct response (timed, park-to-first-byte), and a second curl is served with the control-plane access log silent (hit path proof); `GET /v1/serving/{workload}` shows the published endpoint.

**Commit:** `feat(embervm): activator miss path with single-flight wake and restart adoption`

### Task 9: Idle-to-bank, scale-to-zero, and drain ordering

**Why:** The economics: a serving workload with no traffic must cost disk, not VMs, and the road back must be exactly one slow request.

**Deliverables:**
- Stats scrape: a sweeper polls each node Envoy's stats port for per-cluster `upstream_rq_total` on a values-configured cadence (default 30s), computes deltas, appends `serving_stats` ops (usage per standing decision 9), and updates `last_active_at` on instances of clusters with a non-zero delta.
- Idle-bank: an instance idle past `idleBankSeconds` (zero delta across the window) AND above `minInstances` (or minInstances 0) triggers the drain-then-bank sequence: unpublish (reason `drain`), wait `drainSeconds`, `StopServing(BANK)`, append `serving_banked`. The last instance's unpublish installs the activator endpoint in the same xDS update (standing decision 5), so scale-to-zero is never a 503 window.
- Max-lifetime expiry and banked-TTL GC riding the R2 sweeper patterns: expire live instances (drain, destroy), evict stale banked snapshots, invoke-time-equivalent checks not needed (no invokes; the sweep is the only trigger, plus wake-time TTL check on relight).
- Per-node concurrent-bank cap shared with sessions (the existing default 1).
- Forced roll: `DELETE /v1/serving/{workload}/instances` (management auth) drains and destroys all instances so the next miss cold-creates on the current base (the Task 3 convergence lever).

**Specification:**
- Scrape failure fails open for warmth (no banking decision without fresh stats: an instance is never banked on stale idle data) and fails closed for nothing (no enforcement reads stats); this is the ADR 001 posture applied to the idle signal.
- Drain wait is wall-clock after the sidecar ACKs the PUT plus `drainSeconds`; v1 does not verify in-flight-zero via stats (recorded follow-on: drain by watching `upstream_rq_active`).
- ExUnit: idle detection from scripted stats sequences, min-instances floor respected, drain ordering (unpublish op precedes bank op in the log, activator installed atomically with last unpublish), TTL expiry, forced roll; clock-injected timers throughout.

**Acceptance:** CI green; live: the drill workload left idle past `idleBankSeconds` shows `banked` with zero live VMs (kubectl + `/v1/serving/...`), and the next curl wakes it (timings recorded).

**Commit:** `feat(embervm): idle-to-bank, scale-to-zero, and drain-before-bank for serving`

---

## Phase 3: Operability, the consumer, and closure

### Task 10: Serving observability and alerts

**Why:** Two new failure surfaces (xDS publication and the activator) and one new latency phase (wake) must be visible before real traffic depends on them; the hit path is deliberately invisible to the control plane, so its observability comes from Envoy.

**Deliverables:**
- Control-plane spans: activator root span per miss with children `park`, `placement`, `wake` (`ember.wake_ms`, `ember.cold` bool), `publish` (`ember.publish_ms`, sidecar ACK round-trip), `proxy`; lifecycle spans for `drain`, `bank`, `stats_sweep`. Attributes: `ember.workload`, `ember.instance_id`, `ember.principal`, `ember.endpoint_count`.
- Node Envoy access logs and stats shipped via the existing OTel collector arrangement (scrape config for the stats port; access logs to stdout collected as pod logs); the hit-path latency story is Envoy histograms, not ember spans.
- SigNoz alerts (METRIC_BASED_ALERT registration seam, threshold-0 dry-run then restore): sustained publication failure (sidecar PUT retries exceeded), activator error rate, serving snapshot-dir watermark shared with the R2 disk alert.
- `status.serving` counts wired (Task 3), debounced.

**Specification:** The Task 12 gate numbers (hit overhead, wake p95, publication latency, off-path proof) must be derivable from Envoy histograms plus ember spans alone.

**Acceptance:** CI green; spans and Envoy metrics visible in SigNoz from a live miss/hit/bank cycle; dry-run alert reaches Discord.

**Commit:** `feat(embervm): serving observability, envoy metrics, and publication alerts`

### Task 11: First consumer: og-image as a serving workload

**Why:** The rung's named consumer, per ADR 001 "Tenant web APIs" made concrete: og-image already runs as a FaaS (task-class) function with real public traffic, is stateless (safe for the shared-VM serving row), and gives a direct fresh-VM-per-request versus warm-serving comparison.

**Deliverables:**
- A `serving-og-image` Workload CR in the embervm chart: `class: serving`, the og-image zip-lane source (same runtime base + code sha identity the FaaS registration produced), `port`/`healthPath` per the zip shim's HTTP surface, `{minInstances 0, maxInstances 2, idleBankSeconds 600, drainSeconds 5, maxLifetimeSeconds 86400}`, sized per the fc-base sizing coupling (peak sets memMib).
- Edge route for its private drill host on the existing private gateway (replacing Task 6's hello host as the canonical drill route).
- The flip, gated and reversible: after gates 1-5 pass on the private host, route the production og-image path to the serving workload. The og-image production path is served through the monolith/public tier today, so the flip is a monolith-side upstream URL change (values-injected, never hardcoded) pointing at the serving host, with the public-tier checklist read first and the task-class FaaS registration kept intact as the instant rollback (flip the value back).
- Monolith chart bump in the flip PR per the bump rules.

**Specification:**
- The guest is unchanged: the zip shim already answers HTTP on a port with a health surface; if its health path is missing, add it to the shim additively (task-class byte-compatibility proven by existing guest tests).
- Before/after numbers recorded: task-class og-image p50/p95 (dispatch + fresh restore + render) versus serving warm-hit p50/p95 (Envoy histogram) and first-hit-after-idle (wake) p95.
- pytest for any monolith URL plumbing change (respx, hand-registered py_test); no new RBAC.

**Acceptance:** CI green; the private drill host serves og-image renders warm; the production flip is live with the rollback lever documented in the PR description; error rate on the flipped path is zero over a 24h soak before Task 12 closes.

**Commit:** `feat(embervm): og-image served warm on the serving class`

### Task 12: R3 gates and closure

**Specification (the gates, all measured, appended to this plan as a Closure section):**
1. **Control plane off the hit path, destructively proven:** with `serving-og-image` live and receiving a steady curl loop, scale the control-plane Deployment to zero for 60 seconds; every hit succeeds during the outage (zero non-2xx), and the node Envoy config-dump is unchanged. Restore the control plane; adoption republishes an identical snapshot (diff of before/after `GET /snapshot/...`).
2. **Hit overhead:** p95 added latency through the node Envoy tier (Envoy upstream histogram vs direct-to-VM curl from a node-local pod) <= 10ms; end-to-end warm-hit p95 through the full edge recorded for the before/after story.
3. **Miss/wake latency:** park-to-first-byte on a banked og-image instance p95 <= 750ms over 20 cycles (relight plus publish plus proxy); cold create (no snapshot) recorded, no gate.
4. **Publication latency and drain honesty:** endpoint publish (fact change to Envoy ACK) p95 <= 250ms from spans; a forced idle-bank under a steady request loop yields zero 5xx (drain-before-bank observed: the log shows unpublish strictly before bank, and the activator absorbs the tail).
5. **Scale-to-zero economics:** idle past `idleBankSeconds` reaches zero live VMs and one banked snapshot; the next request wakes it within gate 3's bound; `serving_stats` usage ops account the active window.
6. **Single-flight and abuse guards:** 50 concurrent curls against a banked workload produce exactly one wake (op-log count) and 50 successes; a wake-rate-limit drill returns 429s without any node call (span absence).
7. **Adoption drill:** restart the control plane with one live and one banked serving workload: the live one keeps serving throughout (gate 1 mechanics), the banked one wakes on demand afterward; zero orphaned VMs, snapshots, or stale endpoints after one sweep. Then restart noded: live instances fail loudly to `banked`/`failed` per the Task 4 posture and the workload recovers via the activator.
8. **Churn isolation:** across 10 bank/wake cycles, the Envoy Gateway's resources are untouched (resourceVersion/generation unchanged) and all config movement is node-tier xDS (config-dump version counter).
9. **Consumer soak:** the flipped og-image path serves 24h of production traffic with zero serving-attributed 5xx and the before/after latency table recorded.

**Deliverables:** Gate numbers in the Closure section; ADR embervm/001 roadmap row R3 to `Shipped <date>`; a note in ADR embervm/003 that serving residency is now a live consumer of the distribution loop's future verbs; this plan's out-of-scope list carried forward into the R4 planning seed.

**Commit:** `docs(embervm): R3 closure with gate evidence`

---

## Explicitly out of scope for R3 (recorded, not dropped)

- **Per-session xDS publication:** session traffic stays proxied by the control plane (R2 Option A). Moving it to Envoy-direct requires an answer for per-session token auth that does not put ember back on the hit path (header-matched tokens in route config leak capability into config; ext_authz to ember violates decision 1). Deferred until a high-rate session consumer exists; the endpoint facts and the publisher seam make it additive.
- **A true ember edge Envoy tier and cross-node VM routing:** v1's edge is the existing Envoy Gateway over one serving node (Fork 2). Multi-node serving needs either node-affine routing at the edge or a cluster-scoped ember edge fleet, plus routable VM IPs or node-tier hairpinning; the cluster naming and node-scoped snapshot keys hold the seam. The pending Cilium migration is the likely enabler.
- **eBPF acceleration** (sockmap short-circuit node-Envoy-to-VM, XDP steering, per-VM egress at the tap): the ADR 001 optimization lane, never v1.
- **Load-based autoscaling between min and max instances:** v1 scales on miss (up) and idle (down) only. Concurrency- or latency-driven scaling (Knative KPA-shaped) is the recorded follow-on, and the stats scrape is its future signal source.
- **Envoy ALS (gRPC access-log service) request-level metering:** v1 meters live-seconds plus request-count deltas; ALS is the richer stream when chargeback needs per-request granularity.
- **Node-tier active health checks in Envoy:** the daemon is the health authority in v1; Envoy active checks are the recorded fast-ejection follow-on.
- **Serving egress:** ingress-only NIC in v1; brokered egress (ADR agents/023) for serving guests is a follow-on.
- **Snapshot distribution for serving** (ADR 003 Restore/Export applied to serving snapshots): wake is residency-bound in v1; distribution frees placement later through the held placement seam.
- **Drain by in-flight verification** (`upstream_rq_active` watching) instead of the fixed `drainSeconds` wait.
- **TLS between EG, the node tier, and VMs:** in-cluster plaintext consistent with the current posture; revisit with the mesh migration.
- **Multi-hostname or wildcard routing per workload; weighted / canary routing between instance generations:** one host, one workload, uniform LB in v1.

## Open risks tracked for execution

| Risk | Watch signal | Fallback |
| ---- | ------------ | -------- |
| Pod-to-VM-bridge routing blocked by the CNI (Task 6 assumption) | Task 6 bring-up drill fails to curl the VM from a pod | hostNetwork for the node Envoy DaemonSet (recorded in Task 6); worst case a hostPort |
| go-control-plane / Envoy proto dep tree is heavy in bzlmod | PR-3 CI build time and gazelle churn | The sidecar is a self-contained module; pin a minimal version; the snapshot API means a rewrite swaps cleanly if the library fights Bazel |
| xDS snapshot and reality diverge (stale endpoint serves a dead VM) | Gate 1/7 snapshot diffs; Envoy 503 upstream_reset spikes | Health-authority ejection (Task 7) plus adoption re-derive; the publisher is a pure function of facts, so divergence is a facts bug, greppable in the op-log audit trail |
| Activator becomes a de facto proxy under sustained miss storms | Activator span rate vs Envoy hit rate; wake-rate 429 counts | Per-principal wake limits and park caps are v1; raise `minInstances` for hot workloads; the miss path is capacity-bounded by design (ADR 001) |
| Wake p95 misses 750ms on the og-image snapshot | Task 9/11 timings | Budget honesty first (R2 relight measured 121-132ms, so the envelope is publish+proxy; tune the publisher debounce and sidecar ACK path); pre-warm via `minInstances: 1` as the consumer-level escape |
| Drain window (fixed 5s) too short under slow clients, causing bank-time resets | Gate 4 zero-5xx check under load | Raise `drainSeconds` per workload; the in-flight-verified drain is the recorded follow-on |
| Stats-scrape idle signal misses traffic (scrape gap) and banks a busy workload | Gate 4 drill; `serving_relit` immediately after `serving_banked` patterns in the log | Fail-open rule already forbids banking on stale stats; widen the idle window; ALS is the precise signal later |
| Tap IP churn across relights breaks guests that cache their own address | Consumer soak; guest logs | Contractual: guests must bind 0.0.0.0 and not persist IPs (documented in the CRD serving block); og-image's shim already complies |
| The og-image flip regresses a public surface | Gate 9 soak; public-tier checklist | Values-level rollback to the task-class FaaS path, kept registered and warm throughout R3 |
| Two Envoy tiers double-count or confuse latency attribution in SigNoz | Task 10 dashboard review | Distinct stat prefixes per tier; the gate arithmetic names which histogram is authoritative per gate |
