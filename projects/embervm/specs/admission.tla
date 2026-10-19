------------------------------- MODULE admission -------------------------------
(***************************************************************************)
(* EmberVM memory admission across bricks: noded's in-flight ticket ledger *)
(* and the control plane's pending-claim ledger, placing on stale reports. *)
(*                                                                         *)
(* On 2026-10-10 production bricks were OOMKilled three times in about 70  *)
(* minutes, and two holes lined up. noded admitted on OBSERVED cgroup      *)
(* headroom while a restored guest faults its RAM in lazily, so an 8gi     *)
(* brick (budget 7936 MiB) admitted 5-7 claude-runtime guests of 3072 MiB  *)
(* declared, they grew, and the whole brick died. And the control plane    *)
(* placed a burst of creates on one brick from one stale NodeStatus with   *)
(* no in-flight accounting and one attempt, so a noded refusal surfaced as *)
(* a failed create. This spec models the protocol that replaced both and   *)
(* keeps each historical hole reachable behind a flag, so every cfg checks *)
(* the same spec text.                                                     *)
(*                                                                         *)
(* This is a bounded model of the protocol, not a proof that the code      *)
(* conforms to it; see the mapping below and the findings in README.md.    *)
(*                                                                         *)
(* ASYNC RESIDENCY (#7210): queued demand is outside this physical model. *)
(* Admitting demand uses the existing claim/ticket actions, including one  *)
(* claim per coalesced restore. Unresolved claims survive CP restart and   *)
(* do not expire by timer. Ready/consumed demand releases that claim only  *)
(* after exact landed-VM accounting or cessation proof. The new path       *)
(* requires reserved admission; it does not weaken any memory invariant.   *)
(* Invoke/cancel CAS and operation replay are covered by residency tests.  *)
(*                                                                         *)
(* IMPLEMENTATION MAPPING (revision c05e43f66).                            *)
(*                                                                         *)
(*   noded (projects/embervm/noded/server/pressure.go unless noted)        *)
(*     Admittable(b)    admissionFacts.admittableMib. observed =           *)
(*                      headroom - inflightMib - floor; reserved =         *)
(*                      budget - claimed - inflightMib                     *)
(*                      - O * (live + inflightVMs + 1); the reserved model *)
(*                      admits on min(reserved, observed).                 *)
(*     Admit(r)         Server.admit: under admission.mu, memAdmits, then  *)
(*                      inflightMib += need and inflightVMs++ before the   *)
(*                      unlock. That reservation is the ticket. A refusal  *)
(*                      is RESOURCE_EXHAUSTED pressure:mem.                *)
(*     Check, Reserve   the same two halves with the mutex dropped between *)
(*                      them (AtomicAdmit = FALSE).                        *)
(*     Land(r)          the driver boot call (fcvm driver Claim,           *)
(*                      RestoreSession, ClaimServing) returning: track()   *)
(*                      puts the VM in the live map, where LiveCount and   *)
(*                      ClaimedMib count it, BEFORE the ticket is released.*)
(*     ReleaseTicket(r) admissionTicket.release right after that call      *)
(*                      returns (server.go Prime and Relight;              *)
(*                      admissionLanded on the serving, stateful and group *)
(*                      paths). sync.Once makes the deferred release that  *)
(*                      covers every early return a no-op, so it is not a  *)
(*                      separate action.                                   *)
(*     BootFail(r)      the boot call, or any step between admit and it,   *)
(*                      returning an error: the deferred release, nothing  *)
(*                      claimed.                                           *)
(*     Grow(r)          a restored guest faulting its declared RAM in from *)
(*                      the snapshot memfile. One step takes it from none  *)
(*                      to all of it; a partly faulted guest only yields   *)
(*                      readings between those two.                        *)
(*     Destroy(r)       fcvm driver Release: kill the VMM, wait for exit,  *)
(*                      then delete the live-map entry.                    *)
(*                                                                         *)
(*   reports                                                               *)
(*     Report(b)        Server.admissionStatus into NodeStatus             *)
(*                      mem_admittable_mib (sent on signalChange and the   *)
(*                      liveness tick), stored by NodeRegistry in          *)
(*                      NodeCapacity. It fires at arbitrary times, so      *)
(*                      view[b] is the brick as it was at its last         *)
(*                      delivered report, arbitrarily old (A5).            *)
(*                                                                         *)
(*   control plane (projects/embervm/control/lib/embervm/)                 *)
(*     ViewAdmits       Scheduler.mem_eligible?/2: Brick.admittable_mib    *)
(*                      (the reported mem_admittable_mib) minus            *)
(*                      Scheduler.Inflight.pending_mib/2 >= need.          *)
(*     Pending(b)       Inflight.pending_mib/2: every unexpired row, each  *)
(*                      charging Brick.charge_mib (need + vm_overhead_mib).*)
(*     Place(r)         Scheduler.place/1 with the in-flight debit, then a *)
(*                      claim on the chosen head before the boot RPC:      *)
(*                      dispatcher.ex pick_node + spawn_assign_worker,     *)
(*                      pool_manager.ex start_prime_worker,                *)
(*                      Scheduler.place_and_claim/2. frontier[r] is the    *)
(*                      rest of the ordered candidates, walked without a   *)
(*                      re-check (dispatcher.ex claim_hop). No candidate   *)
(*                      is the retryable :capacity denial: r stays put.    *)
(*                      A request in UnclaimedRequests is placed by a lane *)
(*                      that debits other claims but writes no row of its  *)
(*                      own: session_manager.ex place_create and           *)
(*                      serving_manager.ex run_cold at this revision.      *)
(*     Booted(r)        Inflight.settle/3 on a successful boot RPC: the    *)
(*                      row becomes a short hold.                          *)
(*     Refused(r)       release_hop on RESOURCE_EXHAUSTED, then            *)
(*                      Scheduler.Retry.run moves to the next candidate    *)
(*                      (WalkFrontier: EMBERVM_PLACEMENT_RETRY). An        *)
(*                      exhausted frontier is the :capacity outcome, which *)
(*                      Embervm.Retry.classify/3 always retries            *)
(*                      (RetryableDenial). With both flags off a refusal   *)
(*                      is a terminal failure, the 2026-10-10 behaviour.   *)
(*     BootFailed(r)    release_hop on a non-pressure boot error; the      *)
(*                      request is retried later.                          *)
(*     Expire(b, r)     a settled row's hold (5 s) running out, or         *)
(*                      release_ref when a task worker finishes. Only a    *)
(*                      held row expires: the 200 s TTL exceeds the worker *)
(*                      timeout, so an unsettled row outlives its boot RPC *)
(*                      only when its claimer crashed (A8).                *)
(*                                                                         *)
(* ASSUMPTIONS AND DELIBERATE ABSTRACTIONS (each is a requirement on the   *)
(* implementation, not a claim about it):                                  *)
(*                                                                         *)
(*  A1 Memory is in abstract units and every brick has one Budget          *)
(*     (memory.max minus the daemon reserve): one size class.              *)
(*  A2 A ticket reserves exactly the memory the landed VM's live-map entry *)
(*     declares. noded charges the registry sizing (primeSizing,           *)
(*     relightNeedMib) while the live map records the snapshot's mem_mib   *)
(*     or the driver default, so the two agree only while the registry and *)
(*     the base agree. README.md records where they can drift.             *)
(*  A3 Overhead is both the per-VM admission charge (vmOverheadMib) and an *)
(*     upper bound on the host memory a live VM costs beyond its guest     *)
(*     RAM, and a guest never holds more than its declared RAM. Observed   *)
(*     headroom is Budget minus that resident total.                       *)
(*  A4 The live-VM slot backstop and the serving tap allocator are         *)
(*     omitted: they are read under the same mutex and only add refusals.  *)
(*  A5 A report is produced and received in one step. A separate send and  *)
(*     receive would also let the CP act on a report older than the newest *)
(*     one sent; that multiplied the state space twelvefold, and only CP   *)
(*     choices read the view, while noded re-checks every boot.            *)
(*  A6 Place reads the view and claims in one step. Two placers racing     *)
(*     between read and claim is one more optimistic reading, which the    *)
(*     arbitrary report staleness already produces.                        *)
(*  A7 Scheduler.Retry's max_attempts (3) never binds with 3 or fewer      *)
(*     bricks, so it is not modeled.                                       *)
(*  A8 A claimer crash is not modeled. Its supervisor releases the row on  *)
(*     DOWN (dispatcher release_claim, pool_manager DOWN) and the TTL      *)
(*     backs that up; a leaked row only makes the CP's view pessimistic.   *)
(*  A9 A failed boot hands the request back for a later attempt, at most   *)
(*     MaxBootFails times in total, so a fair run stays finite.            *)
(* A10 Destroy is atomic. The driver keeps memory charged until the VMM    *)
(*     has exited, which is the conservative order.                        *)
(***************************************************************************)

EXTENDS Integers, FiniteSets, TLC

CONSTANTS
    Bricks,            \* noded instances (model values)
    Requests,          \* VM create requests (model values)
    UnclaimedRequests, \* requests whose placer writes no Inflight row
    LargeRequests,     \* requests declaring LargeSize; the rest SmallSize
    SmallSize,         \* declared guest memory of a small request
    LargeSize,         \* declared guest memory of a large request
    Budget,            \* each brick's admission budget, in abstract units
    Overhead,          \* vmOverheadMib: per-VM charge and real host cost
    Floor,             \* memRejectFloorMib, the observed predicate's cushion
    MaxBootFails,      \* total boot failures the run may inject
    NULL,              \* no brick
    AdmitReserved,     \* TRUE: reserved model; FALSE: observed headroom only
    AtomicAdmit,       \* TRUE: check and reserve under one mutex
    LazyGrowth,        \* TRUE: a guest lands with no resident RAM and grows
    WalkFrontier,      \* TRUE: a refusal moves on to the next candidate
    RetryableDenial    \* TRUE: an exhausted frontier is retried, not failed

ASSUME /\ Budget \in Nat /\ Overhead \in Nat /\ Floor \in Nat
       /\ MaxBootFails \in Nat
       /\ SmallSize \in Nat \ {0} /\ LargeSize \in Nat \ {0}
       /\ LargeRequests \subseteq Requests
       /\ UnclaimedRequests \subseteq Requests

Size(r) == IF r \in LargeRequests THEN LargeSize ELSE SmallSize

\* The Inflight row a placement writes for r on brick b: an unsettled claim,
\* or nothing at all for a lane that does not claim.
ClaimRow(r) == IF r \in UnclaimedRequests THEN "none" ELSE "unsettled"

ReqStates   == {"waiting", "rpc", "running", "done", "failed"}
BootStates  == {"idle", "checked", "ticket", "landed", "live",
                "refused", "failed", "gone"}
ClaimStates == {"none", "unsettled", "held"}

\* Safety cfgs may reduce by brick symmetry. Requests are not symmetric
\* when their sizes differ, and liveness cfgs must not use symmetry.
BrickSymmetry == Permutations(Bricks)

VARIABLES
    \* -- control plane --------------------------------------------------
    req,         \* the request's placement phase
    at,          \* the brick its boot RPC (or VM) is on, NULL when none
    frontier,    \* candidates left to walk after a refusal
    claim,       \* claim[b][r]: the Inflight row for r on brick b
    view,        \* view[b]: the mem_admittable_mib the CP last received
    \* -- noded ----------------------------------------------------------
    boot,        \* the noded side of r's boot RPC
    resident,    \* guest RAM r's VM has faulted in
    inflightMib, \* admissionLedger.inflightMib (the counter, not a ghost)
    inflightVMs, \* admissionLedger.inflightVMs
    \* -- bounds ---------------------------------------------------------
    bootFails

vars == <<req, at, frontier, claim, view, boot, resident, inflightMib,
          inflightVMs, bootFails>>

RECURSIVE SumSize(_)
SumSize(S) ==
    IF S = {} THEN 0
    ELSE LET r == CHOOSE x \in S : TRUE IN Size(r) + SumSize(S \ {r})

RECURSIVE SumResident(_)
SumResident(S) ==
    IF S = {} THEN 0
    ELSE LET r == CHOOSE x \in S : TRUE IN resident[r] + SumResident(S \ {r})

Min(a, b) == IF a < b THEN a ELSE b

(***************************************************************************)
(* noded's facts. LiveOn is the driver's live map; TicketsOn is every boot *)
(* still holding a ticket. A VM in "landed" is in both: the driver call    *)
(* returned and track() ran, but release() has not, so admission counts    *)
(* it twice for that instant, which only ever refuses more.                *)
(***************************************************************************)
LiveOn(b)    == {r \in Requests : at[r] = b /\ boot[r] \in {"landed", "live"}}
TicketsOn(b) == {r \in Requests : at[r] = b /\ boot[r] \in {"ticket", "landed"}}
CommittedOn(b) == LiveOn(b) \cup TicketsOn(b)

ClaimedMib(b)  == SumSize(LiveOn(b))
ResidentMib(b) == SumResident(LiveOn(b)) + Overhead * Cardinality(LiveOn(b))

ObservedAdmittable(b) == (Budget - ResidentMib(b)) - inflightMib[b] - Floor
ReservedAdmittable(b) ==
    Budget - ClaimedMib(b) - inflightMib[b]
        - Overhead * (Cardinality(LiveOn(b)) + inflightVMs[b] + 1)
Admittable(b) ==
    IF AdmitReserved THEN Min(ReservedAdmittable(b), ObservedAdmittable(b))
    ELSE ObservedAdmittable(b)

\* What an empty brick admits: the largest request that can ever fit.
EmptyAdmittable ==
    IF AdmitReserved THEN Min(Budget - Overhead, Budget - Floor)
    ELSE Budget - Floor
FitsEmpty(r) == Size(r) <= EmptyAdmittable

(***************************************************************************)
(* The control plane's gate: the brick's last report less every pending    *)
(* claim against it, each charging need plus the per-VM overhead.          *)
(***************************************************************************)
ClaimsOn(b) == {r \in Requests : claim[b][r] # "none"}
Pending(b)  == SumSize(ClaimsOn(b)) + Overhead * Cardinality(ClaimsOn(b))
ViewAdmits(b, r) == view[b] - Pending(b) >= Size(r)

TypeOK ==
    /\ req \in [Requests -> ReqStates]
    /\ at \in [Requests -> Bricks \cup {NULL}]
    /\ frontier \in [Requests -> SUBSET Bricks]
    /\ claim \in [Bricks -> [Requests -> ClaimStates]]
    /\ view \in [Bricks -> Int]
    /\ boot \in [Requests -> BootStates]
    /\ resident \in [Requests -> Nat]
    /\ \A r \in Requests : resident[r] <= Size(r)
    \* The Go counters are unsigned; Int lets TicketsBalanced, not TypeOK,
    \* name a double release.
    /\ inflightMib \in [Bricks -> Int]
    /\ inflightVMs \in [Bricks -> Int]
    /\ bootFails \in 0..MaxBootFails

(***************************************************************************)
(* SAFETY                                                                  *)
(***************************************************************************)

\* The reserved model's promise: the declared memory of every VM in the
\* live map plus every boot holding a ticket, each with its overhead, fits
\* the budget. Each VM counts once even in the "landed" instant.
NoOvercommit ==
    \A b \in Bricks :
        SumSize(CommittedOn(b)) + Overhead * Cardinality(CommittedOn(b))
            <= Budget

\* The physical property the 2026-10-10 bricks lost: resident guest RAM
\* plus per-VM overhead never exceeds the budget. Under A3 it follows from
\* NoOvercommit; under the observed model it does not.
ResidentWithinBudget ==
    \A b \in Bricks : ResidentMib(b) <= Budget

\* No CP claim outlives the boot RPC it guards. An unsettled row exists only
\* while its request's RPC to that brick is outstanding, so a placed,
\* refused, failed or denied request never leaves one behind; a held row is
\* only the settled hold of a VM that booted there, which Expire drops
\* (ClaimsDrain checks that it does).
NoLeakedClaim ==
    \A b \in Bricks, r \in Requests :
        /\ claim[b][r] = "unsettled" => req[r] = "rpc" /\ at[r] = b
        /\ claim[b][r] = "held" => req[r] \in {"running", "done"} /\ at[r] = b

\* Every ticket is taken and released exactly once: noded's two counters,
\* moved by += and -= in the actions, always equal the boots that hold a
\* ticket. A double release or a missed one breaks the equality.
TicketsBalanced ==
    \A b \in Bricks :
        /\ inflightMib[b] = SumSize(TicketsOn(b))
        /\ inflightVMs[b] = Cardinality(TicketsOn(b))

\* Safety reformulation of EventuallyPlaced: a refusal never terminally
\* fails a request that an empty brick would admit. Under the shipped
\* protocol a refusal is always walked past or retried, so no request ever
\* reaches "failed"; with both retry flags off a single refusal strands it.
NoStrandedRequest ==
    \A r \in Requests : req[r] = "failed" => ~FitsEmpty(r)

(***************************************************************************)
(* ACTIONS                                                                 *)
(***************************************************************************)

Init ==
    /\ req = [r \in Requests |-> "waiting"]
    /\ at = [r \in Requests |-> NULL]
    /\ frontier = [r \in Requests |-> {}]
    /\ claim = [b \in Bricks |-> [r \in Requests |-> "none"]]
    /\ view = [b \in Bricks |-> EmptyAdmittable]
    /\ boot = [r \in Requests |-> "idle"]
    /\ resident = [r \in Requests |-> 0]
    /\ inflightMib = [b \in Bricks |-> 0]
    /\ inflightVMs = [b \in Bricks |-> 0]
    /\ bootFails = 0

\* The CP scores the bricks its (possibly stale) view says admit r, net of
\* pending claims, claims the head, and keeps the rest as the frontier. An
\* empty candidate set is the retryable :capacity denial: r stays waiting.
Place(r) ==
    /\ req[r] = "waiting"
    /\ LET F == {b \in Bricks : ViewAdmits(b, r)} IN
         \E b \in F :
            /\ req' = [req EXCEPT ![r] = "rpc"]
            /\ at' = [at EXCEPT ![r] = b]
            /\ frontier' = [frontier EXCEPT ![r] = F \ {b}]
            /\ claim' = [claim EXCEPT ![b][r] = ClaimRow(r)]
    /\ UNCHANGED <<view, boot, resident, inflightMib, inflightVMs, bootFails>>

\* noded's atomic gate: check and reserve under one mutex.
Admit(r) ==
    /\ AtomicAdmit
    /\ req[r] = "rpc" /\ boot[r] = "idle"
    /\ LET b == at[r] IN
         IF Size(r) <= Admittable(b)
         THEN /\ boot' = [boot EXCEPT ![r] = "ticket"]
              /\ inflightMib' = [inflightMib EXCEPT ![b] = @ + Size(r)]
              /\ inflightVMs' = [inflightVMs EXCEPT ![b] = @ + 1]
         ELSE /\ boot' = [boot EXCEPT ![r] = "refused"]
              /\ UNCHANGED <<inflightMib, inflightVMs>>
    /\ UNCHANGED <<req, at, frontier, claim, view, resident, bootFails>>

\* NEGATIVE (AtomicAdmit = FALSE): the check reads the facts and drops the
\* mutex; Reserve takes the ticket later. Two boots can pass one reading.
Check(r) ==
    /\ ~AtomicAdmit
    /\ req[r] = "rpc" /\ boot[r] = "idle"
    /\ boot' = [boot EXCEPT ![r] =
                  IF Size(r) <= Admittable(at[r]) THEN "checked" ELSE "refused"]
    /\ UNCHANGED <<req, at, frontier, claim, view, resident, inflightMib,
                   inflightVMs, bootFails>>

Reserve(r) ==
    /\ boot[r] = "checked"
    /\ boot' = [boot EXCEPT ![r] = "ticket"]
    /\ inflightMib' = [inflightMib EXCEPT ![at[r]] = @ + Size(r)]
    /\ inflightVMs' = [inflightVMs EXCEPT ![at[r]] = @ + 1]
    /\ UNCHANGED <<req, at, frontier, claim, view, resident, bootFails>>

\* The driver boot call returns and the VM joins the live map. A restored
\* guest has faulted none of its RAM in yet (LazyGrowth); otherwise it is
\* charged in full at once.
Land(r) ==
    /\ boot[r] = "ticket"
    /\ boot' = [boot EXCEPT ![r] = "landed"]
    /\ resident' = [resident EXCEPT ![r] = IF LazyGrowth THEN 0 ELSE Size(r)]
    /\ UNCHANGED <<req, at, frontier, claim, view, inflightMib, inflightVMs,
                   bootFails>>

ReleaseTicket(r) ==
    /\ boot[r] = "landed"
    /\ boot' = [boot EXCEPT ![r] = "live"]
    /\ inflightMib' = [inflightMib EXCEPT ![at[r]] = @ - Size(r)]
    /\ inflightVMs' = [inflightVMs EXCEPT ![at[r]] = @ - 1]
    /\ UNCHANGED <<req, at, frontier, claim, view, resident, bootFails>>

BootFail(r) ==
    /\ boot[r] = "ticket"
    /\ bootFails < MaxBootFails
    /\ boot' = [boot EXCEPT ![r] = "failed"]
    /\ inflightMib' = [inflightMib EXCEPT ![at[r]] = @ - Size(r)]
    /\ inflightVMs' = [inflightVMs EXCEPT ![at[r]] = @ - 1]
    /\ bootFails' = bootFails + 1
    /\ UNCHANGED <<req, at, frontier, claim, view, resident>>

Grow(r) ==
    /\ LazyGrowth
    /\ boot[r] \in {"landed", "live"}
    /\ resident[r] < Size(r)
    /\ resident' = [resident EXCEPT ![r] = Size(r)]
    /\ UNCHANGED <<req, at, frontier, claim, view, boot, inflightMib,
                   inflightVMs, bootFails>>

\* The boot RPC answers success: settle the claim into a short hold.
Booted(r) ==
    /\ req[r] = "rpc" /\ boot[r] = "live"
    /\ req' = [req EXCEPT ![r] = "running"]
    /\ frontier' = [frontier EXCEPT ![r] = {}]
    /\ claim' = [claim EXCEPT ![at[r]][r] =
                   IF r \in UnclaimedRequests THEN "none" ELSE "held"]
    /\ UNCHANGED <<at, view, boot, resident, inflightMib, inflightVMs, bootFails>>

\* The boot RPC answers RESOURCE_EXHAUSTED: release this brick's claim,
\* then walk on, deny retryably, or (both flags off) fail for good.
Refused(r) ==
    /\ req[r] = "rpc" /\ boot[r] = "refused"
    /\ boot' = [boot EXCEPT ![r] = "idle"]
    /\ IF WalkFrontier /\ frontier[r] # {}
       THEN \E b2 \in frontier[r] :
              /\ claim' = [claim EXCEPT ![at[r]][r] = "none",
                                        ![b2][r] = ClaimRow(r)]
              /\ at' = [at EXCEPT ![r] = b2]
              /\ frontier' = [frontier EXCEPT ![r] = @ \ {b2}]
              /\ UNCHANGED req
       ELSE /\ claim' = [claim EXCEPT ![at[r]][r] = "none"]
            /\ at' = [at EXCEPT ![r] = NULL]
            /\ frontier' = [frontier EXCEPT ![r] = {}]
            /\ req' = [req EXCEPT ![r] =
                         IF RetryableDenial THEN "waiting" ELSE "failed"]
    /\ UNCHANGED <<view, resident, inflightMib, inflightVMs, bootFails>>

BootFailed(r) ==
    /\ req[r] = "rpc" /\ boot[r] = "failed"
    /\ boot' = [boot EXCEPT ![r] = "idle"]
    /\ claim' = [claim EXCEPT ![at[r]][r] = "none"]
    /\ at' = [at EXCEPT ![r] = NULL]
    /\ frontier' = [frontier EXCEPT ![r] = {}]
    /\ req' = [req EXCEPT ![r] = "waiting"]
    /\ UNCHANGED <<view, resident, inflightMib, inflightVMs, bootFails>>

Destroy(r) ==
    /\ req[r] = "running" /\ boot[r] = "live"
    /\ req' = [req EXCEPT ![r] = "done"]
    /\ boot' = [boot EXCEPT ![r] = "gone"]
    /\ resident' = [resident EXCEPT ![r] = 0]
    /\ UNCHANGED <<at, frontier, claim, view, inflightMib, inflightVMs,
                   bootFails>>

Expire(b, r) ==
    /\ claim[b][r] = "held"
    /\ claim' = [claim EXCEPT ![b][r] = "none"]
    /\ UNCHANGED <<req, at, frontier, view, boot, resident, inflightMib,
                   inflightVMs, bootFails>>

Report(b) ==
    /\ view[b] # Admittable(b)
    /\ view' = [view EXCEPT ![b] = Admittable(b)]
    /\ UNCHANGED <<req, at, frontier, claim, boot, resident, inflightMib,
                   inflightVMs, bootFails>>

Next ==
    \/ \E r \in Requests :
         \/ Place(r) \/ Admit(r) \/ Check(r) \/ Reserve(r)
         \/ Land(r) \/ ReleaseTicket(r) \/ BootFail(r) \/ Grow(r)
         \/ Booted(r) \/ Refused(r) \/ BootFailed(r) \/ Destroy(r)
    \/ \E b \in Bricks :
         \/ Report(b)
         \/ \E r \in Requests : Expire(b, r)

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* LIVENESS                                                                *)
(*                                                                         *)
(* Every step that finishes work in flight is weakly fair, every VM is     *)
(* eventually destroyed, and every brick eventually reports. Place is      *)
(* strongly fair because other placements and reports can disable it       *)
(* repeatedly. Guest growth needs no fairness.                             *)
(***************************************************************************)
FairSpec ==
    /\ Spec
    /\ \A r \in Requests :
         /\ SF_vars(Place(r))
         /\ WF_vars(Admit(r)) /\ WF_vars(Check(r)) /\ WF_vars(Reserve(r))
         /\ WF_vars(Land(r)) /\ WF_vars(ReleaseTicket(r))
         /\ WF_vars(Booted(r)) /\ WF_vars(Refused(r)) /\ WF_vars(BootFailed(r))
         /\ WF_vars(Destroy(r))
    /\ \A b \in Bricks :
         /\ WF_vars(Report(b))
         /\ \A r \in Requests : WF_vars(Expire(b, r))

\* A refusal on one brick never strands a satisfiable request: every request
\* an empty brick would admit is eventually booted.
EventuallyPlaced ==
    \A r \in Requests : FitsEmpty(r) => <>(req[r] \in {"running", "done"})

\* Every claim row eventually disappears, held rows included.
ClaimsDrain ==
    \A b \in Bricks, r \in Requests :
        (claim[b][r] # "none") ~> (claim[b][r] = "none")

\* Every ticket is eventually released (the "at least once" half of
\* TicketsBalanced's "exactly once").
TicketsEventuallyReleased ==
    \A r \in Requests :
        (boot[r] \in {"ticket", "landed"}) ~> (boot[r] \notin {"ticket", "landed"})

=============================================================================
