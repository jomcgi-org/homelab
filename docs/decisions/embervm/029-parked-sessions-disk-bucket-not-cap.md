# ADR 029: Parked Sessions Count as Disk, Not Against concurrency.cap

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-08-04
**Amends:** [ADR 016](016-kubernetes-scheduling-integration-contract.md) decision 6's session durability ladder, on the capacity-accounting axis only (capture cadence and retention are untouched)
**Builds on:** [ADR 027](027-snapshot-modes-workload-property.md) (the `memory: false, filesystem: true` quadrant, whose park mechanism this ADR's accounting was not updated for at the time), [ADR 008](008-interruptible-bank-stateful-datastores.md) (the standing principle that a dark, resource-holding session must not squat a live-VM cap slot and its memory indefinitely)

---

## Problem

`concurrency.cap` (`spec.concurrency.cap`) is documented in the workload chart template as "the max number of LIVE session VMs," with banked sessions bounded separately by `session.maxSessions` because banked "holds disk, not a VM." `Embervm.SessionStore`'s own capacity-counting comment states the same principle directly. `:parked` never fit that principle: it was carried in `@live_states` for good reason (still non-terminal, still dialable through rejoin), but the per-workload counts derived from `@live_states` bucketed everything in it, `:parked` included, into `:live`, and `check_workload_cap/3` gates create on `counts.live < cap`. So a parked session, which holds zero RAM, counted the same as a running VM against a cap whose own stated purpose is bounding concurrently running VMs.

ADR 027 is what made this a live bug rather than a latent inconsistency. Before it, session-class workloads banked to a memory snapshot and destroyed the VM; ADR 027's `memory: false, filesystem: true` quadrant, which claude-runtime runs, parks instead: `park_session` (`control/lib/embervm/session_manager.ex:1067`) transitions the VM to `:session_parked` and nulls `node_id`/`vm_id`, keeping only the per-lineage workspace volume. A parked session under this quadrant is architecturally identical to a banked one for capacity purposes, disk held, no VM, but the counts code never learned that.

The incident (2026-08-03, context in issue #4298): two idle parked verification sessions on claude-runtime (`cap: 2`) held both cap slots for over five hours. Every session create failed with `workload_cap` for the entire window, with no VM running on either slot. The cap was doing its job by its own bookkeeping and denying real capacity that did not exist.

## Decision

Parked sessions move to the disk bucket. `SessionStore.counts/2`'s `:banked` figure becomes "banked or parked", `check_workload_cap/3`'s live count excludes both, and `check_session_cap/3`'s live-plus-banked check against `session.maxSessions` is unaffected because parked already contributed to that sum (a parked session has always held a resource, disk instead of a VM, so it belongs in `maxSessions`'s accounting; what changed is only which bucket of that accounting it lands in). Mechanically: `SessionStore`'s `bucket_of/1` gets a `:parked -> :banked` clause ahead of the general `state in @live_states -> :live` clause, so a parked session decrements `:live` and increments `:banked` on the `:park_complete` transition, the same way `:banked` itself already does. `@live_states` itself is unchanged, since parked sessions are still non-terminal and still dialable; only which counts bucket they land in moves.

`concurrency.cap` is re-derived from 2 to 3 for claude-runtime now that it measures only concurrently running VMs rather than running-plus-parked. The old value of 2 was never a considered answer to "how many VMs can this workload run at once"; it was a number that happened to survive under a cap that was frequently pinned by parked sessions it should never have counted, so lowering the effective headroom below what the workload can actually use. 3 is this ADR's own derivation of the intended quantity, not a historical constant carried forward.

| Aspect | Before | After |
| ------ | ------ | ----- |
| What `:parked` bucketed as | `:live` (via `@live_states` fallthrough) | `:banked` (disk bucket, alongside `:banked`) |
| What `concurrency.cap` bounds | running VMs plus parked sessions | concurrently running VMs only |
| What `session.maxSessions` bounds | live plus banked (parked already included via `:live`) | live plus disk-held (banked plus parked), same total, reclassified |
| claude-runtime `cap` | 2 | 3 |
| CRD `status.sessions` / `sessionsSummary` | parked counted in `live` | parked counted in `banked`; no schema change |

## Rationale for the asymmetry: wake does not re-check cap

Rejoin from parked does not re-check `concurrency.cap`, mirroring the existing precedent that relight from banked does not either. This is deliberate, not an oversight the accounting fix should have closed: wake capacity is governed by two other mechanisms that already exist independently of the cap check, `Scheduler.place_with_demand`'s memory-eligible-brick admission pinned to the volume's node, and the per-principal wake-rate limit (`park_and_relight`'s sliding-window counter, `session_manager.ex:1364`-`1398`, denying with `:wake_rate_limited`). `concurrency.cap` is therefore a create-time governor on new session VMs, not an invariant that holds for every concurrently running VM at every instant: a wake burst of parked sessions can transiently exceed `cap`, and the platform accepts that, because RAM safety on the receiving node comes from placement's memory admission, not from the cap.

Adding a cap check to rejoin was considered and rejected (see Alternatives) specifically because it would reopen the failure mode this ADR fixes from the other side: a workload with several sessions legitimately parked (the steady state ADR 027's quadrant exists to support) could find its own rejoins blocked by its own cap, for a resource, RAM on a VM about to be created, that a parked session by definition does not hold yet.

## Consequence for CRD status

`status.sessions.banked` and `sessionsSummary` (`session_manager.ex:2548`-`2584`) read directly off `counts.live`/`counts.banked`, so a parked session now reports under the banked number with no CRD schema change; an operator reading `status.sessions` sees "N banked" grow to include parked sessions rather than seeing a `live` count that was never really live. A future ADR could split the disk bucket into `banked` and `parked` if operator visibility into which is which turns out to matter; that split is not made here because nothing today depends on distinguishing the two at the CRD-status granularity.

## Alternatives Considered

| Alternative | Rejected because |
| ----------- | ----------------- |
| Raise `cap` alone, leave the accounting bug in place | Parked sessions would still squat cap slots, just more of them before the workload hits the (now higher) ceiling; does not fix the incident's mechanism, only delays its recurrence |
| A third counts bucket (`live` / `banked` / `parked`) with its own CRD status field | Real future value for operator visibility (see Consequence), but no consumer needs the distinction today; adding the field now is schema churn without a driving need, so it is noted as follow-up rather than done here |
| Add a `concurrency.cap` check to rejoin, so a wake-time admission mirrors create-time admission | Would reintroduce the lockout this ADR fixes on the wake side: a workload with several legitimately parked sessions could have its own rejoins blocked by a cap check against a resource (a running VM) the parked session does not hold. Matches the existing relight-from-banked precedent, which also does not re-check cap on wake, and wake safety already comes from placement's memory admission plus the per-principal wake-rate limit |

## Security

Baseline: [docs/security.md](../../security.md). No trust boundary changes; this is a capacity-accounting fix over data already visible to the owning principal (its own session counts and CRD status). The wake-rate limit that substitutes for a cap check on rejoin is unchanged by this ADR and continues to bound wake-time abuse per principal.

## Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| A wake burst of many parked sessions on one workload exceeds `cap` transiently, since rejoin does not re-check it | Medium | Low | Accepted by design (see Rationale for the asymmetry); RAM safety comes from placement's memory-eligible-brick admission on the volume's node, and the per-principal wake-rate limit bounds burst size independently of `cap` |
| A future consumer wants `live` vs `banked` vs `parked` distinguished in `status.sessions` and finds the merged `banked` number insufficient | Low | Low | Recorded as a named follow-up (see Alternatives); no schema change needed to add the split later since the underlying `SessionStore` counts already track the transition that would feed it |
| `cap: 3` is itself not a measured ceiling, just a re-derivation of what 2 should have been absent the accounting bug | Low | Low | Same review cadence as any other capacity number in `deploy/values.yaml`; revisit if claude-runtime's real concurrent-VM demand exceeds 3 |
| The banked TTL sweep (`bankedTtlSeconds`) filters on `:banked` only, so parked sessions are reaped only by `maxLifetimeSeconds` (6h) or explicit destroy | Medium | Medium | Up to `maxSessions` abandoned parked sessions can therefore deny creates with `session_cap` for hours, the same lockout shape this ADR fixes, at a higher threshold; tracked as a follow-up issue (#4305) |

## Open Questions

1. Whether `status.sessions` should eventually split into three buckets (`live`, `banked`, `parked`) for operator visibility, and if so whether that is worth the CRD schema churn versus leaving parked folded into `banked` indefinitely.
2. Whether the workload chart template's comment on `concurrency.cap` ("max number of LIVE session VMs... banked sessions hold only disk") should be updated to name parked explicitly, now that the code enforces the distinction the comment already implied.

## References

| Resource | Relevance |
| -------- | --------- |
| [ADR embervm/027](027-snapshot-modes-workload-property.md) | The `memory: false, filesystem: true` quadrant that made `:parked` a real, common state for claude-runtime sessions rather than a rare transient |
| [ADR embervm/008](008-interruptible-bank-stateful-datastores.md) | States the standing principle this ADR applies to sessions: a dark, resource-holding workload must not squat a live-VM cap slot and its memory indefinitely |
| [ADR embervm/016](016-kubernetes-scheduling-integration-contract.md) | Decision 6, the session durability ladder this amends on the accounting axis |
| `projects/embervm/control/lib/embervm/session_store.ex` | `@live_states` (parked stays in it, still dialable), `bucket_of/1` (the `:parked -> :banked` clause), `counts/2` |
| `projects/embervm/control/lib/embervm/session_manager.ex:610-626` | `check_session_cap/3` and `check_workload_cap/3`, the two create-time gates this ADR's bucket change feeds |
| `projects/embervm/control/lib/embervm/session_manager.ex:1067` | `park_session`, which nulls `node_id`/`vm_id` on park, the mechanism proving a parked session holds no VM |
| `projects/embervm/control/lib/embervm/session_manager.ex:1364-1398` | `park_and_relight`'s wake-rate limit, one of the two mechanisms substituting for a cap re-check on wake |
| `projects/embervm/control/lib/embervm/session_manager.ex:2548-2584` | `status.sessions` / `sessionsSummary` writer, unaffected by schema but changed in content |
| `projects/embervm/chart/templates/workload-claude-runtime.yaml` | The `concurrency.cap` template comment this ADR's decision is consistent with but does not itself update |
| `projects/embervm/chart/values.yaml` | claude-runtime's `cap: 2` default, re-derived to 3 by this decision |
| `docs/security.md` | Security baseline |
