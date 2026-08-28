# Reading ADRs 019-027

Eight ADRs written as one design pass, answering one question: **what has to change for EmberVM to manage 100k+ workload definitions instead of tens.**

They are numbered in the order they were written, which is not the order they are best read in. This note gives the reading order and the idea they share. Everything decided is in the ADRs themselves; nothing here is a decision.

## The through-line

One principle, applied eight times:

> **The control plane holds sparse facts and derives dense things on demand. It never stores or witnesses anything that scales with the fleet.**

Each ADR is that principle meeting a different subsystem:

| Was O(fleet) | Became |
| ------------ | ------ |
| Request payloads in the ops journal | facts plus a reference, with payloads on their own lifecycle (019) |
| A metering write per dispatch | a local lease debit, reported (020) |
| Placement computed per arrival | assignment precomputed at forecast cadence (020) |
| Session routing state in a directory | an encrypted token the client holds (020) |
| Capacity as a two-dimensional vector | one number, because only memory is reserved (021) |
| A workload definition per tenant | a template plus a tenant record (026) |
| A Kubernetes object per workload | one CR per product, with definitions in the CP datastore (026) |

## Reading order

Three threads. Within each, later ADRs depend on earlier ones.

**Data (standalone, start here).**

- **019** Op-log data structure, payload separation, principal-scoped erasure.

**Runtime.**

- **021** Resource model. Read first: it establishes that capacity is a scalar, which the rest depends on.
- **020** Admission-only control plane, token routing, peer redistribution. The hub of the set, and the most amended: its decision 3 is withdrawn to 023.
- **025** Local disk authoritative, S3 an archive, durability an interval. Withdraws ADR 011's Longhorn decision for stateful volumes.
- **023** Class-scoped ownership arbitration. Resolves 020's withdrawn decision 3, and depends on 025 for the stateful half.
- **027** Snapshot modes as a workload property. Amends 016's durability ladder on four points and makes filesystem-without-memory persistence reachable. Read after 025: it reuses the same content-addressed archive mechanism and inherits its cross-principal prohibition.

**Product.**

- **022** Domain-scoped composition and the access fabric. Superseded in part by both of the next two.
- **024** Identity hierarchy and guest identity assertion.
- **026** Template composition, GitOps without per-workload CRs, registration.

## What this set changes elsewhere

Seven earlier ADRs carry amendment notes rather than being edited in place:

| ADR | Amended by | What changed |
| --- | ---------- | ------------ |
| 001 | 025 | R7's "a placement move is a copy, never a rebuild" no longer holds for stateful |
| 014 | 020 | metering leaves decision 3's synchronous hot-path write set; node-confirmed destruction is untouched |
| 015 | 020 | the per-brick quota lease is kept, but decision 5's fail-closed guarantee is withdrawn |
| 007 | 020 | the creation-critical-path rejection is reversed for the metering write only |
| 011 | 025 | stateful volumes stay on local disk; the Longhorn move is withdrawn |
| 016 | 020, 025, 027 | the CP-owned placement loop; decision 5's Longhorn-plus-S3 durability clause; and decision 6's durability ladder on four points (capture cadence, retention, keyspace, size ceiling) |
| 018 | 020, 023 | `meteringFailOpen` becomes the default; a brick may stop a *running* session under silence |
| 025 | 027 | decision 3's table gains a close-triggered capture row for the no-memory-snapshot mode |

## Confidence

All nine are **Draft**. Where a number is a guess rather than a derivation, the ADR says so: ADR 021's 1,024 MiB pivot, ADR 025's 8h continuity floor, and ADR 020's >90% utilization target are each labelled provisional with what would move them.

The known-unfixed wall: the stateful class is hard-capped at ten workloads by `statefulTcpPortRange` (5400-5409), CRD-validated. No ADR here fixes it; ADR 026 constrains the remedy (it cannot be per-workload ClusterIP Services) without choosing one.

## ADR map

How to read a decision: `projects/embervm/ARCHITECTURE.md` states what is
true (it is standalone and cites no ADRs); the ADR records why. Status is
the ADR's own header plus its amendment trail. Draft ADRs are 014, 015,
019-028, 032, 034, and 035. ADRs 019-027 are the design pass this note
orders; ADRs 029 and 030 are shipped corrections to the session model: read
them as current behaviour, not direction.

| ADR | Decides | Status / superseded by |
| --- | ------- | ---------------------- |
| [001](001-embervm-beam-firecracker-workload-orchestrator.md) | EmberVM itself: BEAM CP + Go noded, hit/miss invariant, classes, isolation, roadmap | Accepted; R7 copy-never-rebuild no longer holds for stateful (025) |
| [002](002-op-log-retention-and-compaction.md) | Op-log retention: read-time TTLs, sweeps, journal horizon + marker | Accepted; shape being restructured by 019 |
| [003](003-control-plane-managed-snapshot-distribution.md) | CP-managed snapshot distribution; Build/Restore/Export/Evict verbs | Accepted; verbs generalized by 009, placement resolved by 011 |
| [004](004-agent-sandbox-interface-compatibility.md) | Back kubernetes-sigs/agent-sandbox as the session interface via a deferred edge adapter | Accepted; adapter still gated on upstream traction |
| [005](005-embervm-eks-scale-out-metal-pool-bricks.md) | EKS scale-out: metal pool, bricks, EmberPool, dial-home, snapshot keys | Accepted; decision 3 (Pattern A guest base) superseded by 028 |
| [006](006-tla-formal-specification-pilot.md) | Scoped TLA+ pilot with three conformance layers | Accepted; three specs ship and run under TLC in the build; trace conformance still deferred |
| [007](007-sharded-control-plane-pg-oplog-cells.md) | Batched Postgres op-log tier; cells; hot-loop corrections | Accepted; metering-write rejection reversed by 020 |
| [008](008-interruptible-bank-stateful-datastores.md) | Opt-in two-phase interruptible bank (hot-or-warm wakes) | Accepted |
| [009](009-roadmap-extension-continuity-before-tenancy.md) | Continuity before tenancy: R6-R9 ladder, spot availability contract, S3 seam | Accepted |
| [010](010-bazel-skyframe-snapshot-query-demo.md) | Bazel warm-Skyframe demo as a stateless query consumer | Accepted |
| [011](011-distribution-longhorn-fencing-cp-rollouts.md) | Vendor-bound warmth; fencing; CP-sequenced rollouts | Accepted; **stateful Longhorn withdrawn by 025**; sole-issuer rule amended by 017/018 |
| [012](012-fleet-colocation-cp-dynamic-sizing.md) | Four-node co-located fleet; etcd blast radius accepted; grandfather rule | Accepted; dynamic sizing retired for bricks (013 §7) |
| [013](013-substrate-lanes-brick-sizing-capacity-tiers.md) | Classes vs substrate lanes; brick sizing; bricks everywhere | Accepted (as amended) |
| [014](014-worker-authoritative-state-hot-path-consistency.md) | Worker-authoritative state; async writes; node-confirmed destruction | Draft; decision 6 flag replaced by 015; metering clause amended by 020 |
| [015](015-isolated-high-throughput-lane-data-plane-placement.md) | Isolated high-throughput lane; data-plane placement; quota leases | Draft; fail-closed lease guarantee withdrawn by 020 |
| [016](016-kubernetes-scheduling-integration-contract.md) | K8s scheduling contract; priority projection; session ladder; credential invariant | Accepted; placement loop superseded by 020; durable posture amended by 025; ladder amended by 027 |
| [017](017-checkpoint-abort-quarantine-auto-heal.md) | Bounded auto-heal of the checkpoint-abort quarantine | Accepted |
| [018](018-node-local-activator-brick-authoritative-lifecycle.md) | Node-local activator (Fork A) and brick-authoritative lifecycle (Fork B north star) | Accepted; Fork A partially landed; posture promoted to default by 020 |
| [019](019-op-log-data-structure-payload-separation.md) | Payload separation; time partitioning; principal-scoped erasure | Draft |
| [020](020-admission-control-plane-token-routing-peer-redistribution.md) | Admission-only CP; JWE token routing; peer redistribution; fail-open metering | Draft; decision 3 withdrawn to 023 |
| [021](021-workload-resource-model-memory-pivot.md) | Memory as the only dial; derived CPU; scalar capacity; GB-seconds | Draft |
| [022](022-domain-composition-access-fabric.md) | Service composition over bindings; three-leg access fabric | Draft; superseded in part by 024 and 026 |
| [023](023-class-scoped-ownership-arbitration.md) | Class-scoped ownership; brick silence timeout as the divergence bound | Draft |
| [024](024-identity-hierarchy-templates-and-registration.md) | Identity hierarchy; platform principal; guest identity assertion | Draft |
| [025](025-local-disk-authoritative-s3-archive-interval.md) | Local disk authoritative; S3 archive; `archiveInterval`; planned drain | Draft |
| [026](026-template-composition-gitops-registration.md) | Templates not stamps; GitOps without per-workload CRs; desired-set registration | Draft |
| [027](027-snapshot-modes-workload-property.md) | Snapshot modes as a declared workload property (persistence flags, shared keyspace) | Draft |
| [028](028-demand-loaded-rootfs-oci-chunk-store.md) | Eager-local rootfs: OCI ref as the interface, EROFS + Account content-defined chunk store, ublk presentation | Accepted; supersedes 005 decision 3 (Pattern A) |
| [029](029-parked-sessions-disk-bucket-not-cap.md) | Parked sessions bucket as disk, not against `concurrency.cap`; wake deliberately does not re-check the cap | Accepted; amends 016 decision 6 on the capacity-accounting axis only |
| [030](030-lineage-decoupled-from-session-generation.md) | Lineage decoupled from session generation; `maxLifetimeSeconds` (6h) reaffirmed as a version-convergence bound; continuity via workspace adoption | Accepted; amends 027's quadrant description |
| [031](031-health-signals-classified-by-time-to-impact.md) | Health signals classified by time-to-impact: immediate latch for platform-impact-now signals, >24h-sustained latch for maintenance-debt signals; both end in the health surface, not alert-only | Accepted; detector implementation tracked in #4338, not yet built |
| [032](032-federated-identity-adapters-authentik-sso.md) | Federated identity adapters: the actor / principal / permission split for the management surface, authentik SSO as the homelab identity source | Draft |
| [033](033-substrate-threat-model-conformance-encryption-at-rest.md) | Substrate's threat model adopted as the external conformance frame; per-principal envelope encryption at rest; digest-verified, tuple-authorized restore | Accepted; implementation tracked in #4691, not yet built |
| [034](034-conformance-harness-synthetic-actions-fault-injection.md) | ADR 006's layer 2 (trace validation), delivered and widened: hermetic and deployed lanes, direct-checker and TLC tiers, anti-vacuity manifests, DRILL/VACUOUS as distinct verdicts, strict-bias specs with a load-bearing Freight-approval override | Draft |
| [035](035-website-snapshotter-task-guest.md) | Task-class `shotter` guest: warm headless Chromium snapshotted as a base, an in-guest proxy that maps `jomcgi.dev`/`private.jomcgi.dev` to internal services and hard-allowlists only those two destinations ahead of the shared credentialed egress sidecar, agent-facing MCP tool returning a real `ImageContent` block | Draft; implementation tracked in #4994, not yet built |
| [036](036-platform-kek-custody-derived-control-plane.md) | Platform-managed KEK custody: per-principal KEKs derived per epoch from one root held by the control plane's key service; neither 1Password-per-principal nor a class-3 swap-tier mirror | Accepted; amends 033, resolving its open question 1 |
| [039](039-iggy-stateful-message-streaming-runtime.md) | Apache Iggy hosted as a stateful-class guest (`iggy`, listener 5402): the server binary lifted out of the digest-pinned `apache/iggy` OCI image because Docker Hub is its only durable release channel, env-var-only configuration with no baked TOML, and a first boot refused when the root password is absent | Draft; shipped inert (`enabled: false`, empty guest image) pending the 1Password item |

## Keeping ARCHITECTURE.md true

`projects/embervm/ARCHITECTURE.md` is the source of truth for EmberVM's
current state and decided direction, written as a standalone document with
no ADR references. The ADRs here record the rationale behind what that
document states. Any PR that creates, amends, supersedes, or withdraws an
ADR in this directory updates ARCHITECTURE.md in the same PR (the
`check-adr-architecture-sync` hook reminds on edits): Draft direction lands
in its decided-direction flags, built behaviour in its as-built narrative,
and this map gains the row. If the document and reality disagree, fix
both.
