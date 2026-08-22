# ADR 037: Brick Silence Timeout, a Wall-Clock Complement to Generation-Bounded Leases

**Author:** Joe McGinley
**Status:** Draft
**Created:** 2026-08-22
**Relates to:** [ADR embervm/023](023-class-scoped-ownership-arbitration.md)
(class-scoped ownership arbitration, the decision that named the brick silence
timeout as the divergence bound), [ADR embervm/018](018-node-local-activator-brick-authoritative-lifecycle.md)
(the node-local activator whose exercise of authority this decision bounds),
[ADR embervm/005](005-embervm-eks-scale-out-metal-pool-bricks.md) (the dial-home
registration whose success this decision reads contact from)

---

## Problem

EmberVM's ownership-arbitration design bounds the divergence a partitioned
brick can cause with the **brick silence timeout**: "a brick that has not heard
from the control plane for longer than the timeout (~6h, in the grant-expiry
range, so a CP roll never trips it) stops serving everything it holds"
(`projects/embervm/ARCHITECTURE.md:658-664`). The 2026-08-22 audit found that
no code implements any such bound; the gap is tracked in issue
[#5073](https://github.com/jomcgi/homelab/issues/5073), and the architecture
document already carries the **Planned** marker saying exactly that
(`projects/embervm/ARCHITECTURE.md:662-664`).

Meanwhile, the node-local authority a silently wedged brick keeps exercising is
real and unbounded in time:

- The serving activator wakes guests and splices parked requests without
  consulting the control plane (`projects/embervm/noded/server/activator.go:66-69`),
  as do the stateful opaque-L4 wake path
  (`projects/embervm/noded/server/stateful_activator.go:37-41`) and the
  composite group activator (`projects/embervm/noded/server/group_activator.go:29-33`).
- A node-local writable attach self-advances the workload's generation through
  the held blessing lease, falling back to an unblessable bump when the lease
  is absent or exhausted (`projects/embervm/noded/server/stateful.go:98-135`).

Blessing leases fence stateful durability by GENERATION count, never by time:
the control plane grants a `[next_generation, lease_end)` range as an op-log
fact (`projects/embervm/control/lib/embervm/stateful_store.ex:1675-1708`), and
noded persists exactly two uint64 generation cursors
(`projects/embervm/noded/volume/volume.go:49-54`). A workload whose lineage
goes quiet never consumes its range, so its lease never exhausts and the
nearest existing fence never fires.

The control plane does notice a silent wedge, on its side: it ages a node to
`:down` off a periodic tick against `last_status_at`, precisely because "a
wedged stream never ends on its own"
(`projects/embervm/control/lib/embervm/node_registry.ex:31-43`). But that is
CP-side bookkeeping. Once the control plane stops dispatching, nothing changes
on the brick: traffic dials pods directly, so activator wakes and blessing-lease
self-advances continue indefinitely.

## Decision

Bound node-local authority by wall-clock silence. All points decided by Joe on
2026-08-22. The implementation work is tracked in
[#5073](https://github.com/jomcgi/homelab/issues/5073), not here.

### 1. Track last contact

noded tracks its last control-plane contact, updated on every successful
dial-home Register POST (the retry loop in
`projects/embervm/noded/server/register.go:96-165`, the POST and 2xx check in
`:171-201`) and on every message exchanged on the WatchNode stream
(`projects/embervm/noded/server/server.go:1737-1769`). These are the only two
live channels between brick and control plane, so together they answer "has
this brick heard from the CP recently".

### 2. Gate new node-local work past the bound

A configurable silence timeout (`EMBERVM_NODED_SILENCE_TIMEOUT_SECONDS`, 0
disables) gates all NEW node-local work once contact has been absent longer
than the bound:

- activator L7 serving wake,
- stateful L4 wake-on-connect,
- group activator start,
- blessing-lease self-advance on writable attach (the
  `ConsumeGenerationFromLease` path in
  `projects/embervm/noded/server/stateful.go:113-126`).

A refusal returns a distinct error naming the gate, so an operator or test can
tell "silenced" apart from draining, stale registry, or resource refusal.

### 3. Fail open on warmth

Nothing is banked or destroyed by the timeout. Live VMs keep running, and held
warmth stays intact, so recovery after the control plane returns is immediate.
The timeout refuses new work; it never destroys continuity.

### 4. One predicate, uniformly applied

All workload classes bind through one shared predicate evaluated at the entry
points above, rather than per-class policies. Splitting durable from
non-durable classes was considered and rejected below.

### 5. Default off, armed at six hours in deployment

Chart value `noded.silenceTimeoutSeconds`, default 0 (disabled). The
implementation PR arms it at 21600 (6h) in both `deploy/values.yaml` and the
dev values. ~6h sits in the grant-expiry range so a routine control-plane roll
never trips the gate; the architecture document states the same figure
(`projects/embervm/ARCHITECTURE.md:659-660`).

## Alternatives Considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Refuse plus drain (bank or destroy held VMs at the bound) | Destroys continuity whenever an outage outlasts the bound and then recovers; contradicts fail-open-on-warmth (section 3) |
| Full isolation (also cut steady-state service: DNAT teardown, vsock invoke refusal) | Most disruptive to live traffic for marginal safety gain over refusing new work, since steady-state serving traffic does not exercise node-local authority |
| Durable classes only (stateful/session/composite, leave serving/task unbound) | Two predicates to reason about instead of one, for negligible gain; task dispatch already fails closed without the control plane |
| Purely CP-side enforcement | Impossible: the whole point is behaviour during control-plane absence. The CP already ages wedged nodes out of dispatch (`projects/embervm/control/lib/embervm/node_registry.ex:31-43`) and that changes nothing on the brick |
| Rely on blessing-lease exhaustion alone | Bounds generations, not time (`projects/embervm/noded/volume/volume.go:49-54`); a quiet lineage never exhausts its lease |

## Consequences

- A wedged WatchNode stream now degrades to refused new work after the bound,
  instead of indefinite node-local authority.
- A genuine >6h control-plane outage freezes new session/serving placement
  fleet-wide until contact returns. Accepted: this matches the stated bound
  (`projects/embervm/ARCHITECTURE.md:658-664`) rather than inventing a softer one.
- The clock source is noded monotonic time, so NTP skew cannot trip the gate
  spuriously.
- The existing registry-stale boot gate is unchanged and composes with this
  one: a daemon already refuses new work until its first live sync
  (`projects/embervm/noded/server/server.go:568-584`) while warmth keeps
  flowing; the silence gate adds a second, independent reason for the same
  refuse-new-work posture.

## References

| Resource | Relevance |
| -------- | --------- |
| GitHub issue [#5073](https://github.com/jomcgi/homelab/issues/5073) | Outstanding implementation work: contact tracking, the shared predicate, entry-point wiring, chart value |
| [ADR embervm/023](023-class-scoped-ownership-arbitration.md) | Class-scoped ownership arbitration; named the brick silence timeout as the divergence bound this ADR implements |
| [ADR embervm/018](018-node-local-activator-brick-authoritative-lifecycle.md) | The node-local activator paths whose authority the gate bounds |
| `projects/embervm/ARCHITECTURE.md` | Section 8's divergence-bound paragraph (lines 658-664), which this decision turns from Planned into decided mechanism |
| `projects/embervm/noded/server/register.go` | Dial-home registration, one of the two contact channels |
| `projects/embervm/control/lib/embervm/node_registry.ex` | The CP-side silent-wedge age-out, cited as why CP-side-only enforcement is insufficient |
| `projects/embervm/noded/server/stateful.go` | `attachGeneration`, the blessing-lease self-advance the gate refuses under silence |
| `projects/embervm/control/lib/embervm/stateful_store.ex` | The `blessing_lease_granted` grant, generation-bounded not time-bounded |
