# EmberVM Architecture

A single, self-contained description of how EmberVM works: the current
state and the decided future of one system, readable end to end.

This document presents the complete decided vision, not only what runs
today, so every section reads as one coherent design. Three kinds of
statement appear, and anything not yet live is flagged inline where it is
claimed, never implied:

- **Built**: live behaviour, shipped code.
- **Decided direction** (**Planned** in tables): designed and agreed but
  not yet implemented or only partially landed; scheduled work carries its
  tracking issue.
- **Accepted risk**: an eyes-open trade, stated where it applies.

An unflagged claim means live behaviour; if reality and this document
disagree, one of them is a bug to fix.

---

## 1. What EmberVM is

Self-hosted Firecracker orchestration: **a private Lambda equivalent on
metal you own**. An organization sizes (or elastically bounds) a Firecracker
nodepool; EmberVM provides placement, fairness, isolation, metering, and
lifecycle so internal workloads get Lambda-shaped submit / scale-to-zero /
warm-serve behaviour without a hosted FaaS product, without a Kubernetes
object per invocation, and without etcd in the execution path.

EmberVM deploys on any Kubernetes with KVM-capable nodes, a local scratch
disk per Firecracker node, and an S3-compatible object store; section 11
states the platform contract. The reference deployment in this monorepo is
a small on-prem cluster whose concrete shape lives in
[deploy/README.md](deploy/README.md).

**Goals**: private Lambda ergonomics (HTTP invoke, zip or image source,
caps, quotas, internal chargeback); honest scheduling on finite capacity;
isolation by default; the control plane off the hit path; one operable
component (Elixir control plane + Go node daemon + `Workload` CRD, managed
by kubectl/Helm/ArgoCD).

**Non-goals**: a hosted multi-tenant cloud; "agent platform as the product"
(agents are dogfood consumers); every workload class as an equal pillar
(task, serving, session are the core; stateful and composite are optional
advanced classes); pretending capacity is infinite (queue depth, saturation
signals, and admission control are the product surface instead).

EmberVM keeps workload *definitions* in Kubernetes (low churn) and
execution state in its own op-log and memory (high churn). The split is the
point: per-job orchestration (an etcd object per job, a pod per step)
prices out thousands of short tasks, and nothing pod-shaped offers
millisecond warm restore or wake-on-connect at all, so the low-latency
classes have no incumbent to compare against.

---

## 2. System overview

### Vocabulary

- **brick**: a fixed-size noded Deployment pod and the unit of VM capacity.
- **noded**: the Go daemon on each brick that owns Firecracker and
  node-local I/O.
- **op-log**: the durable ordered journal of lifecycle and enforcement
  actions.
- **bank / relight**: suspend a VM into a warm snapshot, then restore that
  snapshot.
- **primed / banked / cold / running**: ready pristine, snapshotted,
  fresh-boot, or actively executing instance states. Cold and warm describe
  boot mechanics only; task isolation remains no reuse, including after
  snapshot restore.
- **generation**: the volume incarnation number used to prove state
  provenance and pair a stateful volume with a memory snapshot.
- **principal**: the identity boundary a workload runs as.
- **dial-home**: noded registration and status sent to the control plane.
- **warmth**: a reusable boot artifact, such as a base or banked snapshot,
  that can make the next start faster but is never correctness-critical.

The diagrams use vsock (Firecracker's host-guest socket), DNAT (destination
network address translation), xDS (the Envoy configuration protocol), BEAM
(the Erlang virtual machine), ETS (Erlang Term Storage), and CNPG
(CloudNativePG).

```mermaid
graph TB
    subgraph cp ["Control plane (Elixir/OTP, 'ember')"]
        API["HTTP API<br/>/v1/workloads /v1/usage"]
        DISP["Dispatcher + class managers<br/>(session/serving/stateful/group)"]
        POOL["PoolManager<br/>primed-pool refill"]
        XDS["EndpointPublisher<br/>sole xDS writer"]
        OPLOG[("op-log<br/>Postgres (default),<br/>SQLite-WAL fallback")]
        ETS[("ETS hot set<br/>rebuilt on start")]
    end

    subgraph node ["Firecracker node (brick)"]
        NODED["noded (Go)<br/>Firecracker driver, vsock,<br/>tap/DNAT, volumes, activator"]
        ENVOY["node Envoy<br/>(serving relay)"]
        VM1["task / session VM<br/>vsock only, no NIC"]
        VM2["serving VM<br/>tap NIC"]
        VOL[("vol.img on node NVMe<br/>stateful, authoritative")]
        SCRATCH[("/var/lib/embervm/scratch<br/>warmth cache: bases,<br/>banked snapshots")]
    end

    S3[("S3-compatible object store<br/>artifact export / archive")]
    K8S["Kubernetes<br/>Workload CRD (definitions),<br/>brick pods, PriorityClasses"]

    CALLER["task/session caller"] --> API
    API --> DISP -->|gRPC| NODED
    NODED -->|vsock| VM1
    EDGE["Gateway API HTTPRoute"] --> ENVOY
    XDS --> ENVOY
    ENVOY -->|DNAT into tap| VM2
    NODED ==>|dial-home NodeStatus:<br/>authoritative state| DISP
    NODED <-->|Export/Restore/Evict| S3
    NODED --- VOL & SCRATCH
    K8S -.->|watch definitions| API
```

**Division of labour** (each responsibility placed where it is cheapest to
make correct):

| Component | Owns | Never does |
| --------- | ---- | ---------- |
| Control plane (Elixir) | Coordination facts: queue, placement, fairness, backpressure, lifecycle decisions, xDS publication, op-log appends | Carry steady-state payloads; sit on the serving hit path |
| noded (Go, one per brick) | Payload and OS work: Firecracker process supervision, vsock relay, tap/DNAT, volume attach, snapshot byte movement, node-local wake | Make fleet-wide decisions; join the BEAM cluster (it speaks a narrow gRPC surface instead) |
| Envoy (edge + node tier) | The serving request path, health-based ejection, rate limits | Lifecycle |
| Kubernetes | Low-churn `Workload` definitions, brick pod scheduling, PriorityClasses | Per-invocation objects, execution state |
| Object store (S3-compatible) | Artifact export/archive, off-node durability | Anything on a hot path (read only on deliberate restore or local miss) |

Bricks **dial home**: on start and on a jittered interval each noded POSTs
its identity `{node, pod_uid, address, boot_id}` to `/v1/nodes/register`;
the control plane adopts it keyed by `(node, pod_uid)` and streams
`WatchNode`. The control plane never lists-and-watches daemon pods. Growing
the fleet is labelling a node (`homelab.io/firecracker=true`), not a values
edit.

### How one invocation works

**A task (the all-miss case):**

1. A caller submits `POST /v1/workloads`.
2. The dispatcher assigns a primed VM from the matching pool.
3. It sends gRPC to noded on the chosen brick; the payload enters the VM
   over vsock, the result comes back, the VM is destroyed after one task,
   and the op-log records the action.

**A serving hit (the steady state):**

1. An edge HTTPRoute reaches node-local Envoy, which the control plane
   already programmed over xDS.
2. Kernel DNAT carries the request into the serving VM's tap NIC. The
   control plane is not involved.

**A wake (a serving or stateful miss):**

1. The request reaches the fallback activator endpoint and parks.
2. A single-flighted wake fires, the real endpoint is published, and bytes
   splice through to it.

The task and wake paths are the only ones on which the control plane sits.
That is the hit/miss invariant stated in section 5.

---

## 3. Workload classes

Definitions are Kubernetes `Workload` CRs (schema-validated, GitOps-synced);
`kubectl get workloads` reads back `snapshotRef`, `observedGeneration`, and
readiness. `source` is a oneOf ladder: `image` (bring an OCI image, no SDK;
contract is "listen on the declared port, answer a health path") and `zip`
(runtime base + Lambda-compatible `handler(event, context)` shim; archives
are fetched and unpacked inside the disposable guest).

| Class | Semantics | Network | State | First consumer |
| ----- | --------- | ------- | ----- | -------------- |
| **task** | Fresh VM per invocation from a pristine base, destroyed after one task. Dispatch is assignment-only from a primed pool | vsock only, no NIC | none | scan fleet (semgrep), zip functions |
| **session** | Bank/relight sandbox: idle snapshot to disk, restore on next invoke, principal-bound lineage | vsock only, no NIC | memory snapshot (+ workspace tier) | agent sandboxes |
| **serving** | Long-lived warm HTTP endpoint; Envoy routes hits, CP only on miss/wake | tap NIC | none durable | tenant web APIs, og-image |
| **stateful** | Scale-to-zero singleton datastore; L4 wake-on-connect; volume owns data, snapshot owns warmth | L4 via node Envoy | `vol.img` on node NVMe (authoritative) | demo-postgres |
| **composite** | Multi-VM group, private per-group /24, all-or-none bundle-set bank/relight; warmth only, no member volumes | per-group bridge | group snapshot set | no live consumer |

**Decided direction, not yet built:**

- **Isolated high-throughput lane**: Envoy routes straight to
  per-brick listeners, and each brick pops a fresh VM per request.
- **Persistence as a declared property**: workloads declare
  persistence flags; memory and filesystem persistence decouple from class.
- **Composition**: multi-component apps
  become independent Workloads wired by bindings rather than composite
  groups.

---

## 4. Lifecycle

### Stateful bank/relight and the interruptible bank

```mermaid
stateDiagram-v2
    [*] --> serving: wake (cold boot or relight)
    serving --> banking: idle window elapses, 0 active connections
    banking --> banked: atomic bank (pause, snapshot, destroy)
    banking --> checkpointed: interruptible bank opt-in:<br/>pause + snapshot to temp
    checkpointed --> serving: connection parked -> ABORT<br/>(bless gen+1, delete temp, resume: HOT)
    checkpointed --> banked: nobody waiting -> COMMIT<br/>(publish bundle, destroy)
    banked --> serving: wake -> relight<br/>(only if bundle_generation == volume_generation)
    banked --> serving: pairing mismatch -> cold boot from volume<br/>(slower, never incorrect)
```

Facts that make this safe:

- **Bank only starts at zero active connections**; a long-lived connection
  is never severed by an idle bank (rolls are the exception, bounded by the
  two-minute preemption contract).
- **Resume requires an exact (memory snapshot, volume generation) pair**;
  mismatch discards warmth and cold-boots from the durable volume.
- **The interruptible bank** (`spec.stateful.interruptibleBank: true`, off
  by default) makes steady-state wakes always hot or warm, never cold. The
  three cold exceptions: genuine first boot, explicit operator reset, and
  the max-lifetime forced roll.
- **Only a snapshot taken immediately before teardown may be kept.** Any
  snapshot whose VM resumes is discarded (the abort orders bless-generation,
  delete-temp, resume, so a surviving temp is always refused by pairing).

### Generation blessing and quarantine

The control plane is the adjudicator of volume generations, with exactly
three legitimate issuance shapes:

1. **CP-issued pre-dispatch**: blessed durably before a wake/attach
   (op-log-before-dispatch). The default.
2. **Checkpoint-abort auto-heal**: noded's resolve-timeout
   auto-abort self-bumps by exactly +1 on the same `vm_id`. A durable
   `checkpoint_dispatched{workload, vm_id, generation}` record lets a
   restarted CP prove the +1 was its own checkpoint and bless it instead of
   quarantining. Anything unproven stays quarantined; the break-glass
   runbook is `docs/runbooks/embervm-stateful-generation-quarantine.md`.
3. **Delegated advancement**: a durable, bounded
   `wake_grant{workload, volume_node, gen_floor, gen_ceiling, expires_at}`
   authorises the volume's anchor brick to advance the generation while the
   CP is away (gap budget, default k=4; a time-bounded class for
   sub-second-banking demos). **Decided direction**: the north star is a
   steady-state lease with brick-owned idle-bank.

An advancement no grant covers is quarantined on sight. The grant changes
who may *issue* a generation, never who may *write* a volume (invariant 6).

### Wake path and the node-local activator

A request to a scaled-to-zero workload lands on a fallback endpoint, parks,
and triggers a single-flighted wake; the real endpoint is then published and
bytes splice. The activator (L7 serving, L4 stateful/composite) belongs in
noded rather than the CP pod so that a CP `Recreate` roll cannot black-hole
cold wakes: stable DNAT from the node IP, `NodeStatus` advertises the
activator endpoint, `EndpointPublisher` renders it as the fallback, and
instance ids minted node-side carry `origin: ACTIVATOR` for the CP to adopt
and backfill on reconcile. **Planned**: partially landed, soak ongoing.

### Sessions: the durability ladder

| Tier | Window | Artifact | Pinning |
| ---- | ------ | -------- | ------- |
| Live | 6h continuous ceiling (`maxLifetimeSeconds`) | running VM | node-resident |
| Warm bank | 7 days from last bank | memory snapshot in S3 | CPU-vendor + base-generation |
| Durable workspace | 7 days from last use | zstd content-addressed file set | none |

Resume is one interface with four verbs: cold boot; base-snapshot restore;
warm (memory) restore; base + workspace hydration. The CP picks the cheapest
unexpired artifact; the session contract is instant for 6h, restorable for
7 days. **Decided direction**: capture decouples from bank
(close-triggered for no-memory-snapshot workloads), retention becomes
`latest + N`, and the workspace size cap becomes a declared soft budget.

**The 6h ceiling is a version-convergence bound, not a data lifetime.**
It exists so a session cannot ride a stale base image forever, since a
session pinned to an old base keeps that base's registry entry live and
blocks the retention sweep from reclaiming it. It is deliberately not raised
to buy continuity. Continuity comes from **adoption** instead: lineage is
decoupled from session generation, so `session_id == lineage_id` holds only
for the first generation, and a later generation inherits the prior
lineage's workspace rather than starting blank. That is why a lineage spans
weeks of shorter runs even though no single session may.

**Parked sessions count as disk, not against `concurrency.cap`.**
A session with `memory: false, filesystem: true` persistence parks to
its workspace volume holding zero RAM, so counting it as live would let
idle sessions starve the cap while nothing runs. `concurrency.cap` bounds
concurrently running VMs only, and wake does not re-check it: placement's
memory admission and the per-principal wake-rate limit are what protect the
receiving node.

The S3 artifact GC uses an 8-hour TTL for stateful warmth and 7-day TTLs for
session memory, serving snapshots, session workspaces, and group sets.

---

## 5. The invariants

These are the rules everything else rests on. Every design change is judged
against them.

1. **The hit/miss invariant.** The control plane is on the invocation path
   if and only if the invocation requires a lifecycle action (create,
   restore, wake). Steady-state serving requests go Envoy to VM and never
   traverse the control plane. Task execution is the degenerate all-miss
   case (fresh VM per task by policy), so the dispatcher is on that path by
   definition, bounded by fleet restore capacity rather than by BEAM.

2. **Facts through the control plane, payloads never.** Snapshot bytes move
   node-to-store via noded; serving traffic moves through Envoy; the control
   plane carries facts. The two deliberate exceptions are lifecycle-rate:
   the parked first request after a serving miss, and task dispatch/results.

3. **No VM or snapshot lineage ever crosses a principal.** Content-addressed
   dedup across principals is forbidden (erasure would become a cross-tenant
   reference-counting problem). The workload-class table in section 3
   carries the per-class network and state boundaries.

4. **Fail closed on enforcement, fail open on warmth, and metering is not
   enforcement.** Containment (concurrency caps, fair queues, admission, the
   node-side pressure predicate, node-confirmed destruction) fails closed.
   Warmth (a missing snapshot, an unreachable store) fails open to a cold
   boot: slower, never incorrect. Metering is counting, not enforcement, and
   **fails open by design**: a brick out of contact
   keeps running and keeps counting, and unreconciled spend is written off.
   Cutting off a principal is an admission action (stop minting tokens, 402
   at the edge), not a metering one.

5. **The node agent is authoritative for instance runtime state.**
   What a brick reports over
   dial-home about its VMs, taps, and volumes is the truth; control-plane
   tables are a reconciled cache. The one carve-out is destruction: an
   instance is recorded destroyed only after the owning node confirms
   teardown, and reconciliation is fail-closed toward destruction (an
   unrecognised node VM is an orphan to destroy, unless it carries the
   `origin: ACTIVATOR` marker, in which case it is adopted and backfilled).

6. **Single-writer for stateful is a physical fact, not a protocol.** The
   storage section explains the raw sparse file, one-writable-attach rule,
   and generation-as-provenance detail; failover remains a deliberate
   operator action and the partition window is bounded by the brick silence
   timeout.

7. **Durable-before-observed for the journal.** Every lifecycle and
   enforcement action is an ordered op-log append; callers await their
   (group-committed) batch. The op-log doubles as the audit record.

8. **Kubernetes arbitrates pods; EmberVM arbitrates VMs; the pod is the
   ABI.** No scheduler or autoscaler code is imported and no node
   provisioning is rebuilt. The capacity section's three-layer table shows
   the levers, and a Pending brick is the scale-up signal or the fleet-full
   page.

9. **Classes are reuse semantics; substrates are lanes.** New execution
   technologies (Hyperlight, wasm) enter as lanes under existing classes,
   never as new classes.

10. **Users express posture, never mechanics.** The surface is a small set
    of declared scalars in user-facing units, each under a platform ceiling
    with a non-escalation rule. Priority rungs, disruption budgets, grace
    periods, and CPU are platform-derived; users do not select those
    mechanics. The scalar details are carried by the storage and
    control-plane sections.

---

## 6. Control plane internals

- **State model**: hot working set in ETS (rebuilt on start, healed by
  adoption from node reports); durable book-of-record in the op-log behind
  the `Embervm.OpLog` behaviour. Postgres is the default backend (CNPG in
  the reference deployment); SQLite-WAL is the zero-dependency single-node
  fallback, and
  `Embervm.Application.op_log_mod/0` selects between them purely on
  `EMBERVM_OPLOG_DSN` being set, so the pod spec names the live backend.
  Either adapter creates its own schema on boot, so there is nothing to
  migrate. The dispatch path never reads the durable store.
- **Retention**: result TTLs enforced at read time; terminal tasks
  pruned past 7 days; the ops journal prefix-compacted past a 30-day horizon
  behind a durable `compacted_through_seq` marker; PVC usage alerted at 80%.
  Long-horizon audit lives in the observability stack.
- **Adoption**: noded reports `primed_vm_ids`, session VMs,
  checkpoint-pending VMs, and banked artifacts on every `NodeStatus`; the
  dispatcher and managers reconcile on boot and every sweep. This is the
  standing fix for the restart-wedge bug class, and the protocols are
  model-checked: three PlusCal specs in `projects/embervm/specs/`
  (`adoption`, `bank_relight`, `quota`) run under TLC in the build, so a
  spec violation is a red build rather than a report, and the vocabulary
  guard (`vocabulary.exs`) keeps the specs honest against the code.
  **Planned**: trace validation, op-log events checked against TLA+
  actions.
- **Cells**: the unit of horizontal scale is a cell, a complete
  single-writer control plane owning a bounded set of bricks and workloads,
  with one op-log appender (ordering is within-cell only). The seams exist
  now (`cell_id` in the schema, workload-to-cell assignment as data,
  per-cell dial-home address), with exactly one cell (`cell-0`) today. A
  thin stateless fleet layer (route + capacity roll-up) arrives only with a
  second cell.
- **Registry survives restarts**: noded persists its last-synced registry to
  NVMe marked stale; a restarting noded with an absent CP serves warm
  workloads from cache. No dependency's brief absence may turn a warm node
  into a dead one.

**Decided direction:**

- **Op-log restructuring**: payloads separate from facts and `ops` are
  time-partitioned, making principal-scoped erasure an indexed delete.
- **Admission-only control plane**: the CP admits and mints encrypted
  session-routing tokens; peers redistribute under pressure and metering
  fails open.
- **Resource model**: `memMib` is the only declared dial, CPU is derived
  from it, and GB-seconds is the accounting unit.

---

## 7. Capacity, bricks, and Kubernetes

**A brick is the capacity unit everywhere**: a fixed-size noded
Deployment pod in a T-shirt size class (`2gi` through `16gi`; small classes
pack tasks densely, the largest hold 1-2 serving/session VMs), with honest
Guaranteed requests, sized roughly 4-8x the largest VM of its class, a
handful per node. The daemon is budget-agnostic: it reads its ceiling from
its own cgroup, so a size class is a resources block, not a code fork, and
a brick's size is fixed for its lifetime.

| Layer | Owner | EmberVM's lever |
| ----- | ----- | --------------- |
| VM to brick slot | EmberVM control plane | per-brick contiguous-headroom ledger; pack-to-empty scoring; class-exact placement (no cross-class borrowing) |
| Brick pod to node | kube-scheduler | pod shape only |
| Node provisioning | Karpenter or equivalent (cloud) / none (fixed fleet) | brick count vector from the single-writer controller; a Pending brick is the signal |

On a fixed fleet a Pending brick **is** the fleet-full signal: the
controller flags `:fleet_full`, the dispatcher refuses placement (503), and
a human is paged, rather than overcommitting.

Priority projects onto three axes: PriorityClass ranks brick pools by lane
(occupied-capable bricks always run at default non-preempting priority;
sacrificial low-priority balloon bricks are the burst headroom); QoS is
always Guaranteed; per-workload arbitration happens only in CP dispatch.
Disruption splits workloads into preemptible posture (task, isolated lane)
and durable posture (session, stateful: continuity via banked-state
durability, not node pinning). Remaining node lifetime is a placement input;
a terminating node is a placement target for work that fits its horizon.
Karpenter behaviour is drilled against kwok in CI.

**Brick discovery and scale**: bricks are the only source of node capacity,
so the control plane picks the brick mix from placement demand rather than
running one fixed daemon per node; discovery is dial-home, never a Service
or a per-node DaemonSet. Desired per-class counts are a values knob
reconciled by `Embervm.BrickController` through `/scale`, because ArgoCD
ignores `/spec/replicas` fleet-wide and git-declared replicas would not
sync; instance identity is the kubelet pod UID. Brick autoscale runs at
rung `up` on the `observe -> up -> full` ladder: denial-driven scale-up
acts, clamped to the chart's maxReplicas. **Planned**: promoting to `full`
adds drain-aware scale-down. The live brick mix is deployment state and
lives in the fleet section.

---

## 8. Storage, artifacts, durability

**Artifact model**: one typed verb family,
`ExportArtifact` / `RestoreArtifact` / `EvictArtifact`, over `ArtifactRef
{kind: BASE | SESSION | SERVING | STATEFUL | GROUP_SET | VOLUME}`.
Control-plane-driven, idempotent per key; evict refuses while referenced.
Keys are namespaced by workload (and vendor, below); the principal-scoped
`shared/<principal>/<sha256>` keyspace is a deliberate, named exception.

**Vendor pinning**: Firecracker memory snapshots restore only within a CPU
vendor (and a narrow intra-vendor matrix), so all warmth artifacts are keyed
by `(vendor, template)` and never cross the boundary; the daemon refuses a
vendor-mismatched restore loudly. Volume data is fully portable. Legacy
artifacts cut before stamping existed are grandfathered: restorable on their
home node forever, never distributed.

**Decided direction:**

- **Local disk is authoritative**: local node NVMe relight needs no network.
- **S3 is a zstd content-addressed archive**, written at bank commit, not a
  hot tier.
- **`archiveInterval`** is the user-facing durability control in
  acceptable-loss units.
- **Failover and node rotation** are deliberate planned-drain operator
  actions.

**Ownership arbitration is class-scoped** (decided direction):

| Class | Exclusion | Two-incarnation cost | Mechanism |
| ----- | --------- | -------------------- | --------- |
| stateful | physical (one node, one writable attach) | cannot arise implicitly | none needed; grants are provenance |
| session | none exists | divergence from a common ancestor | durable relinquish record before handoff; divergence bounded, detected by generation comparison on reconnect |
| composite | none (warmth-only) | bundle-set divergence | inherits session rules, governed as one unit |
| serving / task | n/a | nothing durable | n/a |

The divergence bound is the **brick silence timeout**: a brick that has not
heard from the control plane for longer than the timeout (~6h, in the
grant-expiry range, so a CP roll never trips it) stops serving
everything it holds. Token TTL is a convenience, never the correctness
parameter.

---

## 9. Identity, tenancy, security

Only `principal` and `domain` ship now. A domain is contained in exactly one
principal, so a same-domain-by-default binding can never cross the isolation
boundary. Shared platform definitions (such as the sandbox-session and
scan-fleet templates) are owned by a reserved `platform` principal with an
explicit broad instantiation grant, the widest and most-reviewed grant in
the system. The op-log's existing `tenant` field is a deployment constant
occupying the Account slot.

**Decided direction:**

**Hierarchy**:

```text
Account      billing / grouping, NO isolation semantics
 └ Product   grouping, NO isolation semantics
    └ Principal   THE isolation boundary
       └ Domain   env or grouping within exactly one principal
          └ Workload
```

**Definitions at scale**: one product template plus N enrollment
records, one Git CR per product expanded into CP definitions, and idempotent
registration as a desired-set reconcile.

**Credential handling**: material may sit where
it can be stolen only if the platform can kill its validity on demand;
otherwise the request moves to the credential. Secrets are classed:
derivable short-lived (class 1, may enter trusted guests, revoked at bank),
fixed-but-rotatable (class 2, brick-lease only, injected at the egress hop),
fixed-manual (class 3, never leaves a central key-sharded swap tier). A
guest holds no credential material: the brick-local proxy on every guest
egress reads the plaintext request, sets the configured header to the real
value (mounted only in the sidecar), and originates a fresh verified TLS
connection onward. Injection fires only when the destination is in that
secret's `egressTo`, so the credential is unreachable at every other host
(`projects/firecracker/substrate/egress-proxy/cmd/swap.go`). Header
injection is used rather than placeholder substitution because a
guest-controlled placeholder can be spliced into a URL and reflect the
credential into a request line. RAM scrubbing before snapshot is rejected
as a mechanism to rely on; revocation at the validator is the control.

**Public surface hardening** (as shipped): the public routes are scoped at their
HTTPRoutes, with serving routes additionally constrained by node Envoy authority
matches and the guest shim's reserved `/shim/` prefix. The pattern's proof
is a public Bazel-query demo that serves each visitor query from a
disposable CoW clone of a warm-Skyframe snapshot: server-controlled argv,
zero egress, reaped per request.

**Guest identity**: a guest never holds a cluster credential (as
shipped, by construction: no NIC, no mounted ServiceAccount); the
platform holds real credentials and acts on the guest's behalf through
the brokered egress path. **Decided direction**: an audience-scoped
projected guest token (audience `embervm`), not yet shipped.

**Decided direction:** GitHub leaves the agent egress
catalog. Host-keyed injection bounds which host a credential reaches, never
which request reaches it, so a prompt-injected guest can shape any GitHub API
call and have the token attached to it. Agent principals instead reach GitHub
through MCP tools whose URLs name the target repo, entitled per
identity-provider group and backed by a per-group fine-grained PAT, so what a
guest can reach is a fixed tool set rather than an API surface. The egress
broker keeps the credentials that genuinely need host-keyed injection, the
model providers among them.

---

## 10. Threat model

EmberVM evaluates itself against the threat model published by
[agent-substrate/substrate](https://github.com/agent-substrate/substrate/blob/main/docs/threat-model.md),
adopted as EmberVM's external conformance frame. The frame is
deliberately not ours: Substrate attacks the same problem from the density
side (many actors multiplexed onto shared warm worker pods), and its threat
enumeration is the most complete public statement of what a multi-tenant
agent execution plane must defend. Scoring Ember against criteria a
competing project wrote keeps these claims falsifiable, and most of Ember's
differentiation is exactly the rows that project lists as planned work
while Ember carries them as shipped invariants.

Vocabulary mapping: their *actor* is Ember's guest workload, their *worker
pod* is a Firecracker slot on a brick, their *atelet* is noded, their
*snapshot* is Ember's warmth artifact (memory snapshot, session bundle,
volume archive). The threat numbering is ours: upstream rows are
unnumbered, so threats are numbered 1 to 43 in upstream document order as
of its 2026-06-25 revision. The enumeration is condensed below;
shared-worker threats with no Ember analogue are answered by the "no reuse
across principals" row, and five unmapped threats do bind on Ember and are
open mapping work, not implied conformance: 8 (template-author reach into
storage), 21 (worker privilege: noded runs privileged with /dev/kvm), 26
(policy propagated out of band with scheduling), 31 (image-extraction
resource limits), and 42 (detection integrations).

### Attacks from guests

| Requirement (their threat #) | Ember state |
| ---------------------------- | ----------- |
| Hardened sandbox, never bare containers (15) | **Built.** Every guest is a Firecracker microVM; there is no container lane. New execution technologies enter as lanes under existing classes (invariant 9), so the sandbox floor is a platform decision, never a workload one. |
| Default-deny actor networking (17) | **Built, stronger** for the cross-actor half: task and session guests have no NIC at all, vsock only, so no actor-to-actor network path exists. Serving guests get a tap device reachable solely via node Envoy authority matches and kernel DNAT (section 2). Egress is the deliberate exception: the brokered proxy lane defaults internal-deny but external-allow (`EGRESS_EXTERNAL: allow`), so guests read the public internet by design, and the credential boundary rather than reachability is the control there (section 9). |
| No guest access to node services, metadata, or cluster DNS (16, 19, 34) | **Built**, with named exceptions: a vsock guest reaches the shim contract plus the deployment-declared internal egress allowlist (two entries in the reference deployment; the values file records that adding an entry is a security decision). No host network namespace, no metadata service, no cluster DNS inside the guest. |
| No Kubernetes or management-API escalation from guests (20, 22) | **Built** where it binds: a guest holds no cluster credential by construction (no NIC, no mounted ServiceAccount). The audience-scoped projected guest token is decided direction, not yet shipped. Definitions are CP-owned and there is no self-modification verb. |
| Worker state fully reset between actors (18, 27, 30) | **Built by construction.** Ember never reuses an execution environment across principals: a task gets a fresh VM, a session restores only its own lineage, and no VM or snapshot lineage ever crosses a principal (invariant 3). There is no scrubbed-shared-worker path to get wrong, placement is CP-owned, never guest-chosen, and each VM's rootfs and scratch are private to it: no filesystem is shared between guests. |
| Credentials never inside the sandbox by default (28, 29) | **Built.** The brick-local egress proxy holds the real credential and injects it only at the sidecar hop, only for hosts in that secret's `egressTo`; revocation at the validator is the control, and RAM scrubbing is rejected as a mechanism (section 9). **Planned:** per-principal grants at the credential broker and request-scoped GitHub tool mediation replacing host-keyed injection. |
| Quotas and rate limits on creation and spend (9, 33) | **Built** as enforcement machinery: admission fails closed, a configured quota of 0 is a hard stop at submit, and metering rides the operation (invariant 4). The per-principal daily budget is deliberately unset in the reference deployment (`deploy/values.yaml`), so spend is bounded by admission caps and concurrency, not by a per-principal quota, until a budget is set. |
| Snapshot theft, substitution, or self-written snapshots (23, 24, 25, 32) | **Planned** (#4691): per-principal envelope encryption of mutable warmth, digest-verified manifests, and restore authorized by the tuple (principal, lineage, brick, workload, generation, lease), never by storage ACL alone. Today the boundary is store ACLs plus the fail-closed vendor stamp, which defends against accidents, not against an adversary with store access. |

### Attacks from clients and the internal network

| Requirement (their threat #) | Ember state |
| ---------------------------- | ----------- |
| No direct internet exposure of guests, nodes, or the CP (1, 2, 3) | **Built.** Nothing faces the internet directly; ingress rides the deployment's zero-trust edge tunnel, public routes are scoped at their HTTPRoutes, and the serving shim's reserved `/shim/` prefix is unreachable from outside (section 9). |
| Mutual authentication and encrypted transport between components (4, 10) | **Planned** (#4693, deferred at R0): noded's gRPC currently runs open on the pod network. The bearer token is designed but disabled (`noded.bearerTokenSecret` ships empty, the CP attaches no metadata), and no network policy selects noded today (the only CiliumNetworkPolicy rendered covers the tokenbroker). mTLS/SPIFFE is a declared additive upgrade path (`proto/embervm/node/v1/node.proto`). Encrypted session-routing tokens are decided direction. Management callers authenticate via Kubernetes TokenReview against an allow-list; the actor / principal / permission split with per-verb authorization is decided direction. |
| Control plane isolated from the data plane (6) | **Built** as a seam: the CP runs on Kubernetes, noded runs on bricks, and payloads never traverse the CP (invariant 2). **Accepted risk** in the reference deployment: guests co-locate with the etcd masters (section 11); do not import that clause into a cluster whose etcd is precious. |
| Runtime configurable only by administrators (7) | **Built.** A workload chooses class and source (zip or image); the sandbox technology, kernel, and platform bases are CI-built platform artifacts it cannot substitute. |
| A sanctioned, secure path for secrets (11) | **Built.** The cluster's secret operator is the only secret source, and guests receive none (the section 9 credential classes). |

### Attacks from nodes and insiders

| Requirement (their threat #) | Ember state |
| ---------------------------- | ----------- |
| Node storage access scoped to actors scheduled on it (36, 37) | **Planned** (#4691): a brick receives a short-lived decryption capability for exactly the tuple it is waking, so a compromised brick or a bulk bucket copy yields nothing readable beyond its own live assignments. Today any brick with store credentials can read any warmth object. |
| Node API access scoped to its own actors (38) | **Built in shape.** noded dials home and is adopted keyed by (node, pod uid); node reports are authoritative only for instances anchored to that node, and wake grants are gated on the volume's anchor (section 4). |
| Granular admin access and envelope encryption at rest (39, 40) | **Planned** for principal warmth (#4691), with two KEK custody modes: platform-managed, or customer-managed in the principal's own KMS with wrap/unwrap grants only, so key material never enters the platform and revocation is the customer's unilateral act. The op-log deliberately shares a Postgres cluster (section 11); payload separation and principal-scoped erasure are decided direction. |
| Audit logging of all control actions (41) | **Built.** Every lifecycle and enforcement action is an ordered op-log append, and the op-log doubles as the audit record (invariant 7). The journal is prefix-compacted past 30 days; older audit lives only in the observability stack. |
| Containment of a detected-bad actor (43) | Partial. The live lever is principal cutoff as an admission action: stop minting tokens, 402 at the edge. The volume quarantine is a data-integrity guard against generation divergence, not an adversary control, and no brick- or principal-level quarantine primitive exists. An automatic containment policy is not decided. |

---

## 11. Deployment shape

EmberVM asks three things of a platform: KVM-capable nodes (bare-metal
instances on EKS; bare-metal or nested-virtualization nodes on GKE and
similar), a local scratch disk per Firecracker node, and an S3-compatible
object store. Everything else is standard Kubernetes.

- **Scratch is a node-provisioning contract**: every FC-labelled node
  bind-mounts its real device at `/var/lib/embervm/scratch` (hostPath type
  Directory fails closed if unsatisfied). Karpenter `instanceStorePolicy`
  RAID0 satisfies it on EKS; local NVMe elsewhere.
- **Warmth never crosses a CPU vendor** (until CPU templates land):
  artifacts are keyed per vendor, the gate fails closed at the daemon, and
  each vendor pool holds its own warmth.
  `noded.warmRestoreWithVolumeClasses` arms uniformly across the size
  classes, never partially, per the rollback contract; measured
  load-to-resume is 2.5ms on both vendor pools.
- **The control plane runs one replica** with `strategy: Recreate`; its
  op-log is a Postgres database (a shared cluster is acceptable: a CP
  outage is a designed-for state, and CP rolls are the availability events
  the node-local activator exists to survive). Multi-replica needs the
  single-writer-per-cell appender.
- **Small fleets may co-locate guests with control-plane nodes** as an
  eyes-open risk acceptance, provided the cluster is GitOps-reconstructible
  and durable state lives in S3: quorum loss is bounded downtime, not data
  loss, and guests are the first OOM victims. **Do not import this clause
  into a cluster whose etcd is precious.**
- **A hard node taint is a recorded option, not required**: co-tenancy runs
  on honest requests plus a disposable priority class for guests.

The reference deployment's concrete fleet (node roles, live brick mix,
shared Postgres) is in [deploy/README.md](deploy/README.md).

**Known walls and provisional numbers** (each states what would move it):

| Item | Value | Status |
| ---- | ----- | ------ |
| `statefulTcpPortRange` | 10 ports (5400-5409), CRD-validated | hard cap on stateful workload count; remedy constrained to name-based L4 (SNI/PROXY protocol), not per-workload ClusterIP Services |
| CPU pivot | 1,024 MiB per vCPU | provisional, tracks hand-set declarations; replace from measured utilization |
| Active brick utilization target | >90% | chosen by analogy; too high if shed events become common |
| Stateful continuity floor | 8h (also the S3 stateful warmth TTL) | asserted; validate against rotation cadence |
| Session live ceiling | 6h (`maxLifetimeSeconds`) | a version-convergence bound, not a durability claim; independent of the 8h continuity floor |
| Brick silence timeout / grant expiry | ~6h range | the divergence bound and availability trade in one number |
| Wake-grant gap budget | k=4 (default cadence class) | tolerates a CP gap with three noded restarts |
| Definitions target | 100k+ | owner-set goal, not measured demand |

---

## 12. Roadmap state

R0 Tasks, R1 Zip lane, R2 Sessions, R3 Serving, R4 Stateful, R5 Composite,
R6 Continuity, and R8 Consumers (agent threads on sessions) are
**shipped**; R5 has no live consumer, and `warmthS3Gc.allowEmptyKinds:
"group"` is the operator statement to the GC that the class is legitimately
empty. R7 Distribution is decided (vendor-aware placement over the
export/restore verbs; needs a second warm-capable node to matter). R9
Packaging (standalone open-sourceable artifact) is decided; hard
multi-tenancy (virtual control planes) is deferred pending real demand.
In-flight engineering: promoting brick autoscale from `up` to `full`,
node-local activator soak, and the conciseness program (#4009).

Decided direction, security: per-principal envelope encryption at rest and
verified tuple-authorized restore (#4691), and the management surface's
actor / principal / permission split. The threat model section carries the
per-row state.

The availability contract is spot semantics: a routine roll gives every
workload up to two minutes of drain notice; state durability is the hard
guarantee, connection continuity is not (narrowed for stateful by the
planned-drain contract).

### S3 warmth GC retention

An empty CP store of a kind whose S3 keys exist aborts the sweep as
possibly-not-rebuilt; `warmthS3Gc.allowEmptyKinds` is the operator statement
that a class is retired (its store is legitimately empty), which exempts that
kind's branch. Unknown tokens fail the chart render and are dropped by the
app, so the guard can only be weakened deliberately.

The S3 warmth GC is dry-run in code and may delete only the explicit
allowlist of warmth prefixes. It is **armed in the reference deployment**
(`warmthS3Gc.enabled: "1"`); rollback is setting `enabled` back to `""`
and bumping the chart. Its 8-hour stateful TTL keeps the newest one
reference per vendor and workload for live workloads (any non-terminal
instance or volume row); older superseded refs are eligible after the grace
window. Dead workloads' namespaces, including their newest ref, are evicted
after the TTL. `base/` remains excluded, so current base plus the newest
stateful ref for live workloads is preserved by construction.

Session and serving refs, plus session-workspace lineages, have no history
retention guard. They are protected while the corresponding instance is
actively live, including attached and in-flight transition states. Banked and
parked states are not actively live, and their warmth is eligible after the
configured TTL once a parked session's CP `expires_at` has passed. This ensures
a later resume follows the existing session-expiry 410 path rather than
reattaching an empty workspace. Terminal states are expired, evicted,
destroyed, or failed for sessions and evicted, destroyed, or failed for
serving.
