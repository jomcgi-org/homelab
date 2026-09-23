---------------------------- MODULE adoption_trace ----------------------------
(*****************************************************************************)
(* Tier B trace validation for adoption.tla (issue #6415). Staged,           *)
(* default-off: the S6 conformance scenario feeds exported SpecTrace windows *)
(* through this module via TLC, and the runner maps the outcome to           *)
(* pass / fail / vacuous / incomplete.                                       *)
(*                                                                           *)
(* SHAPE. A window is a finite sequence of observed records, one TLA+ record *)
(* per SpecTrace row. Every record carries the same eight fields so field    *)
(* access is total (TLC faults on a missing field); fields an action does    *)
(* not emit take neutral defaults at export time:                            *)
(*                                                                           *)
(*   [action |-> "prime", vm |-> "v1", node |-> "n1", session |-> "",         *)
(*    had_vm |-> FALSE, gate |-> FALSE, node_confirmed |-> FALSE,             *)
(*    confirmed_by |-> ""]                                                   *)
(*                                                                           *)
(* KNOWN NON-1:1 CASES (from the issue scope), handled explicitly:            *)
(*                                                                           *)
(*   Succeed is one spec action but two records. An observed "succeed" record *)
(*   is the first half of the destroy (the durable intent, mirroring          *)
(*   BeginDestroy): IsIntent treats "succeed" and "begin_destroy" alike, so   *)
(*   a confirm preceded only by a succeed still satisfies intent precedence.  *)
(*                                                                           *)
(*   Checkpoint is a synthetic observation, not a spec action. It constrains  *)
(*   nothing here; it contributes coverage (a window of only checkpoints is   *)
(*   still thin, see VacuousWindow) and carries the node-testimony fields    *)
(*   the Tier A checker reads.                                               *)
(*                                                                           *)
(*   crash_cp, crash_node and send_status are unobservable in principle. They *)
(*   are allowed as hidden steps between observed records: the replay consumes*)
(*   every record in order, hidden actions impose no ordering constraint, and *)
(*   the bound is the window itself (Len(Trace)), so TLC always terminates.  *)
(*                                                                           *)
(* COMPOSITION NOTE. tlc.sh stages exactly one spec plus one cfg, so this    *)
(* module is self-contained and does NOT extend adoption.tla. The two        *)
(* ordering predicates below mirror Tier A checker predicates over the same  *)
(* record vocabulary (checker.ex check_no_destroy_before_confirm and         *)
(* check_no_double_assign): a window TLC accepts here is a path the hand     *)
(* checker also accepts, and a window TLC rejects names the violated         *)
(* invariant for the S6 fail verdict. Full composition with adoption.tla's   *)
(* Next remains future work once the dev lane produces live windows.         *)
(*                                                                           *)
(* VACUITY. VacuousWindow is an operator, not an invariant: a thin window    *)
(* satisfies every ordering predicate, so TLC alone would PASS it. The S6    *)
(* runner checks VacuousWindow FIRST and returns vacuous without running     *)
(* TLC. Likewise a truncated TLC run (no "0 states left on queue" line) is   *)
(* INCOMPLETE, never pass; that gate lives in the tlc.sh driver and the S6   *)
(* output classifier, not here.                                              *)
(*****************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Trace,          \* the exported SpecTrace window under check
    MinTraceEvents  \* vacuity threshold: windows shorter than this are vacuous

VARIABLES cursor

ReplayInit == cursor = 1

ReplayNext == cursor <= Len(Trace) /\ cursor' = cursor + 1

Spec == ReplayInit /\ [][ReplayNext]_cursor

\* Every action the control plane can emit into a window, plus the hidden
\* steps that are unobservable in principle but legal between observations.
KnownActions ==
    {"prime", "adopt_inventory", "dispatch_warm", "dispatch_miss",
     "dispatch_failed", "abandon_claim", "succeed", "checkpoint",
     "recv_status", "send_status", "age_to_unknown", "age_to_down",
     "reconnect", "restart_cp", "crash_cp", "crash_node",
     "begin_destroy", "confirm_destroy"}

TraceWellFormed ==
    \A i \in 1..Len(Trace) : Trace[i].action \in KnownActions

IsDispatch(r) == r.action = "dispatch_warm" \/ r.action = "dispatch_miss"

\* Succeed is the first half of the destroy (durable intent alongside
\* begin_destroy), so a confirm preceded only by a succeed still has intent.
IsIntent(r) == r.action = "begin_destroy" \/ r.action = "succeed"

IsConfirm(r) == r.action = "confirm_destroy"

\* A confirm is evaluable only on the gate-on path for a live VM, exactly the
\* Tier A guard: gate-off and snapshot confirms are vacuous, never violations.
IsEvaluableConfirm(r) == IsConfirm(r) /\ r.had_vm /\ r.gate

\* Node confirmation by teardown or absence, or the node-gone cessation proof
\* (#6004) where no confirmation can ever arrive.
ConfirmedOK(r) == r.node_confirmed \/ r.confirmed_by = "node_gone"

\* ADR embervm/014 decision 5 over the observed window: a gated destroy record
\* for a live VM appears only after the node confirmed teardown AND after the
\* durable intent (begin_destroy, or the succeed that refines it) for the same
\* session. A deliberately deviated window (destroyed before node
\* confirmation, or confirm ordered before any intent) violates this.
NoDestroyBeforeConfirm ==
    \A i \in 1..Len(Trace) :
        IsEvaluableConfirm(Trace[i]) =>
            /\ ConfirmedOK(Trace[i])
            /\ \E j \in 1..(i - 1) :
                IsIntent(Trace[j]) /\ Trace[j].session = Trace[i].session

\* Single-use vm_ids: a VM dispatched twice with no intervening consumption
\* (succeed or confirm_destroy, after which RecycleId may legitimately reuse
\* the slot) is a violation. Mirrors the Tier A no_double_assign predicate.
NoDoubleAssign ==
    \A i \in 1..Len(Trace) :
        IsDispatch(Trace[i]) =>
            \A j \in 1..(i - 1) :
                (IsDispatch(Trace[j]) /\ Trace[j].vm = Trace[i].vm) =>
                    \E k \in (j + 1)..(i - 1) :
                        (Trace[k].action = "succeed" \/ Trace[k].action = "confirm_destroy")
                        /\ Trace[k].vm = Trace[i].vm

\* Coverage is the window length. The runner reports it alongside the verdict
\* so "pass" always carries what it checked; a pass with zero coverage is
\* unrepresentable because thin windows never reach TLC (see VacuousWindow).
TraceCoverage == Len(Trace)

\* Thin windows satisfy every ordering predicate and must never read as pass.
\* The S6 runner evaluates this first and returns vacuous without TLC.
VacuousWindow == Len(Trace) < MinTraceEvents
=============================================================================
