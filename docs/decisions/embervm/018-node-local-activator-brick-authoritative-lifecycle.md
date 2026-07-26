# ADR 018: Node-local activator and a brick-authoritative workload lifecycle

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-07-23
**Refines:** [011-distribution-longhorn-fencing-cp-rollouts](011-distribution-longhorn-fencing-cp-rollouts.md), [014-worker-authoritative-state-hot-path-consistency](014-worker-authoritative-state-hot-path-consistency.md), [008-interruptible-bank-stateful-datastores](008-interruptible-bank-stateful-datastores.md), [017-checkpoint-abort-quarantine-auto-heal](017-checkpoint-abort-quarantine-auto-heal.md)

---

## Context

The `/ember/*` public demos exist to showcase Firecracker snapshot-restore latency, so they MUST scale to zero and cold-boot on the first request. That wake path dies during a control-plane restart, and CP restarts are routine: the CP runs one replica with `strategy: Recreate` because the SQLite op-log PVC is RWO and single-writer (`projects/embervm/chart/templates/deployment.yaml:8-15`), so every chart bump is a multi-second gap.

The serving ROUTE already survives the gap. `Embervm.EndpointPublisher` is the sole xDS writer, renders the Envoy snapshot as a pure function of in-memory facts, re-pushes level-triggered every 45s, and does one synchronous publish on boot (`control/lib/embervm/endpoint_publisher.ex`). The node Envoy holds its last-ACKed config throughout.

The WAKE does not survive it, because for a scaled-to-zero workload the last-ACKed config's fallback endpoint IS the activator, and both activators run inside the CP pod:

- L7 serving misses hit `activator_endpoint`, one fixed `{ip, port}` that is the CP pod's own address, proxied by `Embervm.ServingProxy`.
- L4 stateful/composite misses hit `{activator_ip, listen_port}` where `activator_ip = EMBERVM_STATEFUL_ACTIVATOR_IP` is the CP pod's routable IP via the downward API (`control/lib/embervm/tcp_activator.ex:83-88`, `application.ex:773-785`).

The fallback is therefore a POD IP that dies with the old pod on a Recreate, and Envoy black-holes every cold-workload request until the new CP boots and republishes. That is precisely the moment the demo is meant to shine.

A prior spec (issue #3993) proposed moving the wake onto the brick. Reviewing it against the code surfaced three load-bearing facts that reframe the whole design:

1. The two-writer fence for stateful volumes is PHYSICAL and already exists: a volume lives on exactly one node (`StatefulManager` returns `:volume_node_gone` rather than re-place), noded's `volume.Manager` permits exactly one writable attach (`noded/volume/volume.go:137-154`), and on the Longhorn tier attach exclusivity is a hardware-adjacent fact (ADR 011). What a CP-less relight lacks is not the fence but the CP's generation BLESSING, which is provenance ("which incarnation is legitimate"), not exclusion.

2. The CP's own reconciler would kill a brick-woken VM. `StatefulManager.destroy_orphan_stateful_vms` destroys every node-reported stateful VM with no matching CP row ("Stateful has no async-write adoption discriminator: destroy every such VM node-confirmed"), and `adopt_one` would also fail the old banked row as `vm_and_bundle_vanished`. A brick-local relight mints a `vm_id` the CP never issued, so both fire: double kill.

3. Idle-banking is CP-side. The idle-bank sweep decision runs in the control plane (`Embervm.StatefulSweeper` / `GroupSweeper` / `ServingSweeper`; `group_sweeper.ex:665-674` `idle_bank_pass` reads `idle_bank_seconds`). So during a CP gap NOTHING re-banks a workload: it relights once on the first gap request and then stays warm and available for the rest of the gap. This is both the reason a small generation budget suffices for Fork A and the seam Fork B moves.

---

## Decision

Adopt a brick-authoritative workload lifecycle as the declared north-star (Fork B), and ship it in two forks with Fork A as the mandatory first phase of Fork B. This is ADR 014's worker-authoritative thesis ("the node agent is authoritative for instance runtime state") carried from runtime STATE to the lifecycle DECISIONS (wake, and eventually bank and generation advancement) that produce it.

### Fork A: node-local WAKE, CP-side banking

The wake/activator moves onto the brick; the CP still drives idle-bank via the sweeper. This ships demo reliability. It is the spec's Lane A (stateless serving + composite relight) plus Lane B Phase 2 (stateful relight under a delegated blessing grant).

- **The brick activator lives in noded** and boots through the SAME internal code paths the `StartServing` / `StartStateful` RPC handlers use, so there is no second boot implementation. It keeps per-workload single-flight, a bounded park queue, and a local wake-rate limiter (the caps mirror the CP managers), and a local straggler check against noded's own live-VM inventory so a request arriving after the VM is already up splices straight through.
- **The fallback route points at the brick, stably.** noded installs a stable DNAT from `{node_ip, activator_port(s)}` to its own pod IP at startup (repaired on restart, del-before-add per the tap-leak lesson, #3745). `NodeStatus` advertises `activator_endpoint` (L7) and `activator_ip` (L4); `EndpointPublisher` renders those as the empty-cluster fallback, preferring the brick's advertisement and falling back to the CP address for a node that does not advertise one (rollout-safe, no flag day). The pure-function render property is preserved: the activator address is just another node fact.
- **Worker-authoritative creation, CP adoption.** noded mints the instance id and reports the VM with a wire marker `origin: ACTIVATOR`. On reconcile the CP treats an ACTIVATOR-origin VM with no row as an async-write to backfill (the serving/stateful analog of `SessionStore.backfill_created`), not an orphan: it appends the lifecycle ops late, adopts endpoint and health, and publishes. This is the ADR 014 orphan-rule discriminator (below).
- **The stateful relight is gated by a delegated blessing grant** (see the delegation section). The grant is what lets the brick advance a generation the returning CP will trust rather than quarantine.
- **Metering fails open by explicit flag.** A `nodeLocalWake: true` workload carries a companion `meteringFailOpen: true` catalog flag so the policy is named, not implied. During a CP gap the wake proceeds unmetered; on CP return, adoption backfill writes the lifecycle ops late and accounting reconciles best-effort. Gap-time rate-limit denials leave no instance to backfill and exist only in noded logs (accepted).

### Fork B: node-local BANK decision and steady-state generation-advancement delegation

The idle-bank sweep decision ALSO moves to the brick, so bricks own the full bank/relight lifecycle and the CP becomes reconciler and authority-of-record. This makes the scratch-postgres demo keep cycling (idle-bank then relight) even during a CP roll, and it takes the O(workloads) sweep loop off the single-writer CP.

Fork A is a strict subset and foundation of Fork B: the brick activator, `origin: ACTIVATOR` adoption, the durable node-local ledger, and the grant are all built in Fork A and reused unchanged. Fork B extends the grant from a bounded GAP budget into STEADY-STATE delegation, and moves four things across the CP/brick boundary. These are the hard part; they are stated honestly, not glossed.

**1. The grant becomes steady-state authority.** In Fork A the grant is a gap budget (a bounded generation window the brick may spend while the CP is away). In Fork B the CP permanently delegates renewable generation-advancement to the volume's ANCHOR brick, as a time-lease renewed on each reconcile pass. This rewrites ADR 011's standing decision 4 from "the control plane is the sole issuer of volume generation numbers" into "the control plane delegates a renewable generation-advancement lease to the volume's anchor brick, and remains the sole ADJUDICATOR of which advancements are legitimate." This is the load-bearing ADR-level decision in this record.

**2. The op-log absorbs node-authored generation churn.** Today the CP durably blesses BEFORE every dispatch (per-transition CP write). Under Fork B the brick's durable local ledger (`{VolumeRoot}/{workload}/gen`, `genblessed`) is the truth for the current generation, and the CP watermarks FORWARD-ONLY (the ADR 014 anchor-forward rule) and reconciles lazily from node reports, never re-deriving each transition. This is the opposite ordering from today and is what makes generation advancement scale per-brick instead of serializing through the single CP writer.

**3. Bank unpublishes node-locally against a still-published endpoint.** Today the sweeper unpublishes from Envoy first, rechecks idleness, then banks (`group_sweeper.ex:701`). xDS is CP-sole-writer, so a brick cannot pull itself from the fan-out. Under Fork B the node-local bank banks a STILL-PUBLISHED instance and leans on the activator fallback for the next request as the NORMAL path: the endpoint the brick just banked is stale for one request, which lands on the local activator and relights. This is safe because the activator is already the fallback for a scaled-to-zero workload; Fork B just makes "banked but still in the last-ACKed fan-out" an ordinary state rather than a race.

**4. Metering and checkpoint COMMIT migrate node-local.** Metering becomes fully reconcile-time (the CP tallies from backfilled lifecycle ops rather than gating at wake, an extension of the Fork A fail-open flag to the steady state). The ADR 008 checkpoint COMMIT-vs-ABORT decision, which today needs the CP to observe a parked connection, moves to the brick, where "is anyone waiting" is a local fact and noded already owns the resolve-timeout auto-abort.

### The cadence class (per-workload knob, resolved)

Grant sizing is a per-workload cadence CLASS, not one global constant, because the generation budget `k` is orthogonal to single-writer safety: the physical fence excludes a second writer regardless of `k`, so a wider window only widens anomaly-DETECTION, never weakens exclusion.

- **Default (ordinary stateful workload): count-bounded, `k = 4`.** Tight anomaly detection. A normal CP gap consumes ~ONE generation (idle-banking is CP-side, decision fact 3, so nothing re-banks the workload while the CP is away; it relights once and stays live). Extra generations come only from noded flapping mid-gap, so `k = 4` tolerates a gap with up to three noded restarts. Raise to 8 for long rollouts.
- **Demo / high-cadence workload: time-bounded (`gen >= G, expires_at`, no count ceiling).** The scratch-postgres demo intentionally idle-banks sub-second and relights per request, so a count ceiling would strand it; it MUST use the time-bounded class. Safe precisely because `k` does not gate exclusion.

The two bounds are asymmetric: too-small `k` strands the demo (a hard availability cost you feel), too-large `k` costs only detection width (a soft cost). So err generous. Expiry is a separate TIME bound, checked only at wake-start (never kills a running VM, whose safety is the attach), deliberately loose (~6h), and renewed every reconcile pass; its sole job is to eventually invalidate a grant the CP could not explicitly revoke (a long partition after the CP moved the volume, where attach-exclusivity is the primary fence and expiry the backstop).

| Aspect | Today | Fork A | Fork B (north-star) |
| ------ | ----- | ------ | ------------------- |
| Wake decision | CP activator (in the CP pod) | Brick activator | Brick activator |
| Idle-bank decision | CP sweeper | CP sweeper | Brick |
| Generation issuance | CP blesses before every dispatch | CP grants a bounded gap window | CP delegates a renewable advancement lease |
| Op-log generation writes | Per-transition, CP-authored, pre-dispatch | Backfilled on adoption | Node-authored ledger truth; CP watermarks forward-only, lazily |
| Metering | Sync, fail-closed at wake | Fail-open by flag, reconciled on adopt | Fully reconcile-time |
| Fallback route target | CP pod IP | Brick (stable DNAT) | Brick (stable DNAT) |
| Orphan-destroy rule | Destroy every unrecognized node VM | Skip + backfill `origin: ACTIVATOR` under a valid grant | Same |

---

## The two-writer safety argument

Two-writer exclusion for a stateful volume rests entirely on the PHYSICAL fence, not on grant width or cadence class. The grant/lease is PROVENANCE (which incarnation the control plane will trust), never the exclusion mechanism. Two writers cannot coexist regardless of how wide or long a grant is:

- **Spatial.** A volume exists on exactly one node. The CP never places a stateful wake anywhere but the volume's anchor (`StatefulManager` returns `:volume_node_gone` rather than re-place onto a node that does not hold the file). A partitioned or stale brick that does not hold the volume physically cannot open it.
- **Node-local.** noded's `volume.Manager` permits exactly one writable attach per volume (`noded/volume/volume.go:137-154`), and the daemon refuses a second live VM per workload with FAILED_PRECONDITION. A CP-driven wake racing a brick-local wake for the same workload reaches the same daemon and loses to the same lock; the loser's CP-blessed generation simply goes unused, the identical harmless gap a failed dispatch leaves today.
- **Longhorn tier.** Attach exclusivity converts the placement decision into a physical fact (ADR 011): a fresh attach on a new node invalidates a zombie's access, so a stale writer's writes fail rather than corrupt.
- **Temporal / provenance.** The grant is issued by the single serialized CP process, durably recorded before it is issued (op-log-before-dispatch, the same ordering as `bless_generation`), single-holder per volume (keyed to the anchor node; the CP does not renew when it moves the volume, and the move itself re-fences via attach), and bounded by generation ceiling and expiry. Any writer whose reported generation is NOT covered by a live grant is quarantined on sight, exactly today's fail-closed behaviour.

Widening `k` or switching to the time-bounded class changes only how tightly the CP can distinguish a benign self-inflicted advancement from an anomalous one after the fact; it never lets a second writer touch the volume. That is why a high-cadence demo can safely relight per request during a CP gap.

---

## Scope caveat: this is not justified on a scaling wall

On a four-brick homelab the CP sweeper is NOT a current bottleneck; the O(workloads) sweep loop is trivially cheap at this fleet size. Fork B's justification is architectural correctness (completing ADR 014's worker-authoritative thesis), full-lifecycle CP-gap resilience (the demo keeps cycling through a CP roll, not just surviving one), and showcase value, NOT relieving a load that does not exist. Fork B is worth doing for what it proves and how cleanly it removes the CP from the hot lifecycle, not because the current CP is slow. This is recorded so the decision is not later defended on invented scaling pressure.

---

## Consequences

What becomes possible:

- Every demo's disk-to-mem cold boot survives a CP roll (Fork A), and the stateful demo keeps idle-banking and relighting THROUGH a roll (Fork B).
- The single-writer CP stops being on the wake lifecycle path at all; generation advancement scales per-brick.
- The wake path stops depending on a pod IP that dies on Recreate.

What is given up / must be maintained:

- New op kinds enter the op-log (`wake_grant` and its renew/consume; the backfilled lifecycle ops for ACTIVATOR-origin instances). They must be accounted for by op-log retention and compaction ([ADR 002](002-op-log-retention-and-compaction.md)); an unconsumed grant must survive compaction until it expires or is consumed, exactly like ADR 017's unresolved dispatch record.
- `meteringFailOpen: true` is a standing, named grant that a `nodeLocalWake` workload wakes without a synchronous quota check. It is allowlisted to blessed homelab demos; it is a policy, not an accident, and is recorded as such.
- Fork B makes "banked but still in the last-ACKed Envoy fan-out" an ordinary state. The one-request staleness it introduces is absorbed by the activator fallback and must not be mistaken for a bug.

What stays true:

- Fail closed on enforcement, fail open on warmth (ADR 011). The default for an ungranted forward generation is still quarantine; the grant is a narrow, positively-proven exception, and every advancement it does not cover stays quarantined.
- Single-writer is still enforced by the physical fence (one-node volume, single writable attach, Longhorn attach-exclusivity), never by the grant. This ADR changes who may ISSUE a generation, never who may WRITE a volume.
- The hit/miss invariant (ADR 011, ADR 017): grant, blessing, bank, and adoption writes are lifecycle actions on the sweep / wake / reconcile path, never on the request hot path.
- Destruction stays node-confirmed and fail-closed (ADR 014 decision 5). A node VM with no valid grant coverage and no CP row is still an orphan to destroy; the `origin: ACTIVATOR` discriminator narrows that rule, it does not remove it.

---

## Alternatives considered

- **Warm-pin the demos (minInstances).** Rejected in one sentence: it erases the scale-to-zero cold boot the demos exist to demonstrate.
- **Multi-replica CP so the activator survives a roll.** Rejected here: that is CP HA (ADR 007, Postgres op-log cells), a much larger change, and it does not remove the CP from the wake path, only makes the pod more available. Node-local wake removes the dependency entirely and is the ADR 014 direction regardless.
- **A distributed fencing LEASE as the safety primitive.** Rejected as a mis-frame: the physical attach fence already excludes a second writer, so a lease protocol would re-solve a solved problem. The grant is deliberately scoped to PROVENANCE (which generation the CP trusts), a straight generalization of ADR 017's durable `checkpoint_dispatched` record, not a new consensus mechanism.
- **Move only the wake, never the bank (Fork A as the terminal state).** Rejected as the north-star, retained as phase 1: Fork A ships demo reliability but leaves idle-bank CP-side, so a stateful demo goes static (relit once, never re-banked) during a long CP gap and the sweep loop stays on the single writer. Fork B is the completion; Fork A is how it is delivered safely and incrementally.
- **Node-local composite full-create.** Rejected for this program: group create is CreateGroupNetwork plus role-ordered multi-member orchestration owned by the per-instance `GroupManager`; porting it to Go is a large, risky lift for little demo gain. Node-local composite is scoped to RELIGHT of a complete local banked set; full-create falls back to "wait for CP".

---

## References

| Resource | Relevance |
| -------- | --------- |
| [ADR 011](011-distribution-longhorn-fencing-cp-rollouts.md) | The physical fence (one-node volume, single writable attach, Longhorn attach-exclusivity) and the sole-issuer rule this ADR delegates; amendment note added there |
| [ADR 014](014-worker-authoritative-state-hot-path-consistency.md) | The worker-authoritative thesis this ADR completes; the orphan-destroy rule gains an `origin: ACTIVATOR` discriminator; amendment note added there |
| [ADR 017](017-checkpoint-abort-quarantine-auto-heal.md) | The durable-record provenance pattern (`checkpoint_dispatched`) the wake grant generalizes |
| [ADR 008](008-interruptible-bank-stateful-datastores.md) | The interruptible-bank checkpoint whose COMMIT/ABORT decision migrates node-local under Fork B |
| [ADR 012](012-fleet-colocation-cp-dynamic-sizing.md) | The co-located fleet and noded's persisted registry cache the brick activator boots from |
| [ADR 007](007-sharded-control-plane-pg-oplog-cells.md) | CP HA, the deliberately-not-this-work alternative to surviving a CP gap |
| Issue #3993 | The tracking issue and the reviewed spec this ADR records the decision for |
| Incidents: #3745 (tap-leak wedge), demo-postgres-after-CP-roll quarantine | The del-before-add discipline the stable DNAT reuses; the quarantine behaviour the grant clause extends |

---

## Amendment (2026-07-26)

- **[ADR 023](023-class-scoped-ownership-arbitration.md) decision 3b states a deliberate departure.** This ADR checks grant expiry "only at wake-start (never kills a running VM, whose safety is the attach)." ADR 023 has a brick stop a *running* workload once its control-plane silence timeout elapses, because a session has no attach providing that safety. The timeout is sized in this ADR's grant-expiry range so a control-plane roll never trips it. ADR 023 also declines to invoke quarantine for the session class, since sessions carry no grants and the rule would otherwise quarantine every legitimate relight.
- **[ADR 020](020-admission-control-plane-token-routing-peer-redistribution.md) decision 6 promotes Fork B's posture to the default.** `meteringFailOpen` stops being an allowlisted exception for blessed demos and becomes how metering behaves everywhere, on the grounds that metering allocates running costs within an organisation rather than charging customers. The flag retires rather than spreading.
