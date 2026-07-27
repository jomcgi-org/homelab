------------------------------ MODULE quota ------------------------------
(***************************************************************************)
(* EmberVM per-principal daily quota gate (ADR embervm/006 protocol 3).    *)
(*                                                                         *)
(* The implementation has two gates, both calling Metering.within_quota?/4: *)
(* the router submit gate and Dispatcher.fq_take/6. Quota is opt-in: a      *)
(* principal with no configured budget is allowed. For a principal with a  *)
(* budget, an unreadable public ETS cache fails closed. The durable usage    *)
(* projection is truth; ETS is an advisory read-through cache rebuilt from  *)
(* that projection after the supervised Metering owner restarts.            *)
(*                                                                         *)
(* PROSE MAP: each PlusCal action abstracts a concrete implementation site.  *)
(*                                                                         *)
(*   Submit          ~ the router's within_quota?/4 check and its 429        *)
(*                     quota_enforced op-log audit append; denied requests  *)
(*                     are not enqueued (router submit path).               *)
(*   DispatchTick    ~ Dispatcher.over_budget_principals/2 and fq_take/6:    *)
(*                     an over-budget principal is skipped and remains      *)
(*                     queued, rather than being failed or audited.         *)
(*   Complete        ~ TaskStore completion hook: the succeeded/failed op    *)
(*                     with usage lands durably first, then on_metered/2     *)
(*                     calls charge/4 to bump ETS when the table exists.    *)
(*   MeteringCrash   ~ the supervised Metering singleton owner dies, so its  *)
(*                     public named ETS table disappears.                  *)
(*   MeteringRestart ~ Metering init/rebuild/1 recreates the table from the  *)
(*                     durable usage projection.                            *)
(*   DayFlip         ~ day_of/1 crossing the UTC epoch-day boundary and the *)
(*                     hourly prune/1; this model starts a new day's row.   *)
(*                                                                         *)
(* Abstractions: Principals has two model values, every task costs uniform   *)
(* TaskCost = 1 budget unit, and the cfg bounds submits, crashes, days,      *)
(* queue depth, and in-flight share. The implementation compares             *)
(* used_cpu_ms / 1000 < budget in FLOAT arithmetic; this model uses integer  *)
(* budget units. The model has one current-day row per principal and resets  *)
(* it at DayFlip instead of keying rows by {principal, day} and modeling the  *)
(* hourly prune as a separate action. Ghost variables below record history    *)
(* properties and are not implementation state.                             *)
(*                                                                         *)
(* quota_enforced is the submit-time durable audit kind. Dispatch-side       *)
(* skips are counters only and are deliberately not audited.                *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS
    Principals,
    Budgets,
    QuotaPrincipal,
    NULL,
    TaskCost,
    QueueDepthCap,
    InflightShare,
    MaxSubmits,
    MaxCrashes,
    MaxDays,
    DispatchGate

(*--fair algorithm quota

variables
    charged = [p \in Principals |-> 0],
    durable = [p \in Principals |-> 0],
    queued = [p \in Principals |-> 0],
    inflight = [p \in Principals |-> 0],
    cacheUp = TRUE,
    day = 0,
    submits = 0,
    crashes = 0,
    quotaEnforcedOps = 0,
    \* Ghost state: a configured principal dispatched while the cache was blind.
    dispatchedWhileBlind = [p \in Principals |-> 0],
    \* Ghost state: an unconfigured principal was denied or skipped.
    blockedWithoutBudget = [p \in Principals |-> 0],
    \* Ghost state: submit-time quota denials.
    submitDenials = 0;

define
    \* QuotaPrincipal identifies the one configured principal in these small cfgs;
    \* all other principals have the requested NULL, unconfigured budget value.
    Budget(p) == IF p = QuotaPrincipal THEN Budgets ELSE NULL

    WithinQuota(p) ==
        IF Budget(p) = NULL THEN TRUE
        ELSE IF ~cacheUp THEN FALSE
        ELSE charged[p] < Budget(p)

    TypeOK ==
        /\ charged \in [Principals -> Nat]
        /\ durable \in [Principals -> Nat]
        /\ queued \in [Principals -> Nat]
        /\ inflight \in [Principals -> Nat]
        /\ cacheUp \in BOOLEAN
        /\ day \in 0..MaxDays
        /\ submits \in 0..MaxSubmits
        /\ crashes \in 0..MaxCrashes
        /\ quotaEnforcedOps \in 0..MaxSubmits
        /\ dispatchedWhileBlind \in [Principals -> Nat]
        /\ blockedWithoutBudget \in [Principals -> Nat]
        /\ submitDenials \in 0..MaxSubmits
        /\ \A p \in Principals : Budget(p) \in Nat \cup {NULL}
        /\ \A p \in Principals : queued[p] <= QueueDepthCap
        /\ \A p \in Principals : inflight[p] <= InflightShare
        /\ Budgets \in Nat
        /\ TaskCost \in Nat /\ TaskCost > 0

    FailClosedDeny ==
        \A p \in Principals : dispatchedWhileBlind[p] = 0

    OptInNeverDenies ==
        \A p \in Principals : blockedWithoutBudget[p] = 0

    ZeroBudgetNeverDispatches ==
        \A p \in Principals : Budget(p) = 0 =>
            /\ inflight[p] = 0
            /\ durable[p] = 0

    CacheNeverExceedsDurable ==
        \A p \in Principals : charged[p] <= durable[p]

    OvershootBounded ==
        \A p \in Principals : Budget(p) # NULL =>
            durable[p] <= Budget(p) + InflightShare * TaskCost

    DenialAudited ==
        quotaEnforcedOps = submitDenials
end define;

begin
Run:
    while TRUE do
        either
            with p \in Principals do
                await submits < MaxSubmits /\ queued[p] < QueueDepthCap;
                submits := submits + 1;
                if WithinQuota(p) then
                    queued[p] := queued[p] + 1;
                else
                    submitDenials := submitDenials + 1;
                    quotaEnforcedOps := quotaEnforcedOps + 1;
                    if Budget(p) = NULL then
                        blockedWithoutBudget[p] := blockedWithoutBudget[p] + 1;
                    end if;
                end if;
            end with;

        or
            with p \in Principals do
                await queued[p] > 0 /\ inflight[p] < InflightShare;
                if DispatchGate /\ ~WithinQuota(p) then
                    if Budget(p) = NULL then
                        blockedWithoutBudget[p] := blockedWithoutBudget[p] + 1;
                    end if;
                else
                    queued[p] := queued[p] - 1;
                    inflight[p] := inflight[p] + 1;
                    if Budget(p) # NULL /\ ~cacheUp then
                        dispatchedWhileBlind[p] := dispatchedWhileBlind[p] + 1;
                    end if;
                end if;
            end with;

        or
            with p \in Principals do
                await inflight[p] > 0;
                inflight[p] := inflight[p] - 1;
                durable[p] := durable[p] + TaskCost;
                if cacheUp then
                    charged[p] := charged[p] + TaskCost;
                end if;
            end with;

        or
            with dummy \in {0} do
                await crashes < MaxCrashes /\ cacheUp;
                cacheUp := FALSE;
                charged := [p \in Principals |-> 0];
                crashes := crashes + 1;
            end with;

        or
            with dummy \in {0} do
                await ~cacheUp;
                cacheUp := TRUE;
                charged := durable;
            end with;

        or
            with dummy \in {0} do
                await day < MaxDays;
                day := day + 1;
                charged := [p \in Principals |-> 0];
                durable := [p \in Principals |-> 0];
            end with;
        end either;
    end while;
end algorithm; *)
\* BEGIN TRANSLATION (chksum(pcal) = "d6a43e9f" /\ chksum(tla) = "8483c795")
VARIABLES charged, durable, queued, inflight, cacheUp, day, submits, crashes, 
          quotaEnforcedOps, dispatchedWhileBlind, blockedWithoutBudget, 
          submitDenials

(* define statement *)
Budget(p) == IF p = QuotaPrincipal THEN Budgets ELSE NULL

WithinQuota(p) ==
    IF Budget(p) = NULL THEN TRUE
    ELSE IF ~cacheUp THEN FALSE
    ELSE charged[p] < Budget(p)

TypeOK ==
    /\ charged \in [Principals -> Nat]
    /\ durable \in [Principals -> Nat]
    /\ queued \in [Principals -> Nat]
    /\ inflight \in [Principals -> Nat]
    /\ cacheUp \in BOOLEAN
    /\ day \in 0..MaxDays
    /\ submits \in 0..MaxSubmits
    /\ crashes \in 0..MaxCrashes
    /\ quotaEnforcedOps \in 0..MaxSubmits
    /\ dispatchedWhileBlind \in [Principals -> Nat]
    /\ blockedWithoutBudget \in [Principals -> Nat]
    /\ submitDenials \in 0..MaxSubmits
    /\ \A p \in Principals : Budget(p) \in Nat \cup {NULL}
    /\ \A p \in Principals : queued[p] <= QueueDepthCap
    /\ \A p \in Principals : inflight[p] <= InflightShare
    /\ Budgets \in Nat
    /\ TaskCost \in Nat /\ TaskCost > 0

FailClosedDeny ==
    \A p \in Principals : dispatchedWhileBlind[p] = 0

OptInNeverDenies ==
    \A p \in Principals : blockedWithoutBudget[p] = 0

ZeroBudgetNeverDispatches ==
    \A p \in Principals : Budget(p) = 0 =>
        /\ inflight[p] = 0
        /\ durable[p] = 0

CacheNeverExceedsDurable ==
    \A p \in Principals : charged[p] <= durable[p]

OvershootBounded ==
    \A p \in Principals : Budget(p) # NULL =>
        durable[p] <= Budget(p) + InflightShare * TaskCost

DenialAudited ==
    quotaEnforcedOps = submitDenials


vars == << charged, durable, queued, inflight, cacheUp, day, submits, crashes, 
           quotaEnforcedOps, dispatchedWhileBlind, blockedWithoutBudget, 
           submitDenials >>

Init == (* Global variables *)
        /\ charged = [p \in Principals |-> 0]
        /\ durable = [p \in Principals |-> 0]
        /\ queued = [p \in Principals |-> 0]
        /\ inflight = [p \in Principals |-> 0]
        /\ cacheUp = TRUE
        /\ day = 0
        /\ submits = 0
        /\ crashes = 0
        /\ quotaEnforcedOps = 0
        /\ dispatchedWhileBlind = [p \in Principals |-> 0]
        /\ blockedWithoutBudget = [p \in Principals |-> 0]
        /\ submitDenials = 0

Next == \/ /\ \E p \in Principals:
                /\ submits < MaxSubmits /\ queued[p] < QueueDepthCap
                /\ submits' = submits + 1
                /\ IF WithinQuota(p)
                      THEN /\ queued' = [queued EXCEPT ![p] = queued[p] + 1]
                           /\ UNCHANGED << quotaEnforcedOps, 
                                           blockedWithoutBudget, 
                                           submitDenials >>
                      ELSE /\ submitDenials' = submitDenials + 1
                           /\ quotaEnforcedOps' = quotaEnforcedOps + 1
                           /\ IF Budget(p) = NULL
                                 THEN /\ blockedWithoutBudget' = [blockedWithoutBudget EXCEPT ![p] = blockedWithoutBudget[p] + 1]
                                 ELSE /\ TRUE
                                      /\ UNCHANGED blockedWithoutBudget
                           /\ UNCHANGED queued
           /\ UNCHANGED <<charged, durable, inflight, cacheUp, day, crashes, dispatchedWhileBlind>>
        \/ /\ \E p \in Principals:
                /\ queued[p] > 0 /\ inflight[p] < InflightShare
                /\ IF DispatchGate /\ ~WithinQuota(p)
                      THEN /\ IF Budget(p) = NULL
                                 THEN /\ blockedWithoutBudget' = [blockedWithoutBudget EXCEPT ![p] = blockedWithoutBudget[p] + 1]
                                 ELSE /\ TRUE
                                      /\ UNCHANGED blockedWithoutBudget
                           /\ UNCHANGED << queued, inflight, 
                                           dispatchedWhileBlind >>
                      ELSE /\ queued' = [queued EXCEPT ![p] = queued[p] - 1]
                           /\ inflight' = [inflight EXCEPT ![p] = inflight[p] + 1]
                           /\ IF Budget(p) # NULL /\ ~cacheUp
                                 THEN /\ dispatchedWhileBlind' = [dispatchedWhileBlind EXCEPT ![p] = dispatchedWhileBlind[p] + 1]
                                 ELSE /\ TRUE
                                      /\ UNCHANGED dispatchedWhileBlind
                           /\ UNCHANGED blockedWithoutBudget
           /\ UNCHANGED <<charged, durable, cacheUp, day, submits, crashes, quotaEnforcedOps, submitDenials>>
        \/ /\ \E p \in Principals:
                /\ inflight[p] > 0
                /\ inflight' = [inflight EXCEPT ![p] = inflight[p] - 1]
                /\ durable' = [durable EXCEPT ![p] = durable[p] + TaskCost]
                /\ IF cacheUp
                      THEN /\ charged' = [charged EXCEPT ![p] = charged[p] + TaskCost]
                      ELSE /\ TRUE
                           /\ UNCHANGED charged
           /\ UNCHANGED <<queued, cacheUp, day, submits, crashes, quotaEnforcedOps, dispatchedWhileBlind, blockedWithoutBudget, submitDenials>>
        \/ /\ \E dummy \in {0}:
                /\ crashes < MaxCrashes /\ cacheUp
                /\ cacheUp' = FALSE
                /\ charged' = [p \in Principals |-> 0]
                /\ crashes' = crashes + 1
           /\ UNCHANGED <<durable, queued, inflight, day, submits, quotaEnforcedOps, dispatchedWhileBlind, blockedWithoutBudget, submitDenials>>
        \/ /\ \E dummy \in {0}:
                /\ ~cacheUp
                /\ cacheUp' = TRUE
                /\ charged' = durable
           /\ UNCHANGED <<durable, queued, inflight, day, submits, crashes, quotaEnforcedOps, dispatchedWhileBlind, blockedWithoutBudget, submitDenials>>
        \/ /\ \E dummy \in {0}:
                /\ day < MaxDays
                /\ day' = day + 1
                /\ charged' = [p \in Principals |-> 0]
                /\ durable' = [p \in Principals |-> 0]
           /\ UNCHANGED <<queued, inflight, cacheUp, submits, crashes, quotaEnforcedOps, dispatchedWhileBlind, blockedWithoutBudget, submitDenials>>

Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(Next)

\* END TRANSLATION 
=============================================================================
