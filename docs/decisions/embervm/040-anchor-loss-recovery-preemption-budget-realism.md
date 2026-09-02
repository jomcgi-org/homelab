# ADR 040: Anchor-Loss Recovery, and Retiring the AWS Preemption Number

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-09-02
**Amends:** standing decision 11 (`projects/embervm/control/lib/embervm/stateful_manager.ex:72-81`), never previously its own ADR
**Builds on:** [ADR embervm/011](011-distribution-longhorn-fencing-cp-rollouts.md) (the control plane as sole generation issuer, its Longhorn half withdrawn by ADR 025 but its fencing half unaffected), [ADR embervm/009](009-roadmap-extension-continuity-before-tenancy.md) (the drain-budget priority order and the 2-minute preemption figure this amends), [ADR embervm/025](025-local-disk-authoritative-s3-archive-interval.md) (deliberate, non-automatic failover as previously decided, and `exported_generation` as the recovery proof)

---

## Problem

Brick nodes on GKE carry `cloud.google.com/gke-spot=true` and are preemptible
by design: they churned four times in 24 hours during the week of
2026-08-31. `demo-postgres`, a stateful workload, went down twice in 18
hours as a direct result. Both times, every wake against the workload
failed `Embervm.StatefulManager.plan_wake/2`'s `{:error, :volume_node_gone}`
path (`stateful_manager.ex:72-81`), and both times the only recovery was a
human issuing a management-auth DELETE to tear down the stuck row and force
a fresh boot, discarding whatever the workload held.

The data was never at risk. `demo-postgres`'s volume exports to the object
store on every bank commit (ADR 025 decision 3); `gs://h0melab-ember-bases/
volume/demo-postgres/vol.img` and its generation ledger were intact through
both outages, and a banked snapshot under `stateful/intel/` sat unused the
whole time. Two things kept the control plane from reaching for it:

- `volume_restorable?/2` asked the volume's own anchor node whether the
  object store was reachable (`store_reachable?/2`, pre-fix). A node that
  has been preempted reports no capacity fact at all, so the answer was
  unconditionally `false`. Reachability is a property of GCS, not of one
  dead brick; any live node that can reach the store can drive a restore.
- `exported_generation` lived only in ETS and was dropped on every
  projection rebuild, so a control-plane restart erased the one fact that
  proved a recoverable copy existed, at exactly the moment recovery needed
  it.

Both gates are now closed on `main` (`fix(embervm): restore a stateful
volume when its anchor node is gone`, and its two immediate follow-ups
making the recovery projection durable and spec-classified). This ADR
records what that change decided, why the anchor pin from standing decision
11 still holds everywhere it mattered, the one place standing decision 7
(fire-and-forget exports) had to bend, and a second, unrelated but
adjacent fact this incident exposed: the platform's preemption-notice
budget is sized for the wrong cloud.

---

## Decision

### 1. Standing decision 11 is amended, not repealed

Decision 11 anchors a stateful wake to the node holding the volume, "never
a silent recreate on a different node," because volume files are node-local
NVMe with exactly one writer. That remains correct for a volume that exists
**only** on its anchor's disk: two writers pointed at one VM disk image is
corruption, full stop, and the pin is what a single-writer fence looks like
before any second copy exists.

What changes is the case an exported volume falls into. Once a `(vol.img,
generation)` pair has left the anchor for the object store (ADR 025), the
volume is no longer only on the dead node. Restoring it onto a live peer is
not the silent recreate decision 11 forbids; it is the exact operation
`Embervm.StatefulHandover` already performs deliberately, `export before
restore, restore before re-anchor, re-anchor before evict`
(`stateful_handover.ex`), run here with the difference that the source
copy cannot be evicted because it is gone rather than merely superseded.
The anchor pin still governs every volume with no export: a fresh workload,
or one whose only bank predates the object-store wiring, still fails
`:volume_node_gone` and stays down until an operator acts, exactly as
before.

### 2. The evidence required before re-anchoring

A re-anchor needs all three of the following, not any one:

1. The anchor absent from `NodeCapacity` right now.
2. Absent continuously for at least the registry expiry window, 90 seconds
   (`node_registry.ex:126`, `@expire_after_ms`).
3. The control plane itself has been up for at least that same 90-second
   window (tracked per-manager as `missing_anchor_since_ms`, reset on
   every `Embervm.StatefulManager` boot, `stateful_manager.ex:174` and
   `:1156-1176`).

Absence alone is not proof of anything. `NodeRegistry` retracts a node's
capacity fact the moment it merely ages to `:down`, at 15 seconds
(`node_registry.ex:117`, `@down_after_ms`), which is a routine pod restart,
not a preemption. Gating on the 15-second signal would let an ordinary
noded rollout trip a re-anchor mid-roll, restoring a volume the source node
was about to reclaim in seconds. The 90-second registry-expiry window is
the shortest signal that actually distinguishes "briefly quiet" from
"gone."

The control-plane-uptime floor exists for a different failure shape: after
any control-plane restart, `missing_anchor_since_ms` starts empty and every
anchored volume is, from this process's perspective, freshly absent
regardless of how long the node has actually been reporting. Without the
floor, a control plane that restarts into a normal, fully-populated fleet
would treat every volume as a fresh miss and could act on that in the same
tick it boots. Requiring the control plane's own clock to clear the window
before it trusts its own absence observations means the dangerous direction
(acting on stale or incomplete state right after a restart) is the one that
needs time to elapse, not the safe one.

### 3. Why the fence still holds

The restore path writes exactly one durable fact: `node_id` on the volume
row, via `StatefulStore.upsert_volume/3` (`stateful_manager.ex:875`),
projected durably by the narrow `volume_recovery_updated` op
(`op_log/postgres.ex:1221`, `op_log/sqlite.ex:1571`, an `UPDATE` of
`node_id` and `exported_generation` only, never the blessing ledger). It
never writes a generation. The already-issued
blessing (ADR embervm/011's amendment, decision shape 1: CP-issued,
pre-dispatch) remains the sole authority for what generation is next; the
restored copy cold-boots and requests its next generation through
`start_wake`'s ordinary blessing path exactly as any other cold boot does.

That is what makes the amendment safe rather than merely convenient. If the
presumed-dead anchor was actually partitioned rather than dead and later
returns, it is holding a stale generation. `pair_valid?/1` and the
blessing ledger both compare against the current, control-plane-issued
generation, and the returning node's copy fails that check on its next
contact rather than being trusted as a second writer. The physical
single-writer fence (one writable attach per volume, `volume.Manager`) and
the generation-authority fence work together exactly as ADR embervm/011
describes: a re-anchor changes who holds the volume, never who may issue
the next generation, and a stale copy has no path to compete for a write
it was never blessed for.

### 4. Standing decision 7 gets one narrow exception: stateful volumes may hold the drain

Standing decision 7 says an async export must never stall the bank path or
the drain deadline: an enqueue that would block is dropped, full stop
(`store.go:1096-1100`). That rule is correct for bases and bundles, where
a lost export costs a slower boot and nothing else.

It is not correct for a stateful volume, where a lost export is data loss.
As decision 7 stands today, RPO for a stateful workload is "time since the
last successful export," not "the moment of preemption": a write committed
seconds before a node is preempted can be lost even though the drain ran
to completion, because the export of that final generation was fire-and-
forget and the drain did not wait for it.

**Decided:** for the stateful-volume artifact class only, a volume export
started during drain may hold the drain open, bounded by whatever remains
of the drain budget after `safety_margin_ms` is reserved
(`drain_coordinator.ex:38`, default 15 seconds). Bases, bundles, and
session workspaces are unaffected and keep decision 7 exactly as decided:
fire-and-forget, drop-on-block, next reconcile re-enqueues. The priority
order the drain coordinator already uses, durable classes first, serving
second, builds last (ADR embervm/009 resolved-question 5,
`drain_coordinator.ex:127-136`), is what makes room for this: stateful is
already first in line for the budget, so holding its export is holding the
front of a queue that was already going to run first.

State plainly what this does not buy: it is a bound, not a guarantee. If
the remaining budget expires before the export completes, the drain
proceeds anyway, the in-flight export is abandoned, and the workload
recovers to its previous successful export rather than the one that never
finished. The exception narrows the loss window from "since the last
export" to "since the last export, or the export attempted at drain time if
it had enough budget to finish," which is a real improvement on a fast,
mostly-idle volume and no improvement at all on a volume too large to
archive inside the leftover seconds. It does not make data loss impossible,
and nothing in this ADR should be read as claiming otherwise.

### 5. The preemption budget is sized for the wrong cloud

`DrainTimeout` defaults to 110 seconds, documented as "the 2-minute
spot-instance preemption notice minus notification latency"
(`config.go:194-202`, `values.yaml:1341-1349`, both citing ADR embervm/009
resolved-question 5). That figure is the AWS EC2 Spot contract. GCE Spot
gives approximately 30 seconds of preemption notice, delivered via the
instance metadata server, not two minutes. The published 110-second
budget the control plane's force-bank pass is bounded against therefore
overstates the fleet's actual preemption window by roughly a factor of
four: on the current GKE fleet, a preemption can arrive with barely a
quarter of the time the drain machinery believes it has.

Two numbers must not be collapsed into one. The **graceful budget** (a
scheduler-timed noded rollout, an upgrade, a deliberate scale-down) is a
number the control plane chooses and can afford to make generous, because
nothing outside the platform is imposing a clock. The **preemption
budget** is a number a cloud provider imposes, is specific to that
provider's spot contract, and is not negotiable. `DrainTimeout` today
conflates the two: it is used as both the graceful-rollout deadline
(ADR embervm/011's rollout sequencing) and the assumed preemption window,
and only one of those two callers is actually bounded by GCE's 30 seconds.

The accompanying work adds a GCE metadata-server preemption-notice watcher
so a brick's drain begins the moment the metadata server reports the
notice, rather than waiting for the Kubernetes `SIGTERM` that follows part
of that window later and spends delivery latency out of an already-short
budget before noded ever calls `SetDraining`. It hangs on
`instance/preempted` and enters the same drain path `SIGTERM` uses, and
`SetDraining` keeps the EARLIEST deadline, so a `SIGTERM` arriving after a
notice cannot extend it: the node leaves on the provider's schedule
whatever the kubelet believes.

The split lands with it. `DrainTimeout` stays at 110 seconds and is
explicitly redocumented as the GRACEFUL budget only; a new
`PreemptionDrainTimeout` carries 20 seconds, being GCE's approximately 30
minus notice-observation latency and noded's own stop. Lowering
`DrainTimeout` instead would have been the obvious change and the wrong
one: it also governs brick rollouts, where a long budget is correct, so
collapsing the two would have quietly given the control plane less time to
force-bank on every ordinary deploy.

The watcher lands DISABLED (`PreemptionNoticeEnabled` defaults false),
armed by a values change after live verification, per the standing
convention that a new mechanism is proven in production before it is
trusted. Until it is armed, the 110-second figure remains what the control
plane is told during a preemption, and remains wrong by roughly a factor
of four; that is the reason to arm it promptly rather than a reason to
treat this decision as complete.

### 6. Deliberately not decided here

**A per-workload `anchorLossPolicy` enum** (`hold` / `recreate` /
`restore`) was scoped during this work and is not being built. With the
restore path now functioning, `restore` is the behavior every stateful
workload actually wants when its anchor is confirmed gone: recover the
real data rather than either sitting down (`hold`) or booting empty
(`recreate`). `recreate` is only meaningful for a workload whose data is
genuinely disposable, and no workload on the fleet today declares that.
Building a policy switch to choose among three behaviors when one of them
is a strict improvement for the only workloads that exist is speculative
generality; the analysis is recorded here so a future workload that
*does* want disposable-on-anchor-loss semantics does not have to redo it,
it can be added as a per-workload override against the mechanism this ADR
already ships.

**Moving volumes to network-attached storage** (the Longhorn RWO shape
ADR embervm/011 originally decided and ADR embervm/025 withdrew on cost)
would remove the anchor concept entirely: with no node-local single copy,
there is nothing to lose when a node vanishes and nothing to restore.
That is the correct answer if a workload ever needs an RPO of zero rather
than "since the last export, plus whatever the drain-time export bought
it." It is not decided here because ADR embervm/025 already priced it and
found the fixed cost (CSI in the durability chain, a fourth placement
owner, per-workload engine overhead paid by everyone) too high for a
property most workloads do not need. Nothing in this ADR reopens that
trade; it is named so the next person facing a genuinely zero-RPO
requirement starts from the archive-interval model's actual limit rather
than rediscovering it.

---

## Consequences

What becomes possible:

- A confirmed-dead anchor with an export on file recovers automatically,
  onto a live peer, without an operator's management-auth DELETE. The
  `demo-postgres` failure mode (down until a human notices and acts) is
  closed for any workload with at least one completed export.
- The drain-time export exception narrows, but does not close, the window
  between "last export" and "moment of preemption" for a stateful volume
  that happens to have spare drain budget when the notice arrives.
- The 110-second `DrainTimeout` is named as a known-wrong number for GCE
  rather than silently trusted, ahead of the split this ADR calls for.

What is given up:

- A volume with no completed export still fails `:volume_node_gone` and
  still needs an operator. This ADR narrows the outage class it applies
  to; it does not eliminate the manual path for a workload that has never
  banked off node.
- The drain-time export exception can still lose the in-flight generation
  if the remaining drain budget is shorter than the export. On the actual
  GCE preemption window (roughly 30 seconds, not the 110-second figure
  the drain code currently assumes), this exception buys real recovery
  headroom for a small, mostly-idle volume and very little for a large
  one, until the metadata-server watcher and the budget split land.
- `anchorLossPolicy` and NAS-backed volumes stay off the roadmap absent a
  workload that actually needs `recreate` semantics or a zero-RPO
  guarantee.

What stays true:

- Decision 11's core claim is unweakened: a volume that exists on exactly
  one disk is never silently recreated elsewhere. Only an exported volume,
  which by definition is not on exactly one disk, is eligible to move.
- The control plane remains the sole issuer of the next generation
  (ADR embervm/011). Restore changes placement, never issuance.
- Spot semantics (ADR embervm/009): durability, not connection continuity,
  is the hard guarantee, and it is bounded by whatever `archiveInterval`
  the workload configured (ADR embervm/025), not made unconditional by
  this ADR.

---

## References

| Resource | Relevance |
| -------- | --------- |
| `projects/embervm/control/lib/embervm/stateful_manager.ex:72-81` | Standing decision 11 as originally recorded, the anchor pin this ADR amends |
| `projects/embervm/control/lib/embervm/stateful_manager.ex:1109-1176` | `volume_restorable?/2`, `confirmed_anchor_gone?/1`, and `observe_missing_volume_anchors/1`, the evidence gate this ADR decides |
| `projects/embervm/control/lib/embervm/op_log/postgres.ex:1221`, `op_log/sqlite.ex:1571` | The `volume_recovery_updated` projection, the durable fact standing decision 11's re-anchor writes and nothing else |
| `projects/embervm/control/lib/embervm/stateful_handover.ex` | The manual volume-move verb whose ordering (export, restore, re-anchor, evict) this ADR's automatic path reuses |
| `projects/embervm/control/lib/embervm/node_registry.ex:116-134` | The 15-second `:down` age-out and the 90-second registry-expiry window this ADR's evidence rule is built from |
| `projects/embervm/noded/server/store.go:1096-1100` | Standing decision 7, the fire-and-forget export rule this ADR narrowly exempts for the stateful-volume class |
| `projects/embervm/noded/config/config.go:194-202` | `DrainTimeout`, defaulted to the AWS 2-minute spot contract this ADR flags as GCE-inaccurate |
| `projects/embervm/control/lib/embervm/drain_coordinator.ex:38,127-136` | `safety_margin_ms` and the durable-first drain priority order (ADR embervm/009 resolved-question 5) this ADR's export exception rides on |
| [ADR embervm/011](011-distribution-longhorn-fencing-cp-rollouts.md) | The single-writer fence and sole generation issuer this ADR relies on, unweakened |
| [ADR embervm/025](025-local-disk-authoritative-s3-archive-interval.md) | Deliberate, non-automatic failover as previously decided, `exported_generation` as recovery proof, and the NAS trade this ADR declines to reopen |
| [ADR embervm/009](009-roadmap-extension-continuity-before-tenancy.md) | The bounded-preemption contract and drain priority order this ADR narrows for the stateful class |
