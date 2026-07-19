# EmberVM R0 Brick Contracts Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` to execute this plan task by task. One comprehensive code review per merged PR, at the end of the PR, per CLAUDE.md. Defer all test execution to CI on the pushed branch.

**Goal:** Implement the remaining "build now (R0)" interface contracts from
[ADR embervm/005](../decisions/embervm/005-embervm-eks-scale-out-metal-pool-bricks.md)
so that brick-tier capacity (multi-daemon Deployments, EmberPool count vector)
becomes a values change on EKS-day instead of a rewrite, and so the resize
tier decided in [ADR embervm/012](../decisions/embervm/012-fleet-colocation-cp-dynamic-sizing.md)
has the budget-agnostic daemon it assumes. Sizing rules and the substrate-lane
taxonomy are recorded in
[ADR embervm/013](../decisions/embervm/013-substrate-lanes-brick-sizing-capacity-tiers.md).

**What is already done or tracked elsewhere (do not duplicate):**

- **Drain protocol (R0 item 3): done.** SIGTERM sets `draining` plus
  `drain_deadline_unix_ms` on the `NodeStatus` stream
  (`noded/cmd/main.go` run(), `WaitForManagedDrain`), the control plane's
  `Embervm.DrainCoordinator` force-banks over existing per-class RPCs,
  bounded by `EMBERVM_NODED_DRAIN_TIMEOUT` (110s default). No dedicated
  Drain RPC is needed; the stream flag is the protocol.
- **Snapshot content-key (R0 item 5): tracked** in the artifact-decoupling
  plan PR-E (`cpu_sku = (vendor, FC template)` stamping, digest-keyed
  rootfs). Today's `SnapshotRef` carries `(Node, Arch, Vendor)`
  (`noded/substrate/substrate.go`), with fail-closed vendor mismatch in
  `noded/fcvm/driver/driver.go`.
- **Per-instance scratch namespacing: tracked** in artifact-decoupling PR-F
  (run-dir/CID/TAP keyed by `instance_id`).

**Landing order and dependencies:**

1. **PR-1 budget-agnostic daemon**: independent; unblocks the ADR 012 resize
   loop and honest brick budgets.
2. **PR-2 dial-home registration + instance-keyed registry**: independent of
   PR-1; supersedes the EndpointSlice discovery bridge ("C4" seam in
   `node_registry.ex`) that artifact-decoupling PR-H expected to replace,
   and is the registry seam ADR 007's cell design keys on.
3. **PR-3 capacity signals surface**: after PR-2 (reads the instance-keyed
   ledger).

**Tech stack:** Go (noded), Elixir/OTP (control plane), additive-only proto
changes, Bazel + BuildBuddy CI, chart bump via
`bazel/tools/git/bump-chart.sh projects/embervm` in every PR whose code must
deploy.

---

## PR-1: Budget-agnostic daemon

**Branch:** `feat/embervm-budget-agnostic-noded`. No dependencies.

noded currently learns its capacity from static config: `MaxLiveVMs`
(`EMBERVM_NODED_MAX_LIVE_VMS`, default 8, `noded/config/config.go:71-75`) and
a hand-set 36Gi chart memory limit. The only cgroup self-inspection is the
advisory `readMemHeadroomMib()` (`noded/server/server.go:2357-2371`), and
`CpuHeadroomMillicores` is hard-coded 0 (`server.go:1514`, the README
roadmap gap). After this PR the daemon reads its ceiling from its own cgroup
at start and on refresh, so static, resize, and brick tiers are deployment
choices against one binary (ADR 005 item 4; the precondition for ADR 012's
resize loop, where the CP moves the cgroup and the daemon must notice).

### Task 1.1: cgroup budget reader

**Files:**

- Create: `projects/embervm/noded/server/budget.go`
- Test: `projects/embervm/noded/server/budget_test.go`
- Modify: `projects/embervm/noded/server/server.go` (fold
  `readMemHeadroomMib` at lines 2357-2379 into the new reader)
- Modify: `projects/embervm/noded/config/config.go` (new
  `EMBERVM_NODED_DAEMON_RESERVE_MIB`, default 512)

**Steps:**

1. Add a `budget` type reading cgroup v2 `memory.max`, `memory.current`,
   `cpu.max`, and `cpu.stat` from `/sys/fs/cgroup`, with an injectable fs
   root for tests (mirror the `parseMemBytes` style at
   `server.go:2373-2379`).
2. Derived values: `MemBudgetMib = memory.max - reserve` (reserve covers the
   daemon's own RSS; unlimited cgroup reports 0 = unknown, never a guess),
   `MemHeadroomMib` as today, `CpuBudgetMillicores` from the `cpu.max`
   quota/period pair (unlimited reports 0), `CpuHeadroomMillicores =
   CpuBudgetMillicores - usage rate` sampled from two `cpu.stat`
   `usage_usec` readings across the refresh interval.
3. Refresh on a ticker (reuse the existing status cadence) so an in-place
   resize of the pod (ADR 012) is observed without restart.
4. `MaxLiveVMs` keeps its runaway-backstop meaning only (comment at
   `config.go:71-75` already says the control plane owns real concurrency);
   no capacity decision may read it after this PR.

**Tests:** `budget-reads-cgroup-v2`, `unlimited-cgroup-reports-zero`,
`resize-observed-on-refresh` (rewrite `memory.max` between refreshes),
`cpu-headroom-from-usage-delta`.

**Commit:** `feat(embervm): noded reads its resource budget from its own cgroup`

### Task 1.2: report budget and CPU headroom on NodeStatus

**Files:**

- Modify: `projects/embervm/proto/embervm/node/v1/node.proto` (additive:
  `NodeStatus.mem_budget_mib` = 26, `NodeStatus.cpu_budget_millicores` = 27;
  field 25 `node_template_hash` is the current tail)
- Modify: `projects/embervm/noded/server/server.go` (populate the two new
  fields plus the real `CpuHeadroomMillicores` at the assembly site,
  lines 1513-1514)
- Modify: `projects/embervm/control/lib/embervm/node_registry.ex`
  (`facts_from_status/3` at lines 502-579 carries the new fields)
- Modify: `projects/embervm/control/lib/embervm/node_capacity.ex` (facts map)
- Test: `projects/embervm/control/test/embervm/node_registry_test.exs`,
  noded server test alongside existing NodeStatus tests

**Steps:**

1. Regenerate proto bindings (Bazel targets; additive-only, no renumbering).
2. Populate the new fields from the Task 1.1 reader; keep headroom fields'
   existing best-effort semantics (0 = unknown).
3. Project both budget fields into the ETS facts so the dispatcher and the
   future resize loop read ceiling and headroom together.
4. Closes the README roadmap line "CPU headroom reporting from cgroups in
   NodeStatus"; update that checkbox in `projects/embervm/README.md`.

**Tests:** `nodestatus-carries-budget-fields`,
`registry-projects-budget-facts`.

**Commit:** `feat(embervm): report cgroup budget and CPU headroom on NodeStatus`

**End of PR-1:** chart bump (`bazel/tools/git/bump-chart.sh
projects/embervm`), push, BuildBuddy CI, one end-of-PR review, rebase-merge,
then verify live: `kubectl get applications -n argocd` synced and a
`GetNodeStatus` probe (or control-plane log line) showing non-zero
`cpu_headroom_millicores` and `mem_budget_mib` on node-4.

---

## PR-2: Dial-home registration and (node, pod-UID) registry keying

**Branch:** `feat/embervm-dial-home-registry`. Independent of PR-1;
coordinate with artifact-decoupling PR-H (this PR provides the discovery
replacement PR-H's header comment defers to; PR-H then only changes who
stamps the pod).

Today the control plane discovers daemons by polling EndpointSlices every
30s (`Embervm.K8s.list_noded_endpoints/3`, `k8s.ex:241-316`; reconcile loop
`node_registry.ex:965-1005`, explicitly an interim "C4" bridge) and keys the
registry by Kubernetes node name (`node_registry.ex:477-492`). ADR 005
requires the inversion: bricks dial home, the CP never lists-and-watches
daemon pods, and the registry key is `(node, pod-UID)` so two daemons can
coexist on one node, which ADR 012's surge rolls (maxSurge 1 /
maxUnavailable 0, instance-partitioned state) already assume.

**Mechanism choice (simplest that satisfies the contract):** registration is
a daemon-initiated HTTP POST to the control plane's existing API surface,
advertising `{node, pod_uid, address, grpc_port}`; the CP then dials the
advertised address for `WatchNode` and the RPC channels exactly as today.
This satisfies "daemon-initiated registration, no pod list/watch" without
adding an Elixir gRPC *server* dependency; the status stream already
rebuilds the fail-closed ledger on reconnect, so no information moves to the
registration call itself. If a later cell design wants status flowing over a
daemon-initiated stream, that lands behind this same seam.

### Task 2.1: noded registers itself

**Files:**

- Modify: `projects/embervm/noded/config/config.go` (new
  `EMBERVM_NODED_CONTROL_PLANE_URL`, `EMBERVM_NODED_POD_UID`;
  register interval knob with default 30s)
- Create: `projects/embervm/noded/server/register.go` (+ test)
- Modify: `projects/embervm/noded/cmd/main.go` (start the register loop
  after the gRPC surface is up; stop it on drain so a draining instance
  stops re-advertising)
- Modify: `projects/embervm/chart/templates/noded-deployment.yaml`
  (Downward API `metadata.uid` env, control-plane URL from values)
- Modify: `projects/embervm/chart/values.yaml`

**Steps:**

1. POST `{node, pod_uid, address: podIP:grpcPort, boot_id}` to
   `/v1/nodes/register` on start and on a jittered interval; treat failures
   as retryable and log at most once per state change (registration is
   advertisement, not liveness; liveness stays on the WatchNode stream).
2. Bearer auth reusing the existing noded/control auth material
   (`noded/cmd/auth.go` pattern).
3. Stop re-registering once draining; the CP ages the instance out.

**Tests:** `register-posts-identity`, `register-stops-on-drain`,
`register-retries-without-crash`.

**Commit:** `feat(embervm): noded dial-home registration to the control plane`

### Task 2.2: control plane accepts registration; retire EndpointSlice discovery

**Files:**

- Modify: `projects/embervm/control/lib/embervm/router.ex` (new
  `/v1/nodes/register` route, auth'd)
- Modify: `projects/embervm/control/lib/embervm/node_registry.ex` (replace
  `@discover_interval_ms` poll + `reconcile_discovered/1` at lines 93-96 and
  965-1005 with registration-driven add/re-point/expire; keep the
  streamer/monitor machinery at 845-896 unchanged)
- Modify: `projects/embervm/control/lib/embervm/application.ex`
  (`node_discovery_opts/0` at 375-445: drop `discover_fun` wiring)
- Delete: `Embervm.K8s.list_noded_endpoints/3` (`k8s.ex:241-316`) and its
  EndpointSlice RBAC once nothing references it
- Test: `projects/embervm/control/test/embervm/node_registry_test.exs`,
  `router_test.exs`

**Steps:**

1. Registration upserts `{node, pod_uid} -> address`; a changed address for
   a known instance re-points its streamer (same behavior the reconcile loop
   has today at 965-1005, now event-driven instead of polled).
2. Instances expire after N missed registration intervals AND a dead
   WatchNode stream (both signals, so a CP-side network blip alone never
   drops a healthy node; fail-closed capacity semantics at 444-580 are
   unchanged).
3. Remove the EndpointSlice RBAC verbs from the chart in the same commit
   (RBAC hygiene per CLAUDE.md).
4. Static `configured_nodes` config path stays for tests and the minimum
   self-contained example.

**Tests:** `register-upserts-and-dials`, `re-register-repoints-address`,
`expiry-requires-both-signals-dead`, `no-endpointslice-calls-remain`.

**Commit:** `feat(embervm): registration-driven node registry, retire EndpointSlice discovery`

### Task 2.3: key the registry and ledger by (node, pod_uid)

**Files:**

- Modify: `projects/embervm/control/lib/embervm/node_registry.ex` (ETS put
  keying at 477-492)
- Modify: `projects/embervm/control/lib/embervm/node_capacity.ex` (key shape
  at 44-55; accessors grow an instances-of-node view)
- Modify: `projects/embervm/control/lib/embervm/dispatcher.ex` (placement
  reads per-instance rows; prefer the newest registered healthy instance per
  node, so a surging replacement wins while the draining one empties)
- Modify: `projects/embervm/control/lib/embervm/drain_coordinator.ex`
  (drain per instance, not per node)
- Modify: `projects/embervm/proto/embervm/node/v1/node.proto` (additive
  `NodeStatus.pod_uid` = 28) and the noded assembly site
- Test: registry, capacity, dispatcher, drain coordinator test files

**Steps:**

1. ETS key becomes `{node_id, pod_uid}`; `facts_from_status/3` carries
   `pod_uid`; vendor/template facts stay per node (hardware facts are
   node-scoped even when keyed per instance).
2. Dispatcher placement: per-instance headroom is the placement input (the
   ADR 013 rule: refill and placement select an instance with contiguous
   headroom, never an aggregate).
3. Two instances on one node must be simultaneously representable
   (surge-roll invariant from ADR 012); add a test that a draining old
   instance and a fresh instance coexist, with placement going only to the
   fresh one.
4. Session/serving/stateful projections that reference node identity keep
   node scope (snapshots and volumes are node resources, not pod resources);
   only liveness/capacity/dispatch move to instance scope.

**Tests:** `two-instances-one-node-coexist`,
`placement-prefers-newest-healthy`, `drain-scopes-to-instance`,
`node-scoped-facts-survive-instance-turnover`.

**Commit:** `feat(embervm): registry and capacity ledger keyed by (node, pod uid)`

**End of PR-2:** chart bump, push, CI, one end-of-PR review, rebase-merge.
Live verification is a rollout restart of noded on one node: the CP log
shows register, dial, adopt for the new pod-UID and drain for the old one,
with no EndpointSlice access in between, and primed pools re-adopt without a
cold restart.

---

## PR-3: Look-ahead signals and the capacity knob surface

**Branch:** `feat/embervm-capacity-signals`. After PR-2.

ADR 005 R0 item 6: expose the signals the EmberPool controller (EKS-day) and
the ADR 012 resize loop will consume, so the control loop is wiring work
later, not archaeology. Nothing in this PR makes scaling decisions.

### Task 3.1: /v1/capacity read surface

**Files:**

- Create: `projects/embervm/control/lib/embervm/capacity_report.ex` (+ test)
- Modify: `projects/embervm/control/lib/embervm/router.ex` (auth'd GET
  `/v1/capacity`)
- Modify: `projects/embervm/docs/api.yaml`

**Steps:**

1. Assemble from existing state, no new bookkeeping: per-instance budget and
   headroom (PR-1/PR-2 facts), per-workload primed occupancy and
   `free_primed_slots` (already in `WorkloadCapacity` facts), queue depth
   per class from the dispatcher, sum of per-workload concurrency floors
   from the `Workload` CRD catalog, and committed-future-load as the cron
   trigger firings within a configurable horizon
   (`Embervm.Cron` / `trigger/cron.ex` already know next-fire times).
2. Shape the JSON as the demand tiers of ADR 013 section 7: `floors`
   (arithmetic), `committed` (exact), `observed` (reactive), so the future
   controller consumes tiers, not raw counters.

**Tests:** `capacity-report-shape`, `floors-sum-from-catalog`,
`committed-load-from-cron-horizon`.

**Commit:** `feat(embervm): add /v1/capacity look-ahead signals surface`

### Task 3.2: metrics and the desired-capacity knob stub

**Files:**

- Modify: `projects/embervm/control/lib/embervm/capacity_report.ex` (emit
  the same tiers as OTel gauges on the existing telemetry path)
- Modify: `projects/embervm/control/config/runtime.exs` (a
  `desired_capacity` config seam: today a static map read at boot, the
  slot the EmberPool controller or resize loop later writes; unset means
  "no opinion", and nothing acts on it yet)
- Modify: `projects/embervm/README.md` (roadmap: mark the R0 contracts
  landed, pointing at ADR 005/013 and this plan)

**Steps:**

1. Gauges per `(class, workload)` for floors/committed/observed and per
   instance for budget/headroom, so SigNoz can graph demand vs capacity
   before any controller exists.
2. The knob is deliberately inert: a typed config value plus a log line at
   boot. Acting on it is the EKS-day EmberPool controller or the ADR 012
   resize loop, out of scope here.

**Tests:** `gauges-emitted-per-tier`, `desired-capacity-parses-and-noops`.

**Commit:** `feat(embervm): capacity gauges and inert desired-capacity knob`

**End of PR-3:** chart bump, push, CI, one end-of-PR review, rebase-merge.
Live verification: `curl` the auth'd `/v1/capacity` route via the private
gateway and confirm SigNoz shows the new gauges.

---

## Execution notes

- Conventional Commits enforced by hook; no local test loop, CI on push via
  BuildBuddy; read failures with `mcp__buildbuddy__get_invocation` +
  `get_log` before hypothesizing.
- Proto changes are additive-only (never renumber; `node_template_hash` = 25
  is the current `NodeStatus` tail, so new fields start at 26).
- Every PR that must deploy carries its chart bump
  (`bazel/tools/git/bump-chart.sh projects/embervm`) in the same PR.
- Coordinate PR-2 with artifact-decoupling PR-H; if PR-H lands first, its
  CP-managed Deployments still speak the registration flow, and this plan's
  Task 2.2 deletion list shrinks to whatever bridge code remains.
- Landing order: PR-1 and PR-2 may run in parallel worktrees; PR-3 rebases
  on PR-2's merge.
