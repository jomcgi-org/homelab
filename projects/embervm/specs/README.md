# EmberVM TLA+ specs

Formal specifications of EmberVM's concurrency-critical protocols, checked by
TLC in CI. This directory is the pilot of [ADR embervm/006](../../../docs/decisions/embervm/006-tla-formal-specification-pilot.md):
five specs now, checked exhaustively over small bounds, plus the layer-1
vocabulary sync guard that keeps them honest against the code. Protocol 1 (VM
lifecycle + adoption) is `adoption.tla`; protocol 2 (session bank/relight
generation pairing) is `bank_relight.tla`, added by the ADR embervm/014 PR 5
follow-through. Both are modeled under the ADR embervm/014 worker-authoritative
consistency rules (node state is the source of truth, node-confirmed destruction).
Protocol 3 (the fail-closed per-principal daily quota gate) is `quota.tla`.
The SessionManager create-starvation model is `session_create.tla`. Protocol 4
(generation issuance authority: blessing, wake grants, quarantine,
checkpoint-abort auto-heal) is `generation_issuance.tla`, added for issue
#4700.

## What is here

- `adoption.tla` : the PlusCal model of the control plane's two views of VM state
  (dispatcher inventory + node-registry health machine) against a live node
  daemon and an adversarial crash scheduler. The header carries a prose map from
  each PlusCal action to the module and function it abstracts; keep it current.
- `adoption.cfg`, `adoption_liveness.cfg`, `adoption_wedge.cfg`,
  `adoption_resurrection.cfg` : the four adoption TLC run configurations (below).
- `bank_relight.tla` : the PlusCal model of the stateful bank/relight generation
  -pairing protocol (ADR 006 protocol 2): a volume's monotonic generation, a
  banked snapshot's pair-key stamp, the monotonic floor (PR #3770), the isolated
  single-use lane, and node-confirmed destruction.
- `bank_relight.cfg`, `bank_relight_isolated.cfg`, `bank_relight_regress.cfg` :
  the three bank/relight TLC run configurations (below).
- `quota.tla` : the PlusCal model of the opt-in, fail-closed daily quota gate at
  submit and dispatch, with durable usage as truth and ETS as a rebuildable cache.
- `quota.cfg`, `quota_zero.cfg`, `quota_submit_only.cfg` : the three quota TLC
  run configurations (below).
- `session_create.tla` : the PlusCal model of SessionManager's single mailbox,
  serial reconcile and create handling, and the #5051 create-starvation wedge.
- `session_create.cfg`, `session_create_liveness.cfg`,
  `session_create_wedge.cfg` : safety, positive liveness, and negative wedge TLC
  run configurations (below).
- `generation_issuance.tla` : the PlusCal model of the generation issuance
  authority (issue #4700): the CP as sole issuer of stateful volume generations,
  the three legitimate issuance shapes (CP pre-dispatch blessing,
  checkpoint-abort auto-heal, delegated advancement under a wake grant), the
  update_quarantine ladder, and the adversarial scheduler (either side crashes
  between any two steps; node reports may lag; a budgeted second writer).
- `generation_issuance.cfg`, `generation_issuance_liveness.cfg`,
  `generation_issuance_heal_wedge.cfg`, `generation_issuance_lag_wedge.cfg` :
  the four generation issuance TLC run configurations (below).
- `BUILD` : seventeen genrules run TLC over the five specs, one per cfg, via the
  `//bazel/tla` prebuilt toolchain (tla2tools.jar + a pinned Temurin JRE).
- `vocabulary.exs` : the layer-1 manifest declaring, per implementation surface
  (proto RPC verbs, health states, op-log kinds), what the specs model vs
  deliberately exclude. The ExUnit test in `control/test/embervm/` asserts every
  live enum member is classified and every modeled op-kind name appears verbatim
  in some spec `.tla`.

## The model, in one paragraph

VMs live on a node (durable across a control-plane crash, gone on a node crash).
The node-side variables are the SOURCE OF TRUTH (ADR embervm/014 worker authority):
the control-plane inventory is a reconciled cache, at all times, never the reverse.
Node status flows to the control plane over a bounded per-node FIFO whose messages
carry the streamer generation they were sent under; a message survives a node kill
(that is how a straggler exists). The control plane keeps a volatile primed-pool
inventory and a durable op-log of task state. `adopt_inventory` additively
reconciles the node's reported primed VMs back into inventory (the cache converging
toward node truth), which is what lets a restarted control plane recover a warm pool
it lost. The health machine ages a silent node to `unknown` then `down`; the down
edge reassigns the node's tasks and forgets the streamer generation before the kill,
so a straggler status is dropped rather than resurrecting the node. Destruction is
the consistency carve-out: a task's completion moves its VM to a `destroying` node
state (durable `:*_destroying` intent, VM still resident) and the CP records the VM
destroyed only after the node confirms teardown (`ConfirmDestroy`), so the CP's
destroyed record never precedes the node's actual teardown.

## The four configurations

Safety and liveness are checked by SEPARATE configs. Exhaustive liveness (the
temporal check) is far more expensive than safety in TLC, so safety runs over a
richer interleaving space while liveness runs over a lean one where the temporal
check is tractable. Two protocol switches (`AdoptionEnabled`, `ForgetBeforeKill`)
select the mode; bounds are set per cfg via the `MaxCPCrashes` / `MaxNodeCrashes`
/ `MaxGen` constants.

| cfg | bounds (nodes,VMs,tasks,princ; CP,node crash) | switches | checks | expect | proves |
| --- | --- | --- | --- | --- | --- |
| `adoption.cfg` | 1,2,2,2; 1,1 | adopt+forget+aging on | all eight invariants | pass | the shipped protocol's safety (incl. node-confirmed destroy) holds under the full crash interleaving |
| `adoption_liveness.cfg` | 1,2,1,1; 1,0 | adopt+forget on, aging OFF | `EventuallyDispatched` | pass | adoption makes dispatch progress across a CP restart |
| `adoption_wedge.cfg` | 1,2,1,1; 1,0 | adopt OFF, aging OFF | `EventuallyDispatched` | fail | re-finds the dispatch restart wedge |
| `adoption_resurrection.cfg` | 1,2,2,2; 1,1 | forget OFF, aging on | `NoResurrection` | fail | re-finds the straggler resurrection |

`AgingEnabled` is off in the two liveness configs. Silence-driven age-out is
unbounded, so with it on the adversary can cycle a node healthy -> down -> healthy
forever and starve dispatch: a scheduler artifact (a node that keeps reporting
stays healthy), not a real wedge. The liveness property is about the CP
crash-restart, not node aging, so disabling age-out there is faithful. The safety
and resurrection configs need aging on (for the down-edge and the straggler).

Generations are modeled as a small cyclic set (`MaxGen`) rather than an unbounded
counter: this keeps the state space finite WITHOUT a TLC state constraint (a
constraint is unsound under liveness checking) while keeping reconnect always
enabled so a down node can always recover.

### Bounds note (deviation from the ADR's "2 nodes, 3 VMs")

The ADR sketches 2 nodes / 3 VMs. The committed configs use ONE node and 2 VMs.
Both historical bugs are single-node phenomena (each is about one node's status
stream versus the control plane), so one node is the minimal faithful witness,
and 2 VMs suffices to exercise a double-assign (two tasks contending) and adoption
idempotence (a primed VM re-reported while claimed). Three VMs with two nodes blew
the liveness state space past a usable CI budget (tens of millions of states); the
ADR's bounds are an upper sketch, and the plan explicitly permits tightening when
the space explodes. VM ids recycle (an inexhaustible-supply model), so the finite
VM set bounds only how many VMs are live at once, not the total ever primed.

### Why the two negative modes exist

A spec that only ever passes is a spec nobody can trust to still bite. The two
negative configurations turn off one guard each and assert TLC still reproduces
the exact historical bug that guard prevents:

- **Wedge** (`AdoptionEnabled = FALSE`): removes `adopt_inventory`, so a
  control-plane crash strands the node's live primed pool (the fresh control plane
  never relearns the running VMs). A queued task then never reaches `assigned`
  across the restart: `EventuallyDispatched` fails. This is the production wedge
  that adoption was added to fix.
- **Resurrection** (`ForgetBeforeKill = FALSE`): models the buggy down-edge
  ordering where the streamer generation was not forgotten before the kill, so a
  straggler `NodeStatus` already on the wire is still accepted and drives a
  just-declared-down node back to `healthy`: `NoResurrection` fails. This is the
  D-R2.7.2 forget-before-kill bug.

The `//bazel/tla:tlc.sh` driver runs the negative cfgs in expectation `fail`: it
fails the build if TLC finds NO violation (the model has gone blind to the bug),
and requires the output to name an actual invariant or temporal-property
violation so a TLC crash is not mistaken for a detection.

## adoption.tla invariants checked (positive mode)

- `TypeOK` : state stays in its declared domains; crash budgets and channel depth
  respected.
- `NoDoubleAssign` : no two live tasks share a VM; a live task's still-live VM is
  in `assigned` state (never re-dispatched while primed).
- `AdoptIdempotent` : a VM never appears twice in inventory, nor in both inventory
  and the in-flight miss meta (the `known_vm_ids` guard).
- `NoResurrection` : a forgotten node (generation NULL) is never `healthy`.
- `NoReapLive` : a reap never destroys a VM the node still hosts live.
- `PrincipalIsolation` : a task is never assigned to a VM primed under a different
  principal (ADR 001's no-cross-principal rule).
- `NoDestroyBeforeConfirm` : the CP records an instance destroyed only after its
  owning node has torn it down (every VM in the CP's `cpDestroyed` set is already
  `destroyed` in node ground truth). This is ADR embervm/014 decision 5's
  node-confirmed destruction guarantee: only the node that performed the teardown
  may truthfully assert it happened.
- `DestroyIntentPrecedesRecord` : a destroy in flight (durable `:*_destroying`
  intent recorded, node not yet confirmed) is never simultaneously recorded
  destroyed; the intent and the destroyed record are disjoint until confirmation
  moves the VM from one to the other.

`EventuallyDispatched` (liveness, checked by `adoption_liveness.cfg`): every
submitted task eventually reaches `assigned` or a terminal state, across a
control-plane crash-restart. Fairness is attached PER PROGRESS ACTION (and per
node / per task where the action is node- or task-scoped) in `FairSpec`; the
adversarial crash actions are deliberately left unfair. Two modeling choices make
the liveness sound and finite: generations CYCLE through a small set (so a down
node can always reconnect and recover, and no state constraint is needed, which
would be unsound under liveness), and node age-out is silence-driven (a node with
fresh status pending does not age out, so the adversary cannot starve dispatch).

## Protocol 2: bank/relight generation pairing (`bank_relight.tla`)

Added by the ADR embervm/014 PR 5 follow-through, modeled under the same
worker-authoritative rules. A workload's on-disk VOLUME carries a monotonic
generation (node ground truth); a BANKED snapshot bundle stamps the volume
generation it was captured at (the pair key); a WARM relight is only legal when
the banked stamp still matches the current volume generation. The control plane's
`storedGen` is a CACHE reconciled from node volume reports, which can LAG (a node
still reporting the pre-bank generation, or a co-located sibling brick's stale
view). The MONOTONIC FLOOR (PR #3770) is the rule that a lagging report never
regresses the stored pair-key generation below its current value; without it, a
lagging report rewinds the stored generation below a just-banked bundle's stamp,
spuriously breaking the pair and evicting a good warm bundle (the recurring
demo-postgres `pair_broken` flap).

Three protocol switches select the mode: `MonotonicFloor` (the PR #3770 floor,
off in the negative mode), `Isolated` (the ADR 015 single-use lane), and the
`MaxGen` bound.

| cfg | bounds | switches | checks | expect | proves |
| --- | --- | --- | --- | --- | --- |
| `bank_relight.cfg` | MaxGen 3 | floor on, isolated off | all five invariants | pass | the shared-lane pairing protocol's safety holds (no regress, no stale relight, node-confirmed destroy) |
| `bank_relight_isolated.cfg` | MaxGen 3 | floor on, isolated ON | all five invariants | pass | a single-use isolated instance never banks, relights, or holds a banked bundle (`SingleUseNeverReused` non-vacuous) |
| `bank_relight_regress.cfg` | MaxGen 3 | floor OFF, isolated off | all five invariants | fail | re-finds the pre-#3770 generation regression (`GenerationNeverRegresses` violated by a lagging report) |

The invariants:

- `TypeOK` : generations, states, and the report channel stay in their domains.
- `GenerationNeverRegresses` : the stored pair-key generation never drops below a
  value it already reached (the monotonic-floor guarantee). This is the invariant
  the negative config trips: with `MonotonicFloor = FALSE`, a lagging report
  rewinds `storedGen`, which the driver requires to reproduce in expectation
  `fail`.
- `NoStaleRelight` : a warm relight only ever resumed a bundle whose stamp matched
  the TRUE volume generation. With the floor on, a CP-believed-valid pair is always
  truly valid; without it, a regressed `storedGen` can make a stale bundle look
  valid, which this catches ("no wake resumes a stale snapshot").
- `SingleUseNeverReused` : an isolated (single-use) instance never reaches a
  banking / banked / relighting state and never holds a banked bundle. Checked
  non-vacuously by `bank_relight_isolated.cfg` (`Isolated = TRUE`); the Bank /
  RelightWarm `~Isolated` guards structurally forbid the reuse lifecycle.
- `NoDestroyBeforeConfirm` : the CP records the instance destroyed only after
  node-confirmed teardown (`cpDestroyed => instState = "destroyed"`), the same
  ADR 014 decision 5 carve-out adoption.tla asserts for tasks.

### Why the negative mode catches the pre-#3770 regression

`bank_relight_regress.cfg` sets `MonotonicFloor = FALSE`, modeling the code before
PR #3770 added the `upsert_volume` floor. Reachable trace (MaxGen 3): a running
instance writes (volGen 1 -> 2); a node report of generation 2 advances `storedGen`
to 2 (high-water 2); then a LAGGING report of generation 1 (a node still reporting
the pre-bank generation) is folded WITHOUT the floor, rewinding `storedGen` to 1,
below the high-water mark. `GenerationNeverRegresses` fails. The driver runs this
in expectation `fail`, so a passing run would mean the model went blind to the
regression the floor fixes.

## SessionManager create starvation (`session_create.tla`)

`session_create.tla` models SessionManager as a single-mailbox GenServer that
serially processes periodic `reconcile` messages and session `create` messages.
Issue #5051 is the create-starvation wedge: when `do_reconcile` performs a slow
node RPC on the manager, the current reconcile never returns and creates queued
behind it cannot be dequeued. The fixed mode moves that work off-manager, modeled
as one reconcile tick, so every submitted create reaches `created` under fair
scheduling.

| cfg | bounds | switch | checks | expect | proves |
| --- | --- | --- | --- | --- | --- |
| `session_create.cfg` | pending 3; creates 2; slow 2 | blocking OFF | `TypeOK` | pass | mailbox, processing, counter, and create state remain within their domains |
| `session_create_liveness.cfg` | same | blocking OFF | `EventuallyCreated` | pass | non-blocking reconcile lets every submitted create complete |
| `session_create_wedge.cfg` | same | blocking ON | `EventuallyCreated` | fail | re-finds the #5051 create-starvation wedge behind blocking `do_reconcile` |

## Protocol 3: the fail-closed quota gate (`quota.tla`)

The quota model covers a per-principal daily vCPU-second budget enforced at both
the router submit gate and the dispatcher fair-queue gate. A configured principal
is denied when the cache is unreadable, while a principal with no configured
budget is allowed. Completion writes durable usage first and then updates the
advisory ETS cache only when it is available. Over-budget tasks stay queued and
unpark at the daily reset; dispatch skips are counters, not `quota_enforced`
audit entries.

| cfg | bounds | switches | checks | expect | proves |
| --- | --- | --- | --- | --- | --- |
| `quota.cfg` | 2 principals; queue 3; share 1; submits 3; crashes 1; days 1 | p1 budget 1, p2 unconfigured; dispatch gate ON | all seven invariants | pass | the shipped quota protocol is fail-closed, opt-in, audited, and bounded |
| `quota_zero.cfg` | same | p1 budget 0, p2 unconfigured; dispatch gate ON | all seven invariants | pass | zero budget is a hard stop and the zero-budget invariant is non-vacuous |
| `quota_submit_only.cfg` | same but crashes 0 | p1 budget 1, p2 unconfigured; dispatch gate OFF | all seven invariants | fail | the rejected submit-only D12.3 protocol exceeds the in-flight overshoot bound |

The invariants are:

- `TypeOK` : variables, budget domains, queue/share bounds, and counters stay in
  their declared domains.
- `FailClosedDeny` : a configured principal is never dispatched while the cache
  is unreadable. Non-vacuous in `quota.cfg`: the guarded state (cache down with a
  budgeted principal's work queued) is reachable, verified by checking the
  negation of that state as a throwaway invariant, which TLC violates at 67
  distinct states. Without a crash in the bounds this invariant would hold
  trivially, so `MaxCrashes` must stay at 1 in both positive cfgs.
- `OptInNeverDenies` : an unconfigured principal is never denied or skipped,
  including during a cache outage. This is the asymmetry-to-auth guard: auth is
  deny-all when unconfigured, quota is allow when unconfigured, and collapsing
  the two would make a Metering crash a global dispatch outage on any cluster
  that never opted into quotas.
- `ZeroBudgetNeverDispatches` : a zero-budget principal has no in-flight task and
  no durable usage. `quota_zero.cfg` makes this check non-vacuous.
- `CacheNeverExceedsDurable` : the advisory cache never exceeds durable usage;
  a dropped charge can only under-count until rebuild.
- `OvershootBounded` : configured-principal durable usage is at most budget plus
  one in-flight fair share, `Budget(p) + InflightShare * TaskCost`. This is the
  invariant the negative config trips.
- `DenialAudited` : every submit-time denial appends exactly one
  `quota_enforced` record. Dispatch-side skips are deliberately not audited.
  Unlike the six above, this one is maintained BY CONSTRUCTION (the two counters
  are only ever incremented together, and no action can separate them), so it
  documents the audit asymmetry rather than checking a property the model could
  violate. It is listed for completeness, and no audit result depends on it.

The model uses uniform `TaskCost = 1` budget units. The implementation compares
`used_cpu_ms / 1000 < budget` using FLOAT arithmetic, while the model uses integer
budget units. It models one current-day cache row per principal and resets both
usage maps at `DayFlip`, rather than keying the table by `{principal, day}` and
having hourly pruning as a separate action. `dispatchedWhileBlind`,
`blockedWithoutBudget`, and `submitDenials` are ghost variables used to turn
history properties into state predicates; they are not implementation state.

### Why the negative mode catches the D12.3 submit-only hole

With p1 budget 1 and zero usage, three `Submit(p1)` actions pass because totals
move only on completion, so all three tasks queue. With `DispatchGate = FALSE`,
three `DispatchTick(p1)` actions dispatch them and three `Complete(p1)` actions
produce `durable[p1] = 3`. The bound is `1 + 1 * 1 = 2`, so `OvershootBounded`
fails. With the gate ON, the first task completes and reaches `charged[p1] = 1`,
then the remaining two tasks park and `durable[p1] = 1 <= 2`.

This cfg sets `MaxCrashes = 0`, the one bound where it differs from the positive
cfgs, and the reason is a finding worth recording. Removing the dispatch gate
removes the dispatch-side fail-closed check along with it, because both gates
call the same `WithinQuota` operator (mirroring the implementation, where the
router and `fq_take` both call `Metering.within_quota?/4`). So with a reachable
cache outage this cfg violates `FailClosedDeny` STRICTLY BEFORE it reaches the
overshoot trace, and TLC reports that instead. Verified: with `MaxCrashes = 1`
TLC names `FailClosedDeny` at 130 distinct states, well short of the overshoot
behavior. Pinning crashes to zero makes `cacheUp` permanently true, so
`FailClosedDeny` holds trivially and stays in the checked set while the cfg
isolates exactly one guard and trips exactly one invariant, the same shape as
`adoption_wedge.cfg` and `bank_relight_regress.cfg`.

The substantive point behind that mechanic: the dispatch-side gate D12.3 added
for the overshoot bound is ALSO what carries fail-closed enforcement past the
submit path. Submit-only enforcement loses both properties, not just the bound,
which is a stronger argument for D12.3 than the decision entry itself makes.

## Protocol 4: generation issuance authority (`generation_issuance.tla`)

Added for issue #4700, this spec models the protocol with the worst incident
record in the system: the CP as SOLE issuer of a stateful volume's generation
(R7, ADR embervm/011 standing decision 4), judged against an adversarial
scheduler (either side crashes between any two steps; node reports may lag; a
budgeted second writer can jump the brick ledger). The header's prose map cites
the implementation modules action by action: `stateful_store.ex`
(`bless_generation`, `update_quarantine`, `ensure_blessing_lease`,
`record_checkpoint_dispatch`), `stateful_manager.ex` (`bless_wake_generation`,
`plan_wake`'s quarantine refusal), `stateful_sweeper.ex`
(`finish_checkpoint`), `noded/server/stateful.go` (`attachGeneration`,
`autoAbortCheckpoint`, `recordAbortGeneration`), and `noded/volume/volume.go`
(`BumpGeneration`, `RecordBlessed`, `ConsumeGenerationFromLease`).

The three legitimate issuance shapes (ARCHITECTURE.md, "Generation blessing
and quarantine") are one protocol here: CP-issued pre-dispatch blessing behind
the op-log-before-dispatch fence; checkpoint-abort auto-heal, where noded's
resolve-timeout backstop self-bumps exactly +1 on the same vm_id and a durable
`checkpoint_dispatched{workload, vm_id, generation}` record lets a restarted CP
prove the +1 was its own (ADR embervm/017); and delegated advancement under a
wake grant `[floor, ceiling)` consumed by the anchor's activator during
control-plane absence, degrading to the unblessable self-bump when exhausted,
which fenced-writer anchor adoption backfills on return (ADR embervm/014).
Report folding copies `update_quarantine/4`'s cond ladder order for order,
including the clear-suppression shield for uncorroborated blessed reports
while quarantined. The activator pairwise interaction is deliberately out of
scope (modeled later with the activator spec, per the issue), as are the ADR
embervm/037 silence gate, export gating, handoffs, and the pair-key machinery
(`bank_relight.tla`'s protocol).

| cfg | bounds | switches | checks | expect | proves |
| --- | --- | --- | --- | --- | --- |
| `generation_issuance.cfg` | 2 nodes; MaxGen 4; lease 1; wakes 2; CP crash 1; auto-abort 1; rogue 1 | heal + adoption + grant expiry ON, adversarial reports ON | all five invariants | pass | the shipped issuance protocol's safety over the full adversarial space: no regress, no serving while quarantined, no false quarantine, well-formed grants |
| `generation_issuance_liveness.cfg` | 1 node; MaxGen 3; lease 1; wakes 1; CP crash 1; auto-abort 1; rogue 0 | heals + adoption ON, expiry OFF, truthful reports | `EventuallyServed` (FairSpec) | pass | under fair scheduling every parked wake drains across a CP crash-restart, an auto-abort, and grant exhaustion |
| `generation_issuance_heal_wedge.cfg` | 1 node; MaxGen 3; rogue 0 | `AutoHealEnabled = FALSE` | all five invariants | fail | re-finds the pre-ADR-017 fail-closed-both-ways quarantine of the CP's own auto-aborted checkpoint |
| `generation_issuance_lag_wedge.cfg` | 1 node; MaxGen 3; rogue 0 | `AdoptFencedWriter = FALSE` | all five invariants | fail | re-finds the pre-ADR-014 fenced-writer deadlock: the anchor's own lag quarantines and the workload can never wake |

The invariants:

- `TypeOK` : ledgers, watermark, grants, records, budgets, and the report
  channel stay in their declared domains.
- `WatermarkNeverRegresses` : the blessing watermark never drops below its
  high-water mark (the `bless_generation` monotonicity guard plus the ladder's
  strictly-forward advances). Stated against a ghost high-water variable the
  way `bank_relight.tla` states its floor.
- `NoServeQuarantined` : a quarantined volume never serves. Tripwire style
  (like adoption.tla's `NoReapLive`): both serving paths carry the guard, and
  a ghost witness catches any drop.
- `NoFalseQuarantine` : the fence never quarantines an advancement the CP can
  account for: neither its own provably-resumed checkpoint (heal half) nor its
  anchor's own watermark lag (adoption half). This is the invariant both
  negative configs trip, one half each, and it is non-vacuous in the positive
  config: genuinely unprovable jumps (a non-anchor report, a mismatched or
  past-+1 jump under a live record) DO reach quarantine states there.
- `LeaseWellFormed` : a grant is an anchor-scoped range inside the generation
  cap whose cursor never passes its ceiling (a drained grant stays recorded
  with cursor = ceiling, like the store keeps the row until regrant). Grants
  change who may ISSUE a generation only: they are granted to and consumed by
  the anchor alone, and the writable attach stays fenced to the single writer
  throughout.

Two modeling decisions deserve their plain statement. First, the liveness
property carries two scoped escapes: the model's generation cap (real
generations are uint64-unbounded) and the ADR embervm/017 manual remainder
(an unresolved dispatch record whose context does not add up fails closed BY
DESIGN; recovery is runbook break-glass, a human decision outside the
protocol). What liveness still promises there is sharp: the fence never parks
a wake behind a mere watermark lag, and never without a provably unresolvable
record context (`NoFalseQuarantine`). Second, the liveness cfg sets
`AdversarialReports = FALSE`: with adversarial reports on, the scheduler can
rotate junk reports through the bounded channel forever and starve the one
truthful report, which is a scheduler artifact (adoption.tla's AgingEnabled
precedent), not a protocol wedge; the safety cfg checks the full adversarial
report space exhaustively instead.

### Why the two negative modes catch the blessing-watermark history

The issue calls this bug "three consecutive wrong fixes of the same shape, CP
ledger reasoning vs brick ledger running ahead". Each negative mode removes
one fix and asserts TLC still reproduces the outage that motivated it:

- **Heal wedge** (`AutoHealEnabled = FALSE`): keeps the durable dispatch
  record and its fail-closed arm but removes the heal, modeling ADR 017's
  Context sentence: a CP that cannot remember its own outstanding checkpoint
  must fail closed on BOTH its own auto-aborted +1 and a rogue's +1. Trace:
  pause at gen 1, record `{vm1, 1}`, CP crash, resolve-timeout auto-abort
  bumps to gen 2 on the SAME vm_id, CP returns, report `(2, unblessed)`:
  forward-unblessed under a live record, signature matches, but with the heal
  off the volume QUARANTINES (`falseQuarantineHeal`), and every later wake is
  refused forever: the demo-postgres 503 that was always going to be a
  rubber-stamp re-bless.
- **Lag wedge** (`AdoptFencedWriter = FALSE`): removes the fenced-writer
  adoption, modeling the naive fence before commit 1fb15264f. Trace: wake
  request parks, CP crashes before issuing anything, the anchor's activator
  takes the unblessable self-bump fallback (ledger 1 -> 2, marker left
  behind), CP returns, report `(2, unblessed)` from the ANCHOR itself:
  forward-unblessed with no checkpoint context, so with adoption off the
  volume QUARANTINES (`falseQuarantineLag`). A quarantined volume can never
  wake, so it can never re-bless to catch the watermark up: the recurring
  demo-postgres quarantine after a CP roll.

## Running TLC

CI runs all seventeen genrules via `bazel test //projects/embervm/specs/...`. There is
no local Bazel test loop in this repo. To iterate on a spec locally you need a JRE
(>= 11) and `tla2tools.jar` (v1.7.4, the version `//bazel/tla` pins); then, from a
copy of this directory (swap `adoption` for `bank_relight` for protocol 2):

```
java -cp tla2tools.jar pcal.trans adoption.tla            # retranslate after any PlusCal edit
java -XX:+UseParallelGC -cp tla2tools.jar tlc2.TLC \
    -workers auto -config adoption.cfg adoption            # positive check
```

The committed `.tla` carries the PlusCal translation between the
`\* BEGIN TRANSLATION` / `\* END TRANSLATION` markers. The CI driver re-runs
`pcal.trans` and diffs, so a PlusCal edit that was not retranslated fails loudly.
Never hand-edit the translation region.

## Scope

The pilot now models five protocols: protocol 1 (VM lifecycle + adoption,
`adoption.tla`), protocol 2 (session bank/relight generation pairing,
`bank_relight.tla`, added by the ADR embervm/014 PR 5 follow-through since the
pilot earned its keep), protocol 3 (the fail-closed quota gate, `quota.tla`),
and protocol 4 (generation issuance authority: blessing, wake grants,
quarantine, checkpoint-abort auto-heal, `generation_issuance.tla`, added for
issue #4700). The SessionManager create-starvation model `session_create.tla`
covers issue #5051 alongside them. Layer-2 trace validation (op-log events
mapped to TLA+ actions and checked against a drill trace) is a separate
follow-up and is deliberately not built here.
