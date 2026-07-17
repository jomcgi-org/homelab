# EmberVM R5 (Composite) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or superpowers:executing-plans in a separate session) to implement this plan task-by-task. This document is the committed spec for rung R5 of [ADR embervm/001](../decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md), riding the R2 bank/relight machinery, the R3 xDS tier, and the R4 L4 activator lane. Every task is a specification with acceptance criteria; no implementation lives here.

**Goal:** Ship EmberVM R5: composite workloads. A composite workload is a **group** of member microVMs that share a private per-group subnet (stable internal IPs across restores), start in role order, and live, bank, relight, and die **as one unit**. The rung's headline property is the ADR 001 showcase made real: instant, legitimately distributed Kubernetes environments with separate kernels, a real inter-node network, and real node kills. The named first consumer is **`scratch-k8s`**: a scale-to-zero three-node k3s cluster (one server, two agents) the agent platform reaches through a wake-on-connect kubectl endpoint, costing a bundle set on disk while idle instead of three running VMs.

**Architecture:** The node contract gains group networking and member lifecycle: noded owns a Linux bridge per group instance (generalizing the R3 serving bridge), pins each member's tap name, MAC, and IP so Firecracker snapshot restore finds an identical world, and resyncs each guest's wall clock over vsock immediately after resume. The control plane gains a `GroupStore` plus a per-group `GroupManager` process that sequences role-ordered member start, group bank (pause all members, snapshot each, record a **bundle set**), and group relight (all-members-or-none: a partial set is evicted and the group boots fresh). The group's single entry endpoint reuses the R4 L4 lane unchanged: one TCP listener on the node Envoy, empty-cluster fallback to the TCP activator, connection-open wakes the whole group. Guest images stay where the k8s knowledge lives: EmberVM injects generic `EMBER_GROUP_*` facts (role, own IP, peer map, a per-instance group secret) through the MMDS-lite seam, and the k3s images map them to `K3S_URL`/`K3S_TOKEN` themselves.

**Tech Stack:** Elixir/OTP control plane and Go noded (existing), per-group Linux bridges + nftables inter-group isolation in noded's pod netns, Firecracker snapshot restore with pinned tap/MAC/IP, vsock guest control agent (clock resync), `go-control-plane` LDS/EDS (existing, reused), k3s with baked airgap images on apko guest rootfs (kata `vmlinux.container` guest kernel), gRPC group verbs on `embervm.node.v1`, SQLite op-log projections (ADR 002 discipline), Workload CRD `class: composite` + the reserved `spec.group` block, Helm + ArgoCD.

---

## Standing decisions (settled, do not relitigate during execution)

1. **The group is the lifecycle atom; members are not workloads.** One `class: composite` Workload CR defines one group; exactly one group instance exists per CR (the R4 singleton discipline). Create, bank, relight, TTL expiry, forced roll, and destroy act on the whole group. Members have no individual CRD surface, no individual endpoints beyond the declared entry, and no individual bank.
2. **Warmth is the only state; the contract says so plainly.** Composite members own no volumes in v1. Bank is the persistence story: destroy (or a failed relight, or TTL expiry) loses the group's entire state and the next wake is a fresh boot of a brand-new environment. This is the correct contract for ephemeral environments and it is stated in the CRD docs, the sample, and the consumer docs. Durable members (a group member owning an R4 volume) are the recorded bridge to clustered stateful, out of scope here.
3. **All-members-or-none is the warmth invariant (R5's analog of R4's generation pairing).** A group bank produces a **bundle set**: one snapshot bundle per member, stamped with the same `set_id`, recorded atomically in the op-log. Relight requires the complete set on the volume node; a partial or unreadable set is evicted whole and the group boots fresh with `fresh_boot_reason` recorded. Warmth fails open; there is no data tier to fail closed (decision 2).
4. **One L2 bridge per group instance; isolation is the bridge boundary plus an nftables deny.** noded creates a dedicated Linux bridge per group instance with a /24 allocated from a values-declared composite supernet (default `10.101.0.0/16`). Members on the same bridge reach each other at L2; an nftables forward-chain rule drops routed traffic between composite bridges and from composite bridges to the serving bridge, so groups cannot reach each other or other classes through the host. Members have **no egress** beyond their group and the entry path (the task-class zero-egress posture, applied at L3).
5. **Member addresses are deterministic facts, pinned across restores.** The member list expands replicas into names first (`replicas: 2` on member `agent` yields `agent-0`, `agent-1`; `replicas: 1` keeps the bare name), and expanded member `i` of that flattened, declaration-ordered list gets `.10 + i` on the group subnet, with a deterministically derived MAC, and the tap device name is pinned per expanded member. Expanded names are the identity everywhere: the op-log, `EMBER_PEER_*` keys, and `NodeStatus`. Firecracker snapshot restore requires the identical host device world; recreating tap/MAC/IP exactly is what makes group relight possible, and it is what makes `EMBER_PEER_*` env values valid for the life of the instance (the D-R3.4.1 pin, generalized).
6. **Role-ordered start, health-gated per stage.** Each member declares a `role` and each role an ordinal (`startOrder`). The GroupManager starts order-0 members first, TCP-health-gates them on their declared port, then starts the next order (members within one order start in parallel). k3s maps to this as server (order 0, port 6443) then agents (order 1, port 10250). Relight resumes in the same order.
7. **Clock resync is a required, verified step of every member resume.** Immediately after a member's snapshot resume, noded sends the host's epoch over the member's vsock control channel and the guest control agent sets `CLOCK_REALTIME`; noded then reads the guest clock back and fails the member resume if the delta exceeds one second. A member that fails clock resync fails the group relight, which (decision 3) evicts the set and boots fresh: a wrong-clock Kubernetes cluster (TLS validity, etcd leases, lease renewals) is worse than a fresh one.
8. **The entry endpoint reuses the R4 L4 lane byte-for-byte, and it covers the no-instance case.** `spec.group.entry` names one member and port, exposed as cluster `group|<workload>` behind one TCP-proxy listener on the node Envoy (LDS from the existing sidecar), with the TCP activator as the empty-cluster fallback. The listener exists for the life of the CR: when no instance exists at all (before first use, after TTL expiry, after a forced roll), the activator stays the published fallback and a connection triggers a full group **create** (network plus ordered fresh start), so scale-to-zero holds from birth and decision 2's "the next wake is a fresh boot" is literally the connection path. Connection-open on a banked group triggers relight instead; both are single-flighted through the GroupManager. Wake-rate limits and parked caps apply unchanged.
9. **Bank only when the entry is idle; never sever a live connection, including spliced ones.** The idle signal is the R4 TCP stats scrape on the group's entry listener: zero active connections and a flat total across `idleBankSeconds`, **plus zero live activator-spliced connections** (the control plane counts its own splices; a long-lived session that began during a wake is invisible to Envoy and must still block banking). Intra-group traffic (kubelet chatter on the bridge) is invisible to Envoy by construction and correctly does not count as activity. The bank sequence unpublishes (activator installed in the same update), rechecks both counts, pauses all members, snapshots each, then destroys, recording the set atomically.
10. **Group snapshots are crash-consistent per VM, never transactionally consistent across VMs, and the platform says so.** Members are paused as close together as the daemon can manage but with no cross-VM barrier; each member's snapshot is that VM's crash point. Distributed state inside the group (etcd/sqlite WALs, kubelet leases) recovers by the application's own crash recovery on resume, which is precisely what real distributed systems must survive. The constraint is stated in the CRD docs and the ADR already records it.
11. **A dead member degrades the group; it does not fail it.** Real node kills are the showcase: the platform reports member health (`degraded` condition naming the dead member) and keeps the group published, because Kubernetes tolerating node loss is the point. There is no member auto-heal in v1; recovery is the consumer's k8s-level story or a forced roll. A dead member at bank time excludes the group from banking (a partial set would violate decision 3); the group stays live until healed by roll or expired by TTL.
12. **Zero-egress k8s via baked airgap images, not a registry cache.** The k3s guest images vendor the k3s binary plus its airgap image tarball in the apko rootfs, so cluster bring-up pulls nothing. The per-node pull-through OCI cache (the agents/041 generalization ADR 001 names) is the recorded follow-on for when guest clusters must run arbitrary user images; the showcase does not need it and it is its own piece of infrastructure.
13. **EmberVM stays k8s-agnostic; the guest image owns the k3s knowledge.** The platform injects only generic facts through the MMDS-lite seam: `EMBER_GROUP_MEMBER` (expanded member name), `EMBER_GROUP_ROLE`, `EMBER_GROUP_IP`, `EMBER_PEER_<NAME>=<ip>` per peer (expanded names uppercased, `-` mapped to `_`), and `EMBER_GROUP_SECRET`. The secret comes from `spec.group.secretRef` (a Kubernetes Secret key, 1Password-managed, **stable across instances** so consumer credentials survive rolls and fresh boots) when declared, else it is minted per instance. The k3s images' init maps these to `K3S_TOKEN`, `K3S_URL`, and token-auth config. Nothing k3s-shaped enters the CRD, the proto, or the control plane. The seam's budget is stated (D-R4.PR-7.1): boot-args carry a few small values against a 2048-4096 byte kernel cmdline, so `maxGroupSize` is bounded by the peer-map fitting the cmdline, not just by capacity.
14. **The node reports, the control plane adopts.** `group_networks`, `group_member_vms`, and `group_bundle_sets` join `NodeStatus`; boot/sweep reconciliation rebinds live groups, heals `banking`/`relighting` limbo, evicts incomplete sets, and re-derives the full xDS snapshot from adopted facts. A control-plane restart must converge without touching any VM (the #3517 lesson, fifth application).
15. **Additive proto only; task, session, serving, and stateful contracts stay frozen.**

### Fork 1: per-group networking mechanism (settled choice)

- **Option A (chosen): one Linux bridge per group instance in noded's pod netns, /24 from a values-declared supernet.** Generalizes the exact machinery R3 built (bridge, taps, DNAT through noded's pod IP for the entry path per D-R3.11.4); isolation between groups is bridge separation plus one nftables forward deny; stable IPs are trivially assignable per member index. Costs: bridge count grows with live groups (bounded: singletons per CR, few CRs), and everything stays single-node (already true of the fleet).
- **Option B: VLANs on the shared serving bridge.** One bridge, per-group VLAN tags. Rejected: VLAN-aware bridge config plus per-tap tagging is more machinery than separate bridges, for zero v1 benefit at this group count.
- **Option C: a network namespace per group.** Strongest isolation on paper, but Firecracker processes run in noded's netns today; per-group netns means veth plumbing, cross-netns tap management, and a jailer interaction audit. Rejected for v1; re-rank if inter-group isolation ever needs to survive a noded compromise (it does not today: noded is already the trust boundary for all classes).

### Fork 2: guest clock resync mechanism (settled choice)

- **Option A (chosen): a vsock guest control agent set-clock command.** A tiny agent baked into composite guest images listens on a dedicated vsock port; after resume, noded sends `{"cmd":"sync_clock","epoch_ns":...}` and verifies by reading the clock back. Deterministic, observable, works with the kernel we have, and the agent seam is reusable (future: clean-shutdown, in-guest probes).
- **Option B: PTP/kvm-clock (`phc2sys` against `/dev/ptp0`).** The upstream-blessed continuous mechanism, but it needs `CONFIG_PTP_1588_CLOCK_KVM` in the guest kernel plus a running daemon per guest, and continuous sync is overkill for a resume-time step correction. Recorded as the upgrade if drift between resyncs ever matters.
- **Option C: no resync (status quo for R2 sessions).** Sessions tolerate stale clocks because nothing in a Python sandbox checks TLS validity windows. A Kubernetes cluster does. Rejected; the ADR names resync as a required step.

### Fork 3: Kubernetes distribution for the consumer (settled choice)

- **Option A (chosen): k3s, one server (sqlite datastore) + two agents.** Single static binary, first-class airgap support (vendored image tarball), tiny footprint, arm64+amd64 releases, and flannel's `host-gw` backend works over the group's flat L2 subnet without vxlan kernel modules. Single-server sqlite keeps the crash-consistency story simple (one WAL, one VM).
- **Option B: kubeadm.** The "real" distribution, but needs a container runtime image set, systemd-shaped init, and far more rootfs surface per member. Rejected for v1.
- **Option C: k0s.** Comparable to k3s; k3s wins on airgap maturity and prior art in microVMs.

### Fork 4: first consumer (settled with Joe, 2026-07-16)

- **Chosen: `scratch-k8s` for the agent platform.** A single scale-to-zero k3s cluster the monolith agent toolchain reaches via a kubeconfig (wake-on-connect at the API port): agents get a disposable, legitimately distributed Kubernetes to operate against (kubectl drills, demos, recipes), and the firecracker story page gets its showcase. Group bank/relight is **in v1 scope** (also settled): resuming a whole cluster in seconds is the rung's wow and the scale-to-zero economics are the reason groups beat pods.
- Per-session stamped instances (N ephemeral clusters from one definition) and a DB-cluster consumer (CNPG-shaped election) were considered and deferred; see out-of-scope.

## Cross-cutting constraints

- **No local test loop.** Implement, commit, push, watch BuildBuddy CI (`gh pr checks <n> --watch`). ExUnit, Go, and pytest targets run under `bazel test //...` in CI only.
- **Conventional Commits; no em-dashes anywhere.**
- **Charts bump via `bazel/tools/git/bump-chart.sh`** in the same PR as the code they deploy. Docs-manifest regeneration (this plan, ADR touches) forces monolith AND monolith-public chart bumps.
- **RBAC verbs verified per task** for every new K8s API call before merge. Expected new runtime RBAC: exactly one addition, namespace-scoped `get` on `secrets` for the control plane (the `spec.group.secretRef` read, Task 6); CRD and Service changes are chart-managed.
- **Op-log schema changes use the guarded ALTER-after-DDL migration pattern.**
- **Additive proto only:** new RPCs, messages, and `NodeStatus` fields; existing verbs untouched.
- **Repository layout:** control plane under `projects/embervm/control/`, xDS sidecar under `projects/embervm/xds/`, noded under `projects/embervm/noded/`, guest images under `projects/embervm/runtimes/`, chart pieces under `projects/embervm/chart/`.
- **R4 live-pending gates:** R5 rides the R4 L4 lane whose live drills are still pending; the Task 3 spike and the R5 gates double as exercise of that lane, and any R4 defect found is fixed under R4's contract, not worked around.
- **One comprehensive code review per merged PR.**

## Suggested PR partitioning

| PR | Tasks | Deploys |
| -- | ----- | ------- |
| PR-0 docs (this branch) | this plan | manifests + monolith/public chart bumps |
| PR-1 contract | 1, 2 | additive proto + op-log schema + CRD; no behavior change |
| PR-2 guest substrate spike | 3 | k3s guest image + guest control agent; single-VM drill on noded, no group machinery |
| PR-3 noded groups | 4, 5 | group networks, member lifecycle, clock-resync-on-resume live on noded, unused |
| PR-4 composite core | 6, 7 | GroupStore + GroupManager + publisher + activator reuse; create and wake-on-connect live |
| PR-5 lifecycle economics | 8 | group bank/relight, TTL, forced roll, degraded handling |
| PR-6 operability | 9 | group spans, member/bridge metrics, set-eviction and disk alerts |
| PR-7 consumer | 10 | scratch-k8s live and wired to the agent platform |
| PR-8 closure | 11 | R5 marked shipped in ADR 001 |

---

## Phase 0: Contract and schema foundations

### Task 1: Node contract group verbs and facts

**Why:** The rung's physical primitives (a bridge, N pinned-address members, a bundle set) live on the node; the contract must state them before anything consumes them.

**Deliverables:**
- Additive RPCs on `embervm.node.v1.NodeService`, proto comments as the spec:
  - `rpc CreateGroupNetwork(CreateGroupNetworkRequest) returns (CreateGroupNetworkResponse)`: create the per-group bridge for `{group_instance_id, cidr}` plus the inter-group nftables deny; idempotent per instance id; `FAILED_PRECONDITION` on CIDR overlap with an existing bridge. Returns `{bridge_name, gateway_ip}`.
  - `rpc DeleteGroupNetwork(...)`: tears the bridge and rules down; `FAILED_PRECONDITION` while any member VM is attached.
  - `rpc StartGroupMember(StartGroupMemberRequest) returns (StartGroupMemberResponse)`: boot or resume one member on its group bridge. Carries `{group_instance_id, member_name, member_index, ip, mode oneOf FRESH|RELIGHT, snapshot_ref (RELIGHT), source, resources, env map, health_port, Trace}`. FRESH cold-boots the image rootfs with a tap pinned to the member's deterministic name/MAC/IP and TCP-health-gates `{ip, health_port}`. RELIGHT recreates the identical tap world, resumes the bundle, then performs the clock-resync handshake (standing decision 7) and fails the call if the verified delta exceeds one second. Returns `{vm_id, ip, was_relight}`.
  - `rpc StopGroupMember(StopGroupMemberRequest) returns (StopGroupMemberResponse)`: `mode = BANK` (pause, snapshot to `group/<set_id>/<member>/`, destroy, return `{snapshot_ref, size_bytes}`) or `mode = DESTROY` (kill, no snapshot). The daemon never decides set membership; `set_id` is caller-supplied and opaque.
  - Group bundles reuse `EvictSnapshot` unchanged (refs are opaque; the control plane evicts a set by evicting each ref).
- `NodeStatus` additions (wire-compatible): `group_networks: [{group_instance_id, cidr, bridge, member_count}]`, `group_member_vms: [{vm_id, group_instance_id, member_name, ip, healthy, last_probe_unix_ms}]`, `group_bundle_sets: [{set_id, group_instance_id, members: [{member_name, snapshot_ref, size_bytes}], created_at_unix_ms}]` (the daemon reports refs grouped by set directory; completeness judgment stays in the control plane).
- Guest control agent contract in proto comments: the vsock port, the `sync_clock` request/response JSON frames, and the read-back verification rule.
- fakenode serves the new verbs and status fields, including scripted partial-set and clock-resync-failure outcomes.

**Specification:**
- Member VMs count against `max_live_vms` and are excluded from `primed_vm_ids`.
- The env map is the MMDS-lite seam exactly as `StartStateful` uses it; the platform-injected `EMBER_GROUP_*` keys are composed by the control plane, not the daemon. Env is **FRESH-only** (D-R4.PR-7.1: boot-args are never re-read on resume); the proto comment states that RELIGHT ignores the env map and the resumed guest keeps its birth env.
- Go+Elixir stubs regenerate through the existing pure-genrule codegen; fake-server round-trip tests per verb and per outcome (fresh, relight, relight-clock-fail, bank, destroy, network create/delete/refusal) in CI.

**Acceptance:** CI green; reviewer confirms additivity, that the four existing class contracts are untouched, and that nothing k3s-shaped appears in the proto.

**Commit:** `feat(embervm): group network, member lifecycle, and bundle-set facts on the node contract`

### Task 2: Op-log group records, projections, and the CRD composite class

**Why:** Group lifecycle is enforcement-adjacent (creates, banks, set evictions, degradations must be ordered and auditable), and the definition surface must open the reserved room without breaking any existing class.

**Deliverables:**
- Additive op kinds: `group_created, group_net_created, group_net_deleted, group_member_started, group_running, group_published, group_unpublished, group_banked, group_relit, group_fresh_booted, group_set_evicted, group_degraded, group_destroyed, group_failed, group_stats`. The terminal `expired` state rides `group_destroyed{reason: expired}` (write-through discipline: every transition appends; expiry is a destroy with a reason, not its own kind).
- New projection tables: `group_instances(instance_id TEXT PRIMARY KEY, tenant, principal, workload, state, node_id, subnet_cidr, entry_member, entry_port, listen_port, set_id, created_at, last_active_at, updated_at, terminal_reason)` and `group_members(instance_id, member_name, member_index, vm_id, ip, state, snapshot_ref, healthy, updated_at, PRIMARY KEY (instance_id, member_name))`, plus a nullable `group_instance_id` column on `ops` (guarded ALTER-after-DDL).
- `group_fresh_booted` carries `{reason: no_set|partial_set|set_unreadable|clock_resync_failed|explicit}`; `group_set_evicted` carries the reason and member refs, so every discarded-warmth event reconstructs from the log alone. `group_banked` records the full set atomically in one append (decision 3's atomicity lives here).
- `group_stats` rides the R4 stats sweep shape; usage accrues live-seconds **per member** (a 3-member group bills 3 VMs' worth), upserted in the same transaction (the D12.1 pattern).
- CRD: `class: composite` joins the enum; `spec.group` (the reserved room, now defined): `{members: [{name (DNS-label, unique), role, startOrder (int, default 0), replicas (default 1), source {image}, resources {vcpus, memMib}, healthPort (required; TCP health gate), env (optional map)}], entry {member (must name a member, or an expanded replica name), port, listenPort (validated inside the values-declared composite listener range, default 5410-5419, unique)}, secretRef (optional {name, key}; when set, EMBER_GROUP_SECRET reads this Kubernetes Secret and is stable across instances, else minted per instance), idleBankSeconds (default 600, min 30), maxLifetimeSeconds (default 86400), bankedTtlSeconds (default 604800), wakeTimeoutSeconds (default 120; a group relight is N resumes plus clock resyncs, and fresh boot is a full cluster bring-up)}`. `spec.serving`, `spec.session`, `spec.stateful`, and `spec.concurrency` are condition-rejected on this class. One size cap: the expanded member count (sum of replicas) is capped by a values-declared `maxGroupSize` (default 4), itself bounded by the kernel-cmdline env budget (standing decision 13).
- `status.group: {state, members: {live, degraded}, setId, subnetCidr}` + a `COMPOSITE` printer column; the crash-consistency and warmth-only contracts (standing decisions 2 and 10) stated in the CRD field docs.
- Sample composite CR under `projects/embervm/crd/samples/workload-composite.yaml`.
- Retention per ADR 002: terminal instances prune past retention.

**Specification:**
- State transitions are write-through appends before ETS visibility; the xDS snapshot is never derived from the durable store on the publish path.
- ExUnit: projection rebuild equivalence from scripted op sequences including a full bank/evict/fresh-boot cycle; watcher parse/validation tests (duplicate member names, entry referencing a missing member, listener range/uniqueness, group-size cap, class cross-rejection).

**Acceptance:** CI green; kill-and-restart test extended to rebuild group and member state exactly; sample CR round-trips; `kubectl get workloads` shows the composite column.

**Commit:** `feat(embervm): composite workload class, group records, and bundle-set audit in the op-log`

---

## Phase 1: Guest substrate and the node

### Task 3: k3s guest images, the guest control agent, and the single-VM spike

**Why:** The whole rung stands on k3s actually running inside a Firecracker guest on the kata `vmlinux.container` kernel. That is the riskiest unknown (kernel config, airgap size, memory floor), so it is proven as a single VM before any group machinery exists, and the guest control agent ships with it.

**Deliverables:**
- A guest control agent (Go, `projects/embervm/noded/guestagent/` or embedded in guest-init lineage): listens on the dedicated vsock port, serves `sync_clock` (set `CLOCK_REALTIME` from the payload, respond with the post-set clock), length-prefixed JSON frames per the D-R2.6.1 convention. Baked into composite guest images as a supervised sidecar of the image init.
- Two apko images under `projects/embervm/runtimes/k3s/`: `k3s-server` and `k3s-agent`, dual-arch, vendoring the k3s binary plus the matching airgap images tarball. Init maps the injected facts per standing decision 13: server runs `k3s server` with sqlite, `--flannel-backend=host-gw`, `--token $EMBER_GROUP_SECRET`, and a static token-auth entry derived from the same secret for API access; agents run `k3s agent --server https://$EMBER_PEER_SERVER:6443 --token $EMBER_GROUP_SECRET`. Health surfaces: server 6443, agent 10250.
- The spike, documented in the PR: boot one `k3s-server` guest on noded via the existing serving lane (a throwaway drill, no composite machinery), confirm the node reaches Ready with zero egress, and record the memory floor, rootfs size, airgap tarball size, and boot-to-Ready time. Confirm (or patch, via the apko-config-checksum-patch seam if kernel-adjacent) the guest kernel provides what k3s needs: overlayfs, br_netfilter, veth, nf_conntrack, iptables/nft. The kata kernel is container-shaped and netfilter=y was already patched in for kata; verification, not faith.

**Specification:**
- Non-root where k3s permits; k3s realistically needs root inside the guest, which is acceptable: the microVM boundary is the isolation statement (task-class posture notes apply, and this is stated in the image README rather than silently deviating from the uid 65532 convention).
- Image sizing recorded against the fc-base sizing coupling (the airgap tarball import at first boot sets the memory high-water mark).
- Go tests for the agent (frame codec, clock-set path behind an interface, table-driven).

**Acceptance:** CI green; the PR description contains the spike's numbers (boot-to-Ready, memory floor, rootfs size) and the kubectl-get-nodes output from the drill; any kernel config gap found is resolved or explicitly recorded with its fix path.

**Commit:** `feat(embervm): k3s guest images, guest control agent, and the single-vm spike`

### Task 4: noded group networks and pinned member addressing

**Why:** The subnet is the group's identity fabric: bridges, deterministic member addressing, and inter-group isolation must exist and be adoptable before member lifecycle can mean anything.

**Deliverables:**
- Group network manager: bridge-per-instance creation/teardown (`CreateGroupNetwork`/`DeleteGroupNetwork`), /24 validation against the values-declared composite supernet, the nftables inter-group forward deny (composite-to-composite and composite-to-serving), and attached-member refusal on delete.
- Deterministic addressing: member index to `.10 + i`, derived MAC, pinned tap name per `(group_instance_id, member_name)`; a rescan-able on-disk record of allocated networks (the adoption source of truth alongside the bridge list itself).
- Entry-path DNAT through noded's pod IP for the entry member's port, exactly the D-R3.11.4 lane (the node Envoy reaches `nodedPodIP:listenPort-mapped-port`).
- Inventory rescan on daemon start (the on-disk network records, group bundle dirs, and whatever bridges/taps actually exist) feeding the new `NodeStatus` fields. Bridges live in noded's pod netns and **die with the pod** (D-R3.11.4); the on-disk record is therefore the durable truth and `CreateGroupNetwork` is idempotent precisely so the control plane can re-issue it before any relight or adoption-time rebind (Task 7 sequences this).

**Specification:**
- Bridge and nftables setup are pure argv-batch functions (the `bridgeSetupArgs` pattern) so the exact commands are table-tested without root.
- Go tests: CIDR allocation/overlap refusal, address determinism (same inputs, same tap/MAC/IP), delete refusal while attached, nft rule generation, rescan round-trip.

**Acceptance:** CI green; on deployed noded, `CreateGroupNetwork` via grpcurl yields a visible bridge, and a second group's members cannot reach the first group's subnet (drilled with two throwaway VMs, documented in the PR).

**Commit:** `feat(embervm): per-group bridges, pinned member addressing, and inter-group isolation in noded`

### Task 5: noded member lifecycle with clock-resynced resume

**Why:** The member verbs make a group instance physically real: boot on the group bridge, bank to a set directory, resume into an identical world with a correct clock.

**Deliverables:**
- `StartGroupMember` FRESH (per-member writable rootfs from the image, session-class mechanics; tap pinned per Task 4; TCP health gate on `health_port`) and RELIGHT (recreate the tap world, resume the bundle, run the clock-resync handshake against the guest control agent, verify read-back within one second, fail the call otherwise with the delta in the error detail).
- `StopGroupMember` BANK (pause, snapshot under `group/<set_id>/<member>/`, destroy) and DESTROY; concurrent-stop refusal per vm_id.
- TCP health-probe loop per live member; results in `NodeStatus.group_member_vms`.
- A daemon restart kills live members (the standing single-node availability posture); rescan reports bundle sets and networks so the control plane resolves each group to relightable (complete set) or fresh-bootable.

**Specification:**
- Bank pauses members with no cross-VM barrier (standing decision 10); the pause loop is tight (sequential pause calls, then sequential snapshot writes) and the pause-spread is measured and logged per bank for the closure gate. The bank never races an in-flight guest-agent command: the agent channel is request/response with one in-flight command, and the daemon issues none between the pause decision and the snapshot.
- Go tests: fake-driver coverage for FRESH/RELIGHT flows, clock handshake success/failure/timeout, pinned-world reconstruction (same tap/MAC/IP on resume), bank-to-set-directory layout, destroy, probe reporting, rescan.

**Acceptance:** CI green; on deployed noded, a grpcurl FRESH boot of the Task 3 server image on a group bridge health-gates; a BANK then RELIGHT round-trips with the guest's clock verified within one second of the host's (documented in the PR).

**Commit:** `feat(embervm): group member lifecycle with pinned-world resume and clock resync`

---

## Phase 2: Composite core (group lifecycle and wake-on-connect)

### Task 6: GroupStore, GroupManager, and entry publication

**Why:** The control-plane brain: one process per group owning the ordered start/bank/relight state machine, one store projecting facts, one publisher extension making the entry reachable.

**Deliverables:**
- `Embervm.GroupStore`: ETS hot set (instance by workload, member states, set completeness) rebuilt from projections on boot; write-through transitions. Group FSM: `creating -> running -> banking -> banked -> relighting -> running ...` plus `fresh_booting`; `degraded` is a flag on `running`, not a state; terminal `expired, destroyed, failed`; illegal transitions raise.
- `Embervm.GroupManager` (one supervised process per live group instance): sequences `CreateGroupNetwork` (re-issued idempotently before any relight, since bridges die with the noded pod), role-ordered member starts (parallel within a `startOrder`, health-gated between orders), composes the `EMBER_GROUP_*` env (peer map from deterministic addresses, `EMBER_GROUP_SECRET` per decision 13), and drives bank/relight member loops. Member start failure during create tears the group down to `failed` (create is atomic; degradation only applies to already-running groups, decision 11).
- Set completeness is a derived fact recomputed on every NodeStatus sweep: a set missing any member's bundle is evicted eagerly (`group_set_evicted{partial_set}`) rather than discovered at wake.
- `EndpointPublisher` extension: composite workloads project listener/cluster `group|<workload>` for the life of the CR: the live entry member's `{ip via DNAT port}` when running, the activator otherwise (banked OR no instance at all, decision 8); the publisher stays the only sidecar writer.
- Chart wiring of the composite L4 range (the R4 Task 6 equivalents): the values-declared composite listener range (default 5410-5419) exposed as container ports on the node Envoy DaemonSet and as TCP ports on the `embervm-serving` Service (names `group-<port>`), plus the activator's composite port-range env so it physically binds every range port (D-R4.PR-4.1 mechanics). Rendered `helm template` verified in the PR.
- `GET /v1/groups/{workload}` management API: instance state, members with health, set id and completeness, subnet, published endpoint.

**Specification:**
- Secret sourcing: with `secretRef`, the control plane reads the referenced Secret key at each create (stable across instances by construction); without it, 32 bytes base64url minted at create and carried in the op-log create record. Members receive it only via the FRESH boot-time env seam.
- ExUnit: FSM exhaustive transitions, ordered-start property (order N never starts before all order N-1 healthy), create-failure teardown, publisher pure-function tests including the activator swap and the no-composite regression (snapshot unchanged from R4 shape), set-completeness derivation and eager eviction.

**Acceptance:** CI green.

**Commit:** `feat(embervm): group store, ordered group manager, and entry publication`

### Task 7: Wake-on-connect group relight and restart adoption

**Why:** The rung's headline verb: a kubectl connection to a sleeping cluster becomes one group relight, three clock-corrected resumes, and a working API session.

**Deliverables:**
- TCP activator reuse: the `group|` listener mapping joins the activator's port-to-workload resolution (D-R4.PR-4.1 mechanics); a connection parks, single-flights the wake through the GroupManager, publishes, splices. The wake is a **relight** when a complete set exists, and a full group **create** (network plus ordered fresh start) when no instance exists at all (decision 8): scale-to-zero from birth, TTL expiry, and forced roll all recover through the same connection path. Wake-rate limits and parked caps apply per the R4 values.
- Relight path: complete set required (decision 3); any member RELIGHT failure (including clock resync, decision 7) aborts the relight, evicts the set, and falls back to a fresh boot inside the same single-flight (`group_fresh_booted{reason}`), with the parked connection held across the fallback up to `wakeTimeoutSeconds`. The GroupManager re-issues `CreateGroupNetwork` before resuming members (bridges do not survive a noded pod restart).
- Restart adoption: boot/sweep reconciliation over `group_networks`, `group_member_vms`, `group_bundle_sets` rebinds live groups (respawning their GroupManagers in the adopted state), heals limbo, resolves daemon-restart casualties to `banked` (complete set) or fresh-bootable, evicts partial sets, re-derives and re-pushes the snapshot. A control-plane restart with a live group must republish the identical endpoint without touching any VM.

**Specification:**
- Single-flight correctness: N concurrent connections to a banked group produce exactly one relight sequence and N working sessions (property test with fakenode latency injection).
- ExUnit: miss round-trip against fakenode (park, ordered relight, splice, subsequent-connection-via-Envoy), relight-failure fallback to fresh with set eviction, adoption matrix (restart during each non-terminal state converges and republishes), degraded-group wake (a group with a dead member is live, not banked; connections route normally).

**Acceptance:** CI green; live drill in the PR: with a drill group banked, a TCP connect wakes it (timed park-to-first-byte), a second connection arrives via Envoy with the control-plane log silent, and `GET /v1/groups/{workload}` shows members live and the set consumed.

**Commit:** `feat(embervm): wake-on-connect group relight with all-or-nothing set fallback and adoption`

### Task 8: Idle-to-bank, TTL, forced roll, and degraded handling

**Why:** The economics: an idle cluster must cost one bundle set on disk, and every path back to running must be an honest relight or an honest fresh boot.

**Deliverables:**
- Stats scrape extension: the group entry listener's `downstream_cx_active`/`downstream_cx_total` join the R4 sweep; `group_stats` ops and `last_active_at` per standing decision 9.
- Idle-bank: zero active Envoy connections, a flat total, AND zero live activator splices across `idleBankSeconds` (decision 9) triggers unpublish (activator installed in the same update), a recheck of both counts, then the GroupManager's bank loop (pause all, snapshot all, atomic set record, destroy all; the on-disk network record persists while the instance does and the bridge is recreated idempotently at relight). A nonzero recheck aborts and republishes. A degraded group is excluded from banking (decision 11) and the exclusion is a logged, visible fact.
- Max-lifetime expiry: wait for zero active entry connections (values-capped patience, default 3600s) then destroy the group and its network; banked-TTL GC evicts stale sets and destroys the instance (warmth-only: an expired set IS the instance's end, unlike R4's volume).
- Forced roll: `DELETE /v1/groups/{workload}/instance` (management auth) destroys members, evicts the set, keeps the definition; the next connection fresh-boots a new environment on the current images. This is both the convergence lever and the degraded-recovery lever.

**Specification:**
- Scrape failure fails open for warmth (never bank on stale stats), exactly as R3/R4.
- ExUnit: idle detection with the degraded exclusion, abort-on-recheck, TTL patience, banked-TTL terminal semantics, forced roll, clock-injected throughout.

**Acceptance:** CI green; live: a drill group left idle past `idleBankSeconds` shows `banked` with zero live VMs and one complete set; the next connection relights it; a forced roll then makes the next connection fresh-boot.

**Commit:** `feat(embervm): group idle-to-bank, ttl, forced roll, and degraded handling`

---

## Phase 3: Operability, the consumer, and closure

### Task 9: Composite observability and alerts

**Why:** The new failure surfaces (set integrity, clock resync, bridge/subnet capacity, N-VM bank duration and disk pressure) must be visible before anything real depends on the class.

**Deliverables:**
- Control-plane spans: group lifecycle root spans (`create`, `bank`, `relight`, `fresh_boot`, `forced_roll`) with per-member child spans carrying `ember.member`, `ember.clock_delta_ms`, `ember.was_relight`; activator spans gain `ember.group` attributes; `ember.pause_spread_ms` recorded per bank (the decision 10 honesty number).
- Node Envoy group-listener TCP stats scraped to SigNoz alongside the R4 stats.
- SigNoz alerts (METRIC_BASED_ALERT seam, threshold-0 dry-run then restore): snapshot-disk watermark accounting for set size (a 3-member group banks roughly the sum of member memory sizes; the R2 disk alert threshold is revisited with that multiplier) and sustained group wake failures. Op-count alerts on `group_fresh_booted{clock_resync_failed|partial_set}` follow the honest R4 posture (D-R4.PR-10.1): no op-log-to-metrics bridge exists, so they ship as placeholders unless a span-attribute-derived signal covers them; the choice is recorded in DECISIONS.md during execution.
- `status.group` wired (Task 2), debounced.

**Specification:** The Task 11 gate numbers (relight latency, clock deltas, pause spread, isolation drills) must be derivable from spans plus Envoy stats plus the op-log alone.

**Acceptance:** CI green; spans and metrics visible in SigNoz from a live create/bank/relight cycle; dry-run alert reaches Discord.

**Commit:** `feat(embervm): composite observability, set-integrity alerts, and bank-size watermarks`

### Task 10: First consumer: scratch-k8s for the agent platform

**Why:** The rung's named consumer (Fork 4): a real, disposable, legitimately distributed Kubernetes environment for agent workflows, and the showcase the ADR promised.

**Deliverables:**
- A `scratch-k8s` Workload CR in the embervm chart: `class: composite`, members `server` (role server, startOrder 0, k3s-server image, healthPort 6443, 2 vCPU / 2048 MiB) and `agent` (role agent, startOrder 1, replicas 2, k3s-agent image, healthPort 10250, 1 vCPU / 1024 MiB), entry `{member: server, port: 6443, listenPort from the composite range}`, `{idleBankSeconds: 600, maxLifetimeSeconds: 604800, bankedTtlSeconds: 604800, wakeTimeoutSeconds: 180}`, images pinned by digest through the existing pipeline, sizing informed by the Task 3 spike numbers.
- Credentials via the 1Password Operator into a Kubernetes Secret named by `spec.group.secretRef` (standing decision 13), so the derived kubectl token is **stable across rolls, TTL expiries, and fresh boots**; consumer wiring behind a feature flag (the scratch-postgres pattern): a `SCRATCH_K8S_KUBECONFIG` surfaced to the monolith agent runtime via values (server `https://embervm-serving.<ns>.svc:<listenPort>`, token auth from that stable secret, TLS verification skipped and documented as scratch-tier in-cluster posture); first wiring exposes it to `run_python` session env so agent sessions can `kubectl`/client-go against it.
- The warmth-only contract stated where the consumer reads it: this cluster's state evaporates on roll/TTL/fresh-boot by design.
- Chart bumps per the bump rules in the same PR.

**Specification:**
- Latency table recorded: fresh boot (create-to-all-Ready), warm relight (connect-to-kubectl-response on a banked group), and the clock deltas observed per member.
- pytest for any monolith env-plumbing change (hand-registered py_test); no new RBAC (the kubeconfig points at the guest cluster, not ours).

**Acceptance:** CI green; an agent session (or a documented drill from the monolith pod) runs `kubectl get nodes` showing three Ready nodes, deploys a pod that schedules onto an agent member, lets the group bank, and a later `kubectl get pods` wakes the cluster and finds the pod again; the latency table is in the PR description.

**Commit:** `feat(embervm): scratch-k8s, a scale-to-zero kubernetes environment for the agent platform`

### Task 11: R5 gates and closure

**Specification (the gates, all measured, appended to this plan as a Closure section):**
1. **A real distributed cluster:** `scratch-k8s` fresh-boots to three Ready nodes with zero external egress during bring-up (host-side taps observed quiet beyond the group bridge); create-to-all-Ready recorded.
2. **Subnet isolation, drilled both ways:** members of one group reach each other on the group subnet; a second concurrently live group (a throwaway drill CR, a copy of the composite sample applied for the drill and removed after) cannot reach the first's subnet (connection attempts fail); neither group reaches the serving bridge.
3. **Stable identity across warmth:** member IPs and MACs identical before a bank and after a relight (op-log plus in-guest `ip addr` evidence); flannel host-gw routes still valid post-relight without agent rejoin.
4. **Group relight with correct time:** kubectl against a banked group wakes it; park-to-first-kubectl-response p95 <= 15s warm over 5 cycles; every member's post-resync clock delta <= 1s (span evidence); no TLS or lease errors in k3s logs across the resume.
5. **All-members-or-none:** deleting one member's bundle from a banked set makes the next wake fresh-boot with `group_fresh_booted{partial_set}` and the remnant set evicted; the op-log alone tells the story.
6. **Real node kills:** destroying one agent member VM of a live group drives the k8s node to NotReady and pods reschedule to the surviving agent; the group reports `degraded` naming the member; a forced roll recovers to three Ready nodes.
7. **Wake single-flight:** 10 concurrent TCP connects to a banked group produce exactly one relight sequence (op-log count) and 10 working sessions.
8. **Off-path and adoption:** with a kubectl watch open on a connection that arrived via Envoy (post-wake; an activator-spliced connection dies with the control plane by design), the control plane scaled to zero for 60s does not disturb it; on restore, adoption republishes an identical snapshot and respawns the GroupManager in the correct state; a noded restart resolves the group per set completeness and the next connection recovers it.
9. **Never-sever and idle honesty:** a group with one long-lived open entry connection is never banked across 2x `idleBankSeconds`; closing it banks the group on the next sweep; the banked group's disk cost equals one complete set and the bridge.
10. **Consumer soak:** scratch-k8s serves agent-platform traffic for 48h across at least 5 bank/wake cycles with zero composite-attributed errors and the latency table recorded.

**Deliverables:** Gate numbers in the Closure section; ADR embervm/001 roadmap row R5 to `Shipped <date>` with a shipped-shape paragraph, the R5 paragraph annotated where the shipped shape resolves a stated constraint differently (baked airgap images in place of the per-node pull-through OCI cache, which moves to the recorded follow-ons), and a composite row added to the ADR isolation table (tenant-trusted group, reuse within one group instance's own lineage, in-guest root documented per Task 3); this plan's out-of-scope list carried into the R6 planning seed.

**Commit:** `docs(embervm): R5 closure with gate evidence`

---

## Explicitly out of scope for R5 (recorded, not dropped)

- **Stamped ephemeral instances (N clusters from one definition):** v1 is one instance per CR, the R4 singleton discipline. Per-session/per-branch stamping needs an instance API, per-instance quota, and naming; it is the natural R5.x once a consumer demands more than one concurrent environment.
- **Cross-node groups:** the whole group places on one node (the fleet is one node today). A cross-node group subnet is an overlay problem that arrives with the multi-node fleet, not before.
- **Member auto-heal:** decision 11; a dead member is a reported degradation, recovery is k8s-level or a forced roll. Supervised member restart is the follow-on once a consumer needs group HA.
- **Durable group members (a member owning an R4 volume):** the bridge from warmth-only groups to real DB clusters, and with it the CNPG-shaped clustered stateful (per-VM volumes, EmberVM-supervised election through the `ra` tier, rw/ro xDS flips) carried from the R4 seed. Its own plan when a durable-cluster consumer exists.
- **Per-node pull-through OCI cache (the agents/041 generalization):** required the day guest clusters run arbitrary user images; the airgap lane covers the showcase (decision 12).
- **Guest-cluster ingress (exposing workloads running inside the guest k8s):** v1 exposes exactly one entry endpoint (the API). Routing into guest-cluster NodePorts is a follow-on with its own auth story.
- **Cross-VM consistent group snapshots:** explicitly never promised (standing decision 10); applications recover by their own crash semantics.
- **Continuous guest time sync (PTP/phc2sys):** Fork 2 Option B, the upgrade path if inter-resync drift ever matters.
- **Group-level fairness or per-principal group quotas beyond the existing wake-rate limits:** groups are few and singleton in v1.
- **TLS verification on the consumer kubeconfig:** scratch-tier in-cluster posture, documented; revisit with the mesh migration.

## Open risks tracked for execution

| Risk | Watch signal | Fallback |
| ---- | ------------ | -------- |
| k3s does not run (or runs badly) on the kata guest kernel | Task 3 spike, run FIRST as its own PR | Patch guest kernel config via the established patch seam; if the kernel is unpatchable, kubeadm-free alternatives (k0s) before any distro surgery; the spike gates the rung before group machinery is built |
| Firecracker resume with a tap NIC breaks when the host tap world differs subtly (name, MAC, index) | Task 5 bank/relight round-trip drill | Pinning is total (name+MAC+IP); if resume still proves unreliable, v1 degrades to fresh-boot-only wakes (warmth deferred, decision 3's fallback made default) while keeping the set metadata |
| Clock resync lands but k3s/etcd still objects to the gap (lease expiry storms on resume) | Task 10 relight drill; k3s logs across resume | Lengthen tolerable gap by tuning k3s lease durations in the guest image; worst case bank is disabled for the consumer (fresh-boot-only) until PTP (Fork 2 B) |
| Group bank size (sum of member memory) pressures node NVMe | Task 9 watermark; set sizes in NodeStatus | Banked-TTL is terminal for groups (no volume floor); cap concurrent composite CRs; diff snapshots against pristine bases are the recorded compression lane (ADR 001) |
| Bank pause-spread wide enough that k8s state is inconsistent beyond recovery | `ember.pause_spread_ms` per bank; gate 4 | Crash consistency per VM is the contract (decision 10); k8s is built to survive it; if spread proves problematic, pause order server-last narrows the etcd-vs-kubelet skew |
| Airgap tarball balloons image size and first-boot import time | Task 3 spike numbers | Trim to the minimal k3s image set; import happens once per fresh boot, and relight skips it entirely (warmth pays it back) |
| Three concurrent member VMs exceed node-4 headroom alongside existing classes | `max_live_vms` accounting; capacity ops | Sizing set from the spike; agents shrink to 1 GiB floors; the group is singleton so worst case is one cluster's footprint |
| nftables inter-group rules interact with the existing serving DNAT chain | Task 4 isolation drill with two groups | Rules are table-tested argv batches; the deny chain is separate from `serving_dnat`; drill before the core lands |
| The static-token kubeconfig auth path weakens the entry surface | Consumer docs; gate 10 soak | Cluster-internal exposure only (R4 decision 10 inherited); the token derives from the per-instance group secret and dies with the instance; per-session endpoint tokens (ADR 001) are the recorded upgrade |

---

## R6 planning seed (carried forward at closure)

R6 Facade (virtual control planes, the kine-style etcd shim over the op-log) requires its own ADR before commitment. The R5 out-of-scope items most likely to be consumed first: stamped ephemeral instances, durable group members (the clustered-stateful bridge), and guest-cluster ingress.

---

## Closure

**Status: code Shipped 2026-07-17, live gates PENDING.** All eleven tasks landed and deployed across eight PRs (contract #3610, k3s guest substrate #3612, noded groups #3614, composite core #3616, economics #3617, observability #3618, scratch-k8s #3619, this closure), each two-stage reviewed (spec then quality) on green CI; the embervm chart is live at 0.1.88 with the control plane, noded, and serving Envoy running the composite code. ADR embervm/001 marks R5 `Shipped 2026-07-17 (gates live-pending)`, matching the R4 posture. The ten gates below are the empirical acceptance; they require live boots on the Firecracker node and were deliberately deferred (they mutate cluster state: booting k3s clusters, killing member VMs). Each is instrumented so its number is derivable from spans plus Envoy stats plus the op-log alone (Task 9). Fill each `TODO` from a drill run against the live cluster, then flip the ADR row to `Shipped` without the live-pending qualifier if all pass.

| # | Gate | Evidence to record | Result |
| - | ---- | ------------------ | ------ |
| 1 | Real distributed cluster | `scratch-k8s` create-to-all-Ready time; host taps quiet beyond the group bridge during bring-up (zero external egress) | TODO |
| 2 | Subnet isolation both ways | members of group A reach each other; a second live drill group cannot reach A's subnet; neither reaches the serving bridge | TODO |
| 3 | Stable identity across warmth | member IPs/MACs identical pre-bank and post-relight (op-log + in-guest `ip addr`); flannel host-gw routes valid post-relight without rejoin | TODO |
| 4 | Relight with correct time | park-to-first-kubectl-response p95 <= 15s warm over 5 cycles; per-member post-resync clock delta <= 1s (span/verdict evidence); no TLS/lease errors across resume | TODO |
| 5 | All-members-or-none | delete one member's bundle from a banked set: next wake fresh-boots with `group_fresh_booted{partial_set}` and the remnant set evicted; op-log tells the story | TODO |
| 6 | Real node kills | destroy one agent member VM: k8s node NotReady, pods reschedule to the surviving agent, group reports `degraded` naming the member, a forced roll recovers to three Ready | TODO |
| 7 | Wake single-flight | 10 concurrent TCP connects to a banked group produce exactly one relight sequence (op-log count) and 10 working sessions | TODO |
| 8 | Off-path and adoption | Envoy-arrived kubectl watch survives a 60s control-plane scale-to-zero; on restore adoption republishes an identical snapshot and respawns the GroupManager in the correct state; a noded restart resolves the group per set completeness | TODO |
| 9 | Never-sever and idle honesty | a group with one long-lived open entry connection is never banked across 2x `idleBankSeconds`; closing it banks on the next sweep; banked disk cost equals one complete set plus the bridge | TODO |
| 10 | Consumer soak | scratch-k8s serves agent-platform traffic 48h across >= 5 bank/wake cycles with zero composite-attributed errors; latency table (fresh boot, warm relight, per-member clock deltas) recorded | TODO |

**Live-drill runbook:** `projects/embervm/runtimes/k3s/drill/` (the Task 3 single-VM spike numbers and the scratch-k8s latency table also land there). One observability item carries into the drill: the OTel ctx-across-Task span nesting (Task 9) is verified only in SigNoz (CI runs `traces_exporter: :none`), so gate 4's per-member clock-delta spans must be confirmed to nest under the group relight root during the drill.

**Known shipped-shape gaps to note when the gates run:**
- `ember.clock_delta_ms` is a `-1` sentinel: noded's `StartGroupMemberResponse` returns only `{vm_id, ip, was_relight}`, not the measured delta, so gate 4's `<= 1s` evidence is currently the boolean verdict (a successful verified relight implies noded checked the delta), not the number. Echoing the measured delta from noded is a small additive-proto follow-on if the gate wants the value.
- The composite bank-size/disk-watermark and fresh-boot op-count alerts ship as documented placeholders (no op-log-to-metrics or spanmetrics bridge in-cluster; the reasons are on the `fresh_boot` span). See DECISIONS.md D-R5.PR-6.1.
