# EmberVM R4 (Stateful) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or superpowers:executing-plans in a separate session) to implement this plan task-by-task. This document is the committed spec for rung R4 of [ADR embervm/001](../decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md), riding the R2 bank/relight machinery and the R3 xDS tier, and bounded by [ADR embervm/003](../decisions/embervm/003-control-plane-managed-snapshot-distribution.md) (volumes are node-anchored in v1; distribution frees nothing here yet). Every task is a specification with acceptance criteria; no implementation lives here.

**Goal:** Ship EmberVM R4: scale-to-zero singleton datastores. A stateful workload is one long-lived microVM owning one writable volume, reachable over an opaque L4 TCP port, that banks to disk when idle and wakes on the next inbound connection. The rung's headline property is the ADR 001 state split made real: **data on the volume, warmth in the snapshot**. The volume owns the data (real durable storage on node NVMe); the memory snapshot only pre-pays cache warmth, and resume requires an exact (memory snapshot, volume generation) match, else the warmth is discarded and the workload cold-boots from the volume: slower, never incorrect. The named first consumer is **`scratch-postgres`**: a scale-to-zero Postgres that the monolith's agent toolchain (agent sessions, recipes, demos) connects to on demand, costing one banked snapshot and one volume file while idle instead of a running database.

**Architecture:** The node contract gains volume facts and stateful verbs: noded owns a per-workload raw sparse volume file plus an on-disk generation ledger, attaches it as a writable block device to exactly one VM at a time (attach bumps the generation; a second concurrent attach is refused), and probes health by TCP connect instead of HTTP GET. The control plane gains a `StatefulStore` (instances and volumes projected from new op-log records), extends the `EndpointPublisher` with L4 listeners, and grows a TCP activator: connection-open on an empty cluster is the wake signal. The data plane extends the R3 node Envoy with dynamic LDS: one TCP-proxy listener per stateful workload (cluster `state|<workload>`), fed by the same xDS sidecar, exposed cluster-internally through a values-declared TCP port range on the existing serving Service. The guest owns its filesystem end to end: guest-init mkfs+mounts the volume on first use, and the host never mounts or reads it (the ADR 001 isolation row: data never leaves the owning workload's boundary).

**Tech Stack:** Elixir/OTP control plane and Go noded (existing), `go-control-plane` sidecar gaining LDS (SotW ADS, existing), Envoy TCP proxy filter on the node tier, raw file-backed Firecracker block devices, gRPC stateful verbs on `embervm.node.v1`, SQLite op-log projections (ADR 002 discipline), Workload CRD `class: stateful` + `spec.stateful` block, apko Postgres guest image, Helm + ArgoCD.

---

## Standing decisions (settled, do not relitigate during execution)

1. **Data on the volume, warmth in the snapshot; the pairing is enforced at the daemon.** A banked stateful bundle records the volume generation it was paused with. `StartStateful(relight)` succeeds only when the bundle's recorded generation equals the volume's current generation; any mismatch (or an unreadable ledger) discards the bundle and cold-boots from the volume. Warmth fails open (cold boot), data fails closed (a missing volume file is `FAILED_PRECONDITION`, never silently recreated). This is the ADR 001 v1 invariant, now load-bearing.
2. **Every writable attach bumps the generation, and one banked bundle exists per workload.** Fresh boot, relight, and cold boot all increment the on-disk generation before the VM starts; bank stamps the then-current generation into the bundle and evicts any prior bundle for the workload. Together these make generation equality a complete staleness test: any attach the snapshot did not witness breaks the pair. The generation is metadata only; nothing ever rewrites volume bytes outside the guest.
3. **Singleton by construction, enforced where the data is.** Exactly one live VM per stateful workload. noded refuses a second writable attach of the same volume (`FAILED_PRECONDITION`), the control plane single-flights wakes per workload, and there is no `maxInstances` knob on the class. Replication is the application's job in a later rung (ADR 001: replicate at one layer only); this class is explicitly the non-replicated scratch/preview tier.
4. **Opaque L4 in v1: connection-open is the whole protocol.** The node Envoy gets one TCP-proxy listener per stateful workload via dynamic LDS from the existing sidecar. No protocol-aware filters, no per-query visibility, no request-level fairness: routing, wake-on-connect, and connection counting are the entire L4 feature set (the ADR 001 visibility spectrum, bottom tier). The Postgres wire-protocol filter is the recorded protocol-aware follow-on.
5. **The listener port is the workload identity.** TCP has no header to inject, so the miss path resolves the workload from the listener the connection arrived on: cluster `state|<workload>` with zero endpoints publishes the control-plane TCP activator as its sole endpoint (the R3 activator-as-fallback pattern at L4), and the activator learns the workload from a per-listener activator port mapping carried in the same snapshot. A connection reaching the activator IS the miss signal by construction.
6. **The activator holds the socket, wakes, then splices bytes at lifecycle rate.** On a miss: park the accepted TCP connection (a BEAM process per parked connection, capped per workload), single-flight the wake (relight if the pair matches, else cold boot), then proxy bytes bidirectionally to the VM for the life of that connection. Parked-connection caps and per-principal wake-rate limits guard the asymmetric-cost surface exactly as R3's HTTP activator does. Subsequent connections arrive via Envoy directly.
7. **Bank only when idle at the connection level; never sever a live connection.** A TCP stream cannot be drained like an HTTP request. The idle signal is the R3 stats scrape extended to TCP: `downstream_cx_active == 0` and a zero `downstream_cx_total` delta across `idleBankSeconds`. The bank sequence unpublishes the endpoint (installing the activator in the same LDS/EDS update, so a connection racing the bank parks instead of failing), confirms active-connection count is still zero, then `StopStateful(BANK)`. Any nonzero recheck aborts the bank.
8. **Bank is pause, not shutdown; destroy is kill, and the database's own crash recovery covers it.** Warm banking pauses VCPUs (crash-consistent volume at the pause point, resumed exactly). Destroy paths (TTL expiry, forced roll, eviction) do not attempt a guest-cooperative shutdown in v1: the next cold boot runs the DB's WAL recovery, which is precisely what the volume-owns-durability contract buys. A guest-ACPI clean-stop verb is a recorded follow-on, not v1.
9. **The host never mounts the volume.** noded creates, sizes, attaches, and deletes the raw file; guest-init detects a blank device, runs mkfs, and mounts it at a declared path. No host-side mkfs, no host-side fsck, no host-side reads of guest data. A compromised node daemon can destroy the bytes but gains no parsing surface over them, and the ADR 001 stateful isolation row ("volume owned by one workload") stays literally true.
10. **Cluster-internal exposure only, DB-native auth.** The stateful port range is reachable only inside the cluster (the existing serving Service, ClusterIP, new TCP ports from a values-declared range). Who may connect is the database's own auth (password provisioned via the MMDS env seam at first boot); no ember auth exists on the connection path (the R3 decision 1 discipline at L4), and nothing stateful is publicly exposed in v1.
11. **Volumes are node-anchored and this class trades away free placement, stated plainly.** The volume file lives on one node's NVMe; wake happens on that node or not at all (`FAILED_PRECONDITION` when the node is gone, surfaced in workload conditions). No volume migration, no cross-node scheduling, no storage replication in v1: single-copy scratch tier by contract, with off-node backup riding the ADR 003 distribution arc later.
12. **The node reports, the control plane adopts.** `stateful_vms`, `stateful_bundles`, and `volumes` (with generations) join `NodeStatus`; boot/sweep reconciliation rebinds live instances, heals limbo states, evicts pair-broken bundles, and re-derives the full xDS snapshot (now including listeners) from adopted facts. A control-plane restart must converge without touching any VM (the #3517 lesson, fourth application).
13. **Additive proto only; task, session, and serving contracts stay frozen.**

### Fork 1: volume backing (settled choice)

- **Option A (chosen): raw sparse file per workload on node NVMe, managed by noded.** `volumes/<workload>/vol.img` beside the snapshot dirs, created sparse at a CRD-declared size cap, attached as a writable Firecracker block device. Zero new infrastructure, same disk noded already owns, generation ledger is a sidecar file, and Firecracker consumes raw files natively. The known cost: no thin-provisioning guarantees beyond sparseness and no snapshots of the volume itself, both acceptable for the scratch tier.
- **Option B: LVM thin volumes.** Real snapshots and space reclaim, but a host LVM dependency, root-privileged device management in noded, and node-image coupling. Rejected for v1 on operational surface; re-rank when volume snapshots are actually needed.
- **Option C: Longhorn/PVC-backed volumes.** Kubernetes-native replication and off-node durability, but puts a distributed storage system under microVM block devices, couples wake latency to Longhorn attach latency, and contradicts the node-anchored contract v1 states. Rejected; ADR 003's distribution loop is the recorded off-node story.

### Fork 2: how L4 listeners reach Envoy (settled choice)

- **Option A (chosen): dynamic LDS from the existing sidecar.** The snapshot API grows a `listeners` array; the sidecar serves LDS alongside CDS/RDS/EDS. One TCP-proxy listener per stateful workload, port from the CRD, added and removed as facts change. Uniform with everything R3 built (one publisher, one snapshot, level-triggered), and the sidecar stays logic-free.
- **Option B: static listener port range in the bootstrap.** Avoids LDS but caps workloads at N pre-declared ports, makes port-to-workload mapping bootstrap config instead of published fact, and still needs per-port cluster indirection. Rejected: config posing as fact.
- **Option C: skip Envoy, DNAT straight to the VM plus a control-plane activator.** Least machinery (the D-R3.11.4 lane), but no unpublish/eject seam for the bank ordering in decision 7, no connection stats for the idle signal, and it forks the data plane the ADR standardizes on. Rejected.

### Fork 3: first consumer (settled choice)

- **Chosen: `scratch-postgres` for the agent platform.** A single scale-to-zero Postgres the monolith agent toolchain connects to on demand (agent sessions, recipes, demos writing scratch relational state). Real recurring but bursty traffic, low-stakes data (scratch tier by name), and it exercises wake-on-connect, generation pairing, and idle-bank in their natural shapes.
- Preview-per-branch databases were deferred (no preview-environment workflow exists to consume them yet); a public demo datastore was deferred on public-tier scope. Both remain natural second consumers.

## Cross-cutting constraints

- **No local test loop.** Implement, commit, push, watch BuildBuddy CI (`gh pr checks <n> --watch`). ExUnit, Go, and pytest targets run under `bazel test //...` in CI only.
- **Conventional Commits; no em-dashes anywhere.**
- **Charts bump via `bazel/tools/git/bump-chart.sh`** in the same PR as the code they deploy. Docs-manifest regeneration (this plan, ADR touches) forces monolith AND monolith-public chart bumps.
- **RBAC verbs verified per task** for every new K8s API call before merge (expected new runtime RBAC: zero; the Service port range and CRD changes are chart-managed).
- **Op-log schema changes use the guarded ALTER-after-DDL migration pattern.**
- **Additive proto only:** new RPCs, messages, and `NodeStatus` fields; existing verbs untouched.
- **Repository layout:** control plane under `projects/embervm/control/`, xDS sidecar under `projects/embervm/xds/`, noded under `projects/embervm/noded/`, guest images under `projects/embervm/runtimes/`, chart pieces under `projects/embervm/chart/`.
- **One comprehensive code review per merged PR.**

## Suggested PR partitioning

| PR | Tasks | Deploys |
| -- | ----- | ------- |
| PR-0 docs (this branch) | this plan | manifests + monolith/public chart bumps |
| PR-1 contract | 1, 2, 3 | additive proto + op-log schema + CRD; no behavior change |
| PR-2 noded stateful | 4 | volume manager, stateful verbs, TCP probe live on noded, unused |
| PR-3 L4 tier | 5, 6 | LDS in the sidecar + TCP listeners on the node Envoy, drill cluster |
| PR-4 stateful core | 7, 8 | store + publisher listeners + TCP activator; wake-on-connect live |
| PR-5 lifecycle economics | 9 | idle-to-bank, TTL, forced roll, pair-mismatch cold boot |
| PR-6 operability | 10 | stateful spans, volume/pairing metrics, alerts |
| PR-7 consumer | 11 | scratch-postgres live and wired to the agent platform |
| PR-8 closure | 12 | R4 marked shipped in ADR 001 |

---

## Phase 0: Contract and schema foundations

### Task 1: Node contract volume facts and stateful verbs

**Why:** The rung's primitive is a VM whose writable block device carries the data and whose lifecycle is generation-checked at the daemon, where the volume physically lives.

**Deliverables:**
- Additive RPCs on `embervm.node.v1.NodeService`, proto comments as the spec:
  - `rpc StartStateful(StartStatefulRequest) returns (StartStatefulResponse)`: bring up the stateful VM with the workload's volume attached writable. `mode` oneOf: `FRESH` (cold boot the image rootfs; create the volume sparse at `volume_size_bytes` if absent, `FAILED_PRECONDITION` if absent and `create_if_missing` is false), `RELIGHT` (resume the banked bundle iff its recorded generation equals the volume's current generation; on mismatch or unreadable ledger, evict the bundle and fall back to a cold boot, reporting `cold_boot_reason`), `COLD` (explicit cold boot from the existing volume). Every mode bumps the generation before boot. Attaches the tap NIC exactly as `StartServing` does; health-gates by TCP connect to `{ip, port}`. Returns `{vm_id, ip, port, generation, was_relight, cold_boot_reason}`.
  - `rpc StopStateful(StopStatefulRequest) returns (StopStatefulResponse)`: `mode = BANK` (pause, snapshot to `stateful/<ref>/` bundle stamped with the current generation, destroy, evict any prior bundle for the workload, return `{snapshot_ref, generation, size_bytes}`) or `mode = DESTROY` (kill, no snapshot). Refuses concurrent stops per vm_id.
  - `rpc DeleteVolume(DeleteVolumeRequest) returns (DeleteVolumeResponse)`: removes the volume file and ledger; `FAILED_PRECONDITION` while attached. The only destructive data verb, and it exists so deletion is an explicit, audited act.
  - Stateful bundles reuse `EvictSnapshot` unchanged (bundle refs are opaque).
- `NodeStatus` additions (wire-compatible): `stateful_vms: [{vm_id, workload, ip, port, healthy, generation, last_probe_unix_ms}]`, `stateful_bundles: [{snapshot_ref, workload, generation, size_bytes, created_at_unix_ms}]`, `volumes: [{workload, generation, size_bytes, allocated_bytes, attached}]`.
- TCP health-probe contract in proto comments: the daemon probes TCP connect to each live stateful VM's `{ip, port}` on the existing probe cadence and thresholds; the control plane consumes the fact, the daemon never acts on it.
- fakenode serves the new verbs and status fields, including scripted generation ledgers for pairing tests.

**Specification:**
- Refs and generations are opaque facts; no paths cross the seam. The generation is a uint64 the daemon owns; the control plane only ever compares and records it.
- Stateful VMs count against `max_live_vms` and are excluded from `primed_vm_ids`.
- `StartStateful` carries `workload`, `Trace`, resource shape, and the MMDS env map (the secrets seam for first-boot DB credentials), exactly as existing verbs do.
- Go+Elixir stubs regenerate through the existing pure-genrule codegen; fake-server round-trip tests per verb and per pairing outcome (match, mismatch, missing ledger, missing volume) in CI.

**Acceptance:** CI green; reviewer confirms additivity, that task/session/serving verbs are untouched, and that nothing Envoy-specific leaks into the node contract.

**Commit:** `feat(embervm): stateful verbs, volume facts, and generation pairing on the node contract`

### Task 2: Op-log stateful records and projections

**Why:** Volume lifecycle and the pairing decisions are enforcement-adjacent facts: every attach, bank, pair check, and deletion must be an ordered, durable, auditable append.

**Deliverables:**
- Additive op kinds: `volume_created, volume_deleted, stateful_started, stateful_published, stateful_unpublished, stateful_banked, stateful_relit, stateful_cold_booted, stateful_evicted, stateful_destroyed, stateful_failed, stateful_stats`.
- New projection tables: `stateful_instances(instance_id TEXT PRIMARY KEY, tenant, principal, workload, state, node_id, vm_id, ip, port, generation INTEGER, snapshot_ref, snapshot_generation INTEGER, snapshot_size_bytes, created_at, last_active_at, updated_at, terminal_reason)` and `volumes(workload TEXT PRIMARY KEY, node_id, generation INTEGER, size_bytes, allocated_bytes, created_at, updated_at)`, plus a nullable `stateful_instance_id` column on `ops` (guarded ALTER-after-DDL).
- `stateful_cold_booted` carries `{reason: generation_mismatch|no_bundle|ledger_unreadable|explicit}` so every discarded-warmth event is reconstructable from the log alone; `stateful_relit` carries the matched generation.
- `stateful_stats` ops carry `{workload, cx_delta, window_ms}` from the Task 9 scrape; usage accrues live-seconds from lifecycle timestamps and connection counts from stats deltas, upserted in the same transaction (the D12.1 pattern).
- Retention per ADR 002: terminal instances prune past retention; the `volumes` row lives until `volume_deleted` (data outlives every instance by design).

**Specification:**
- State transitions are write-through appends before ETS visibility, the standing discipline. The xDS snapshot is never derived from the durable store on the publish path.
- ExUnit: projection rebuild equivalence from scripted op sequences including a full pair-mismatch cycle; retention never prunes a non-terminal instance or a live volume row.

**Acceptance:** CI green; kill-and-restart test extended to rebuild stateful and volume state exactly.

**Commit:** `feat(embervm): stateful instance and volume records with pairing audit in the op-log`

### Task 3: Workload CRD `class: stateful` and the stateful spec block

**Why:** The definition surface. The class enum already reserves `stateful` (rejected by the watcher today); this task makes it real with the volume and L4 contract declared, not inferred.

**Deliverables:**
- CRD: `class: stateful` accepted by the watcher, requiring `spec.stateful`: `{port (required; the guest listen port, e.g. 5432), listenPort (required; the node Envoy TCP listener port, validated inside the chart's values-declared stateful range and unique across stateful workloads), volumeSizeGiB (required; sparse cap), volumeMountPath (default "/data"), idleBankSeconds (default 300, min 30), maxLifetimeSeconds (default 86400), bankedTtlSeconds (default 604800), wakeTimeoutSeconds (default 60; cold boots include WAL recovery and mkfs on first boot, so this is generous by design)}`. `spec.serving` and `spec.session` are condition-rejected on this class; `spec.concurrency` is condition-rejected (singleton, decision 3).
- `status.stateful: {state, generation, bundleGeneration, volumeBytes}` + a `STATEFUL` printer column.
- Validation posture from R0/R3: duplicate `listenPort` across stateful workloads condition-rejected; out-of-range `listenPort` condition-rejected with the configured range named in the condition.
- Sample stateful CR under `projects/embervm/crd/samples/`.

**Specification:**
- Stateful instances ride their birth image like every class; convergence after a deploy is destroy-and-cold-boot (the data is on the volume, so a roll costs a WAL recovery, not data). `maxLifetimeSeconds` bounds it and the Task 9 forced roll is the operator lever.
- `volumeSizeGiB` is immutable in v1 (resize is a recorded follow-on); the watcher condition-rejects edits to it.
- ExUnit: watcher parse/validation tests including port-range, duplicate-port, and concurrency-rejection paths.

**Acceptance:** CI green; sample CR round-trips; `kubectl get workloads` shows the stateful column.

**Commit:** `feat(embervm): stateful workload class and volume spec block`

---

## Phase 1: The data plane (volumes and the L4 tier)

### Task 4: noded volume manager and stateful lifecycle

**Why:** The rung's physical substrate: a raw file that is created once, attached to exactly one VM at a time, generation-tracked on disk, and never mounted by the host.

**Deliverables:**
- Volume manager: `volumes/<workload>/vol.img` (raw, sparse, created at the declared cap) + `volumes/<workload>/gen` ledger + an in-process attach lock. Attach bumps the ledger atomically (write-temp-rename) before the VM boots; a second attach while held is `FAILED_PRECONDITION`. `allocated_bytes` reported from the file's actual block usage.
- `StartStateful` all three modes: FRESH (cold-boot the image rootfs with tap NIC per D-R3.4.2 mechanics, attach the volume as an additional writable drive, TCP health-gate), RELIGHT (pair check against the bundle's stamped generation, resume with the volume re-attached and the pinned IP per D-R3.4.1, fall back to cold boot on mismatch with `cold_boot_reason`), COLD (explicit).
- Guest-side volume contract delivered via boot-args (`ember.volume_dev=`, `ember.volume_mount=` mirroring `ember.serving_port=`): guest-init detects a blank device (no filesystem signature), runs mkfs.ext4, mounts at the declared path before handing off to the image init. The host never mounts (standing decision 9).
- `StopStateful` BANK (pause, snapshot bundle under `stateful/<ref>/` stamped with the generation, evict the workload's prior bundle) and DESTROY; `DeleteVolume` with attached-refusal.
- TCP-connect health probe loop per live stateful VM; results in `NodeStatus`.
- DNAT exposure of stateful VM ports through the D-R3.11.4 lane unchanged (the node Envoy reaches `nodedPodIP:port`).
- Inventory rescan on daemon start (volumes dir, stateful bundle dir, ledgers): the adoption source of truth.

**Specification:**
- Bank pauses VCPUs then snapshots; the volume file is not copied, hashed, or touched at bank time (the generation ledger is the entire pairing mechanism). A relight resumes against the identical file.
- A daemon restart kills live stateful VMs; the control plane resolves each to `banked` (bundle exists and pair still matches) or cold-bootable, and the next wake recovers. Same v1 availability posture as serving.
- Go tests: fake-driver coverage for attach-lock refusal, generation bump ordering (ledger bumped before boot, boot failure does not un-bump), pair match/mismatch/missing-ledger relight outcomes, blank-device detection contract (table-driven), bundle-evict-on-bank, DeleteVolume refusal while attached, inventory rescan.

**Acceptance:** CI green; on deployed noded, a grpcurl `StartStateful(FRESH)` of the Task 11 postgres guest yields an `{ip, port}` that accepts a TCP connection from a debug pod (documented in the PR); `StopStateful(BANK)` then `StartStateful(RELIGHT)` round-trips with data written before the bank readable after it; a `StartStateful(COLD)` after a bank makes the following `RELIGHT` fall back with `cold_boot_reason: generation_mismatch`.

**Commit:** `feat(embervm): volume manager, generation ledger, and stateful lifecycle in noded`

### Task 5: Dynamic LDS in the xDS sidecar

**Why:** Fork 2's chosen mechanism: TCP listeners become published facts served by the same logic-free sidecar.

**Deliverables:**
- Snapshot API extension: the desired-state JSON gains `listeners: [{name, port, cluster}]`, each translated to an Envoy TCP-proxy listener (stat prefix per listener) bound on the pod port; LDS joins CDS/RDS/EDS in the served snapshot (SotW ADS, same cache, same version discipline).
- Backward compatibility: a snapshot with no `listeners` key serves exactly what R3 serves today (byte-identical resources), proven by a regression test.
- The node Envoy bootstrap ConfigMap adds `lds_config` pointing at ADS (the HTTP listener moves into LDS as a static-equivalent resource pushed by the sidecar, OR stays static in the bootstrap with only new TCP listeners dynamic; choose during execution whichever keeps the R3 HTTP path provably untouched, and record the choice in DECISIONS.md).
- Go tests: listener translation (JSON in, expected resources out, table-driven), empty-listeners regression, malformed-port 400s.

**Specification:**
- The sidecar stays logic-free: no port validation beyond well-formedness (the CRD watcher owns range policy), no defaulting, no state.
- Envoy TCP-proxy config per listener is minimal: cluster, idle timeout off (long-lived DB connections are legitimate), connection logging to stdout.

**Acceptance:** CI green; deployed sidecar serves a hand-pushed snapshot containing one TCP listener and `GET /snapshot/...` echoes it.

**Commit:** `feat(embervm): dynamic lds and tcp-proxy listeners in the xds sidecar`

### Task 6: Node Envoy TCP exposure and the drill

**Why:** The wiring that makes a stateful port reachable from any cluster pod, proven end to end before the control plane's stateful core exists.

**Deliverables:**
- Chart: the values-declared stateful TCP port range (default `5400-5409`) exposed as container ports on the node Envoy DaemonSet and as TCP ports on the `embervm-serving` Service (names `state-<port>`); values documentation naming the range as the `listenPort` policy source.
- Bring-up proof without the stateful core: a hand-pushed snapshot (curl the sidecar's PUT) containing one TCP listener on a range port and a cluster pointing at a Task 4 drill VM (the postgres guest), then `psql` from a debug pod through the Service connects and queries.
- Rendered `helm template` verified in the PR.

**Specification:**
- No public exposure: no HTTPRoute, no gateway change, ClusterIP only (standing decision 10). The reviewer confirms nothing in this PR touches an edge resource.
- Service port list is static GitOps config by design (the R3 decision 8 discipline): the range is capacity, the CRD assigns within it, and endpoint churn stays xDS.

**Acceptance:** CI green; the drill psql session is documented in the PR description with output.

**Commit:** `feat(embervm): stateful tcp port range through the node envoy and serving service`

---

## Phase 2: Stateful core (publication and wake-on-connect)

### Task 7: StatefulStore, lifecycle FSM, and publisher listeners

**Why:** The control-plane brain: which instance exists, what generation the volume is at, whether the banked warmth is still valid, and the single projection from facts to L4 config.

**Deliverables:**
- `Embervm.StatefulStore`: ETS hot set (instance by workload, volume facts, pair validity) rebuilt from projections on boot; write-through transitions. FSM states: `starting -> serving -> banking -> banked -> relighting -> serving ...` plus `cold_booting`; terminal `expired, evicted, destroyed, failed`; illegal transitions raise.
- `EndpointPublisher` extension: stateful workloads project one listener `{name: "state|<workload>", port: listenPort, cluster: "state|<workload>"}` and one cluster whose endpoint is the live VM or the activator's TCP port when none (the empty-cluster fallback at L4). The publisher stays the only sidecar writer and the projection stays a pure function of ETS facts.
- Pair validity is a derived fact: `bundle_generation == volume_generation`, recomputed on every NodeStatus sweep; an invalidated bundle is evicted eagerly (op `stateful_evicted` reason `pair_broken`) rather than discovered at wake time.
- `GET /v1/stateful/{workload}` management API: instance state, generation, bundle generation, pair validity, volume bytes, published endpoint.

**Specification:**
- Health ejection: a TCP-probe-unhealthy instance unpublishes (activator installed in the same update) and the sweep decides bank-versus-destroy from node facts.
- ExUnit: FSM exhaustive transitions, publisher pure-function tests including the listener + activator swap in both directions and the no-stateful-workloads regression (snapshot unchanged from R3 shape), pair-validity derivation, eager eviction.

**Acceptance:** CI green.

**Commit:** `feat(embervm): stateful store, pairing facts, and l4 listener publication`

### Task 8: The TCP activator (wake-on-connect) and restart adoption

**Why:** The rung's headline verb: an inbound connection to a sleeping database becomes one wake, one pause, and a working session, with the caller none the wiser.

**Deliverables:**
- `Embervm.TcpActivator`: a `:gen_tcp`/`ThousandIsland` listener on a dedicated control-plane port (exposed on the pod, one port for all stateful workloads). Per accepted connection: resolve the workload from the destination listener mapping carried in the published snapshot (decision 5), park (process per connection, per-workload cap default 16), single-flight the wake (`StartStateful(RELIGHT)`, daemon-side fallback to cold boot per Task 1), publish the real endpoint (activator leaves the cluster in the same update), then splice bytes bidirectionally for the life of the connection.
- Wake-rate limits per principal (values-configured, default 10/min for stateful) and parked-connection caps: excess connections are closed with a connection reset after an audit op (there is no 429 at L4; the op is the observable).
- Wake failure (start error, wake timeout): parked connections closed, instance marked `failed`, activator stays published so the next connection retries (subject to the rate limit).
- Restart adoption: boot/sweep reconciliation over `stateful_vms`, `stateful_bundles`, `volumes` rebinds live instances, heals `banking`/`relighting` limbo, marks vanished instances by pair validity (`banked` or cold-bootable), evicts pair-broken bundles, re-derives and re-pushes the snapshot including listeners. A control-plane restart with a live stateful VM must republish the identical endpoint without touching the VM.

**Specification:**
- The splice is lifecycle-rate by construction (only connections that arrived during a miss); it must be a dumb byte pump with no framing assumptions (opaque L4, decision 4) and must propagate half-close in both directions.
- Single-flight correctness: N concurrent connections to a banked workload produce exactly one `StartStateful` and N working sessions (property test with fakenode latency injection).
- ExUnit: miss round-trip against fakenode (park, wake, splice, subsequent-connection-via-Envoy), single-flight, cap and rate-limit closes with audit ops, wake-failure retry-ability, adoption matrix (restart during each non-terminal state converges and republishes), pair-mismatch wake path (relight falls back to cold boot and the op-log shows `stateful_cold_booted{generation_mismatch}`).

**Acceptance:** CI green; live drill in the PR: with the drill workload banked, a psql connect wakes it (timed, connect-to-first-query), a second psql session arrives via Envoy with the control-plane log silent, and `GET /v1/stateful/{workload}` shows the published endpoint and matched generation.

**Commit:** `feat(embervm): tcp activator wake-on-connect with single-flight and restart adoption`

### Task 9: Idle-to-bank, TTL, forced roll, and the pairing economics

**Why:** The economics: an idle database must cost one volume file and one bundle, and every path back to running must be either a valid relight or an honest cold boot.

**Deliverables:**
- Stats scrape extension: per-listener `downstream_cx_active` and `downstream_cx_total` join the sweep; `stateful_stats` ops and `last_active_at` updates per standing decision 7.
- Idle-bank: zero active connections and zero cx delta across `idleBankSeconds` triggers unpublish (activator installed in the same update), an active-count recheck, then `StopStateful(BANK)`; a nonzero recheck aborts and republishes.
- Max-lifetime expiry: drain-equivalent for L4 is "wait for zero active connections, then destroy" with a values-capped patience window (default 3600s) after which destroy proceeds anyway (WAL recovery covers it, decision 8); banked-TTL GC evicts stale bundles (the volume is never GC'd).
- Forced roll: `DELETE /v1/stateful/{workload}/instance` (management auth) destroys the instance and evicts the bundle so the next connection cold-boots the current image against the volume (the convergence lever).
- Volume deletion: `DELETE /v1/stateful/{workload}/volume` (management auth, refused while an instance exists) appends `volume_deleted` and calls `DeleteVolume`. The CRD deleting does NOT delete the volume (data outlives definitions; deletion is always the explicit API act).

**Specification:**
- Scrape failure fails open for warmth (never bank on stale stats) exactly as R3.
- ExUnit: idle detection with active-connection guard, abort-on-recheck, TTL patience window, forced roll, volume-deletion refusal, clock-injected throughout.

**Acceptance:** CI green; live: the drill workload left idle past `idleBankSeconds` shows `banked` with zero live VMs and one bundle, the next psql wakes it warm, and a forced roll then makes the next psql cold-boot with data intact.

**Commit:** `feat(embervm): idle-to-bank, ttl, and forced roll for the stateful class`

---

## Phase 3: Operability, the consumer, and closure

### Task 10: Stateful observability and alerts

**Why:** Three new failure surfaces (pairing, volume capacity, wake-on-connect) must be visible before anything real depends on the class; the connection path is deliberately invisible to the control plane, so its story comes from Envoy TCP stats.

**Deliverables:**
- Control-plane spans: activator root span per miss with children `park`, `wake` (`ember.wake_ms`, `ember.relight` bool, `ember.cold_boot_reason`), `publish`, `splice` (duration, bytes in/out); lifecycle spans for `bank`, `stats_sweep`, `forced_roll`. Attributes include `ember.workload`, `ember.generation`.
- Node Envoy TCP listener stats (cx active/total, per-listener stat prefixes) scraped to SigNoz alongside the R3 HTTP stats.
- SigNoz alerts (METRIC_BASED_ALERT seam, threshold-0 dry-run then restore): volume allocated-bytes watermark versus the declared cap (the disk-full-inside-the-guest early warning), repeated `stateful_cold_booted{generation_mismatch}` (pairing regression signal: mismatches should be rare, operator-caused events), sustained wake failures.
- `status.stateful` wired (Task 3), debounced.

**Specification:** The Task 12 gate numbers (wake latency warm and cold, off-path proof, pairing drills) must be derivable from spans plus Envoy stats plus the op-log alone.

**Acceptance:** CI green; spans and TCP metrics visible in SigNoz from a live wake/idle/bank cycle; dry-run alert reaches Discord.

**Commit:** `feat(embervm): stateful observability, pairing alerts, and volume watermarks`

### Task 11: First consumer: scratch-postgres for the agent platform

**Why:** The rung's named consumer (Fork 3): a real, recurring, bursty relational scratchpad for agent workflows that is exactly the shape scale-to-zero pays for.

**Deliverables:**
- A postgres guest image under `projects/embervm/runtimes/postgres/`: apko-built (dual-arch), non-root (uid 65532 within the guest userland where postgres permits; document the guest uid story), postgres from apk, an init script that on first boot (blank `PGDATA` on the mounted volume) runs initdb and sets the superuser password from the MMDS env seam, then execs postgres listening on 5432 with `PGDATA` on `volumeMountPath`. Contract per ADR 001 image source: listen on the declared port; health is TCP connect.
- A `scratch-postgres` Workload CR in the embervm chart: `class: stateful`, image source pinned by digest through the existing pipeline, `{port: 5432, listenPort: 5432-mapped range port, volumeSizeGiB: 10, idleBankSeconds: 600, maxLifetimeSeconds: 604800, bankedTtlSeconds: 2592000}`, sized per the fc-base sizing coupling (shared_buffers sets memMib).
- Credentials via the 1Password Operator into the MMDS env seam (never hardcoded); the DSN (`postgresql://...@embervm-serving.<ns>.svc:<listenPort>/scratch`) surfaced to the agent platform: a `SCRATCH_POSTGRES_DSN` env on the monolith agent runtime via values, consumed by agent tools/recipes that want relational scratch state (first wiring: expose it to `run_python` session env so agent sessions can `psycopg`-connect).
- Chart bumps per the bump rules in the same PR.

**Specification:**
- Postgres tuning stays minimal and honest for the tier: `fsync=on` (the volume IS the durability story), small shared_buffers, `listen_addresses='*'` with password auth (pg_hba scram) because network exposure is the cluster-internal Service only.
- Warm-versus-cold numbers recorded: connect-to-first-query on a banked instance (relight) versus after a forced roll (cold boot with WAL recovery) versus first boot ever (mkfs + initdb).
- pytest for any monolith env-plumbing change (hand-registered py_test); no new RBAC.

**Acceptance:** CI green; an agent session (or a documented psql drill from the monolith pod) writes rows, the workload idles to banked, a later session reads the rows back through a wake-on-connect; the latency table is in the PR description.

**Commit:** `feat(embervm): scratch-postgres, a scale-to-zero datastore for the agent platform`

### Task 12: R4 gates and closure

**Specification (the gates, all measured, appended to this plan as a Closure section):**
1. **Data survives every transition:** rows written, then bank -> relight -> rows present; forced roll -> cold boot -> rows present (WAL recovery observed in guest logs); noded restart mid-life -> next wake cold-boots or relights per pair validity -> rows present. Zero data loss across the full matrix.
2. **The pairing invariant, drilled both ways:** (a) a relight with matched generations resumes warm (`stateful_relit` op, warm connect-to-first-query recorded); (b) after an explicit `COLD` boot invalidates the pair, the next wake logs `stateful_cold_booted{generation_mismatch}`, the stale bundle is evicted, and the data is intact. The op-log alone tells the whole story.
3. **Wake-on-connect:** psql connect to a banked scratch-postgres succeeds; park-to-first-query p95 <= 2s warm (relight) over 10 cycles; cold boot recorded, no gate (WAL recovery time is the database's business).
4. **Singleton enforcement:** 20 concurrent psql connects against a banked workload produce exactly one `StartStateful` (op-log count) and 20 working sessions; a manual second-attach attempt at the daemon is refused `FAILED_PRECONDITION`.
5. **Established connections are off the control-plane path, destructively proven:** with a psql session open and mid-transaction, scale the control plane to zero for 60 seconds; the session continues uninterrupted (queries succeed throughout). Restore; adoption republishes an identical snapshot (diff of `GET /snapshot/...`).
6. **Never-sever banking:** a workload with one long-lived open connection is never banked regardless of idle timers (active-count guard observed over 2x `idleBankSeconds`); closing the connection then banks it on the next sweep.
7. **Adoption drill:** control-plane restart with one live and one banked stateful workload converges with zero orphaned VMs, bundles, volumes, or stale endpoints after one sweep; noded restart resolves the live instance per pair validity and the workload recovers via the activator.
8. **Volume containment:** the volume file is never mounted or read host-side (code-review assertion plus no mount syscalls in the daemon's volume path), and `DeleteVolume` is the only path that removes data, refused while attached (drilled).
9. **Consumer soak:** scratch-postgres serves agent-platform traffic for 48h across at least 5 bank/wake cycles with zero stateful-attributed errors and the warm/cold latency table recorded.

**Deliverables:** Gate numbers in the Closure section; ADR embervm/001 roadmap row R4 to `Shipped <date>` and the R4 paragraph annotated with the shipped shape; this plan's out-of-scope list carried into the R5 planning seed.

**Commit:** `docs(embervm): R4 closure with gate evidence`

---

## Explicitly out of scope for R4 (recorded, not dropped)

- **Protocol-aware L4 (the Postgres wire filter):** connection routing plus protocol stats per the ADR 001 visibility spectrum's middle tier. v1 is opaque; the filter arrives when per-query visibility or read/write route splitting is actually consumed.
- **Clustered stateful (CNPG-shaped):** per-VM volumes, an EmberVM-supervised election process per cluster through the `ra` tier, rw/ro route flips via xDS. Recorded in ADR 001 for later; R4 v1 is the scale-to-zero singleton tail on purpose.
- **Volume migration, replication, and off-node backup:** volumes are node-anchored single copies; the ADR 003 distribution arc is the recorded off-node story. Losing the node loses the volume, stated in the class contract.
- **Volume resize and volume snapshots:** `volumeSizeGiB` immutable; LVM (Fork 1 Option B) is the re-rank trigger when either is truly needed.
- **Guest-cooperative clean shutdown (ACPI/agent stop verb):** v1 destroy is kill + WAL recovery (decision 8).
- **Public L4 exposure:** cluster-internal only; a public database endpoint is a different threat model and its own decision.
- **Preview-per-branch database provisioning:** the natural second consumer once a preview-environment workflow exists; the singleton machinery is what it would stamp out.
- **Connection-level fairness or per-principal connection quotas at Envoy:** v1 guards the wake path only; opaque L4 cannot see principals inside connections.
- **TLS on the stateful path:** in-cluster plaintext with DB-native auth, consistent with the current posture; revisit with the mesh migration.

## Open risks tracked for execution

| Risk | Watch signal | Fallback |
| ---- | ------------ | -------- |
| Firecracker snapshot resume with a writable drive misbehaves (device state vs backing file assumptions) | Task 4 bank/relight round-trip drill; guest fs errors after relight | The pairing rule already gates on nothing-touched-since-pause; if resume proves unreliable, v1 degrades to cold-boot-only wakes (decision 1's fallback made default) while keeping the pairing metadata |
| Dynamic LDS churn resets TCP listeners and drops established connections on unrelated updates | Gate 5/6 drills; Envoy listener-draining stats on publish | Envoy updates listeners in place when config is unchanged; keep listener config minimal and stable (only add/remove), and pin the HTTP listener static if LDS-managing it proves disruptive (Task 5 records the choice) |
| The activator's byte splice mishandles half-close or long-lived idle connections | Gate 3; psql sessions through a wake dying early | Splice only ever carries miss-window connections; worst case close them and let the client reconnect via Envoy (document the reconnect-once caveat) |
| Postgres in a microVM with 1 vCPU / small mem is too slow to be a credible consumer | Task 11 latency table | Size up per the fc-base sizing coupling; scratch tier tolerates modest numbers; the gate is correctness, not throughput |
| initdb-on-first-boot exceeds the wake timeout | Task 11 first-boot timing | `wakeTimeoutSeconds` is per-workload and generous; first boot can also be triggered administratively (a management-API wake) before first use |
| Volume sparse file fills node NVMe alongside snapshots | Task 10 watermark alert; `snapshot_disk_free_bytes` | The declared cap bounds each volume; the watermark alert fires before the guest sees ENOSPC; volume count is small (singletons) by construction |
| Agents hold connections open (pools) and the workload never idles | Gate 6 vs consumer behavior; cx_active flatlines at nonzero | Consumer-side: no pooling in the run_python wiring (connect per use); the never-sever rule is correct regardless, and `maxLifetimeSeconds` still bounds the instance |
| Generation ledger corruption (partial write, disk full) strands the pair check | `stateful_cold_booted{ledger_unreadable}` ops | Ledger writes are write-temp-rename; unreadable fails open to cold boot (warmth lost, data safe), and the alert on repeated mismatches catches systemic cases |

---

## Closure (2026-07-17)

R4 is **code-complete on main**: all 12 tasks shipped across 8 PRs (contract, noded volume manager, xDS LDS, node Envoy TCP exposure, StatefulStore + FSM + TCP activator + adoption, idle-to-bank sweeper, observability, the MMDS-lite first-boot secret seam, and the scratch-postgres consumer). The control plane can register a `class: stateful` workload, wake it on an inbound TCP connection, bank it when idle, and relight-or-cold-boot it on the exact bundle-vs-volume generation pairing.

**What is shipped vs what remains a live drill.** The nine gates below are, by their nature (plan Task 12), *measured* acceptance drills against a deployed datastore: they need `scratch-postgres` enabled (its feature flag is off by default until the operator creates the 1Password item and enables `scratchPostgres` in both `deploy/values.yaml` files) and run on node-4 with a real Firecracker microVM. They cannot be measured from a workstation (no local FC, no live cluster write access under GitOps). Each gate is recorded with its exact procedure and the code that makes it pass; the **numbers are appended after the operator runs the drill**. Gates whose mechanism is already covered by merged ExUnit/Go tests are marked *(unit-verified)*; those needing live infra are marked *(live-pending)*.

### The gates

1. **Data survives every transition** *(live-pending)*. Write rows; bank then relight, rows present; force a roll (cold boot), rows present with WAL recovery in the guest log; restart noded mid-life, next wake cold-boots or relights per pair validity, rows present. Zero loss across the matrix. *Code:* volume is a durable raw file on node NVMe attached before the generation bump (Task 4); FSM never destroys a volume on eviction (Task 7); `DeleteVolume` is the only data-removing path (Task 1).
2. **The pairing invariant, both ways** *(unit-verified for the op-log path; live-pending for warm timing)*. (a) matched generations resume warm (`stateful_relit`); (b) after an explicit `COLD` invalidates the pair, the next wake logs `stateful_cold_booted{generation_mismatch}`, the stale bundle is evicted, data intact. *Code:* the mismatch/eviction path is covered by the PR-4 op-log tests; the warm connect-to-first-query number is live-pending.
3. **Wake-on-connect** *(live-pending)*. psql connect to a banked scratch-postgres succeeds; park-to-first-query p95 <= 2s warm (relight) over 10 cycles; cold boot recorded, no gate. *Code:* `TcpActivator` parks the connection, single-flights the wake, publishes the real endpoint, and splices bytes (Task 8, unit-verified against fakenode); the p95 is live-pending.
4. **Singleton enforcement** *(unit-verified for single-flight; live-pending for the 20-way count)*. 20 concurrent psql connects to a banked workload produce exactly one `StartStateful` and 20 working sessions; a manual second attach at the daemon is refused `FAILED_PRECONDITION`. *Code:* single-flight is a property test in PR-4; the daemon refusal is the node registry's fail-closed singleton gate.
5. **Established connections off the control-plane path, destructively proven** *(live-pending)*. With a psql session mid-transaction, scale the control plane to zero for 60s; the session continues; on restore, adoption republishes an identical snapshot (diff of `GET /snapshot/...`). *Code:* the data plane is per-node Envoy fed by the xDS sidecar; the control plane is off the hit path by construction (R3 invariant + Task 5); adoption republish is unit-verified (Task 8), the destructive 60s drill is live-pending.
6. **Never-sever banking** *(unit-verified)*. A workload with one long-lived open connection is never banked regardless of idle timers (active-count guard over 2x `idleBankSeconds`); closing it banks on the next sweep. *Code:* the sweeper banks only when TCP `cx_active == 0` and `cx_total` is flat (Task 9), covered by the sweeper tests.
7. **Adoption drill** *(unit-verified for convergence; live-pending for the zero-orphan count)*. Control-plane restart with one live and one banked workload converges with zero orphaned VMs/bundles/volumes/endpoints after one sweep; noded restart resolves the live instance per pair validity. *Code:* the adoption matrix (restart during each non-terminal state) is a PR-4 test; the live two-workload count is live-pending.
8. **Volume containment** *(code-review assertion, satisfiable now)*. The volume file is never mounted or read host-side (no mount syscalls in the daemon's volume path); `DeleteVolume` is the only removal path and is refused while attached. *Code:* noded attaches the raw file as a guest block device and never mounts it in the host mount namespace (Task 4); the guest mounts it. This gate is a standing code-review invariant, re-asserted here as satisfied.
9. **Consumer soak** *(live-pending)*. scratch-postgres serves agent traffic for 48h across >= 5 bank/wake cycles with zero stateful-attributed errors and the warm/cold latency table recorded. *Code:* the consumer wiring (`SCRATCH_POSTGRES_DSN` to `run_python`) ships behind its flag (Task 11); the soak is the operator's post-enable observation.

### Operator runbook to close the live-pending gates

1. Create the 1Password item with a `POSTGRES_PASSWORD` field; point both `projects/embervm/deploy/values.yaml` (`scratchPostgres`) and `projects/monolith/deploy/values.yaml` (`scratchPostgres.enabled: true`, `onepassword.itemPath`) at it (same item, both namespaces).
2. Bump the embervm + monolith (+ monolith-public) charts, merge, let ArgoCD sync.
3. Run gates 1-5, 7, 9 as psql drills from a debug pod / the monolith pod; record numbers back into this section.

### R5 planning seed (out-of-scope carried forward)

The R4 out-of-scope list above is the R5 intake: **clustered stateful** (CNPG-shaped per-VM volumes, EmberVM-supervised election through the `ra` tier, rw/ro route flips via xDS) is the headline R5 primitive; **preview-per-branch database provisioning** is the natural second stateful consumer that stamps out the singleton machinery; **protocol-aware L4** (the Postgres wire filter) arrives when per-query visibility or read/write routing is actually consumed; **volume migration/replication/resize** track ADR 003's distribution arc. R4 deliberately shipped the scale-to-zero singleton tail first.
