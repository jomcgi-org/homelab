------------------------------- MODULE stateful -------------------------------
(*****************************************************************************)
(* Stateful lifecycle, checkpoint recovery, destroy redrive, and writable     *)
(* attach singleton model. This is pure TLA+, with no PlusCal translation.     *)
(*                                                                            *)
(* The model names every durable stateful op kind it covers so the layer-1    *)
(* vocabulary freshness guard stays exact: stateful_started,                  *)
(* stateful_published, stateful_banked, stateful_relit,                       *)
(* stateful_cold_booted, stateful_evicted, stateful_destroying,               *)
(* stateful_destroyed, stateful_failed, checkpoint_dispatched, and            *)
(* checkpoint_resolved. stateful_unpublished and stateful_stats remain        *)
(* excluded because unpublish is ETS-only and stats are observations.         *)
(*                                                                            *)
(* Concrete action map:                                                       *)
(*   FSMEdge             StatefulState.transition/2, all 11 states and all    *)
(*                       37 legal edges, including destroying -> failed.       *)
(*   BeginCheckpoint     StatefulSweeper begins interruptible bank.            *)
(*   CheckpointFails     finish_checkpoint error, bank_abort plus backoff.      *)
(*   CheckpointSucceeds  checkpoint_ready before resolve.                      *)
(*   CommitCheckpoint    checkpoint_resolved COMMIT, banked and detached.      *)
(*   AbortCheckpoint     checkpoint_resolved ABORT, hot serving and clear.     *)
(*   RequestDestroy      stateful_destroying intent.                           *)
(*   RedriveDestroy      owner-present stop, owner-absent confirmation, or     *)
(*                       bounded missing-owner escape.                         *)
(*   CrashVM             Firecracker death without normal detach.              *)
(*   CrashBrick          noded death losing registry and attach maps.          *)
(*   LoseOwnerReport     absent capacity testimony.                            *)
(*   OwnerReportsAbsent  live noded reports that the VM is gone.               *)
(*   ValidateRegistry    Firecracker API socket liveness probe.                 *)
(*   ReleaseAttach       volume.Manager.ReleaseOrphaned.                        *)
(*   Wake                StartStateful after singleton reclamation.             *)
(*****************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
    Workloads,
    NULL,
    MaxBackoff,
    AbsenceBudget,
    ExploreFSM,
    DestroyingEscapeEnabled,
    DestroyingEscapeState,
    RegistryValidationEnabled

States == {
    "starting", "serving", "banking", "checkpointed", "banked",
    "relighting", "cold_booting", "destroying", "evicted",
    "destroyed", "failed"
}

LiveStates == {
    "starting", "serving", "banking", "checkpointed", "relighting",
    "cold_booting", "destroying"
}

TerminalStates == {"evicted", "destroyed", "failed"}
OwnerReports == {"present", "absent", "missing"}
OwnerIds == {"old", "new"}
Epochs == 0..1

(* The complete implementation transition table. *)
Edges == {
    <<"starting", "publish", "serving">>,
    <<"serving", "unpublish", "banking">>,
    <<"serving", "bank", "banking">>,
    <<"banking", "bank_ready", "banked">>,
    <<"banking", "bank_abort", "serving">>,
    <<"banking", "checkpoint_ready", "checkpointed">>,
    <<"checkpointed", "commit", "banked">>,
    <<"checkpointed", "abort", "serving">>,
    <<"banked", "relight", "relighting">>,
    <<"relighting", "relight_ready", "starting">>,
    <<"relighting", "relight_abort", "banked">>,
    <<"banked", "cold_boot", "cold_booting">>,
    <<"cold_booting", "cold_ready", "starting">>,
    <<"cold_booting", "cold_abort", "banked">>,
    <<"banked", "evict", "evicted">>,
    <<"starting", "destroy", "destroyed">>,
    <<"serving", "destroy", "destroyed">>,
    <<"banking", "destroy", "destroyed">>,
    <<"checkpointed", "destroy", "destroyed">>,
    <<"banked", "destroy", "destroyed">>,
    <<"relighting", "destroy", "destroyed">>,
    <<"cold_booting", "destroy", "destroyed">>,
    <<"starting", "begin_destroy", "destroying">>,
    <<"serving", "begin_destroy", "destroying">>,
    <<"banking", "begin_destroy", "destroying">>,
    <<"checkpointed", "begin_destroy", "destroying">>,
    <<"banked", "begin_destroy", "destroying">>,
    <<"relighting", "begin_destroy", "destroying">>,
    <<"cold_booting", "begin_destroy", "destroying">>,
    <<"destroying", "destroy", "destroyed">>,
    <<"destroying", "fail", "failed">>,
    <<"starting", "fail", "failed">>,
    <<"serving", "fail", "failed">>,
    <<"banking", "fail", "failed">>,
    <<"checkpointed", "fail", "failed">>,
    <<"relighting", "fail", "failed">>,
    <<"cold_booting", "fail", "failed">>
}

NextBackoff(backoff) ==
    IF backoff = 0 THEN 1
    ELSE IF 2 * backoff > MaxBackoff THEN MaxBackoff
    ELSE 2 * backoff

VARIABLES
    instState,
    epoch,
    destroyingHistory,
    vmAlive,
    brickAlive,
    ownerReport,
    destroyConfirmed,
    absenceAge,
    attachOwner,
    registryVM,
    writers,
    wakeRequested,
    wakeSucceeded,
    bankBackoff,
    checkpointFailures

vars == <<
    instState, epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
    destroyConfirmed, absenceAge, attachOwner, registryVM, writers,
    wakeRequested, wakeSucceeded, bankBackoff, checkpointFailures
>>

Init ==
    /\ instState = [w \in Workloads |-> "serving"]
    /\ epoch = [w \in Workloads |-> 0]
    /\ destroyingHistory = {}
    /\ vmAlive = [w \in Workloads |-> TRUE]
    /\ brickAlive = [w \in Workloads |-> TRUE]
    /\ ownerReport = [w \in Workloads |-> "present"]
    /\ destroyConfirmed = [w \in Workloads |-> FALSE]
    /\ absenceAge = [w \in Workloads |-> 0]
    /\ attachOwner = [w \in Workloads |-> "old"]
    /\ registryVM = [w \in Workloads |-> "old"]
    /\ writers = [w \in Workloads |-> {"old"}]
    /\ wakeRequested = [w \in Workloads |-> TRUE]
    /\ wakeSucceeded = [w \in Workloads |-> FALSE]
    /\ bankBackoff = [w \in Workloads |-> 0]
    /\ checkpointFailures = [w \in Workloads |-> 0]

FSMEdge ==
    /\ ExploreFSM
    /\ \E w \in Workloads, edge \in Edges:
        /\ instState[w] = edge[1]
        /\ instState' = [instState EXCEPT ![w] = edge[3]]
        /\ destroyConfirmed' =
            IF edge[2] = "destroy"
            THEN [destroyConfirmed EXCEPT ![w] = TRUE]
            ELSE destroyConfirmed
        /\ destroyingHistory' =
            IF edge[3] = "destroying"
            THEN destroyingHistory \cup {<<w, epoch[w]>>}
            ELSE destroyingHistory
        /\ UNCHANGED <<
            epoch, vmAlive, brickAlive, ownerReport, absenceAge, attachOwner,
            registryVM, writers, wakeRequested, wakeSucceeded, bankBackoff,
            checkpointFailures
        >>

BeginCheckpoint(w) ==
    /\ ~ExploreFSM
    /\ instState[w] = "serving"
    /\ bankBackoff[w] = 0
    /\ instState' = [instState EXCEPT ![w] = "banking"]
    /\ UNCHANGED <<
        epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
        destroyConfirmed, absenceAge, attachOwner, registryVM, writers,
        wakeRequested, wakeSucceeded, bankBackoff, checkpointFailures
    >>

CheckpointFails(w) ==
    /\ ~ExploreFSM
    /\ instState[w] = "banking"
    /\ instState' = [instState EXCEPT ![w] = "serving"]
    /\ bankBackoff' = [bankBackoff EXCEPT ![w] = NextBackoff(@)]
    /\ checkpointFailures' =
        [checkpointFailures EXCEPT ![w] = IF @ < MaxBackoff THEN @ + 1 ELSE @]
    /\ UNCHANGED <<
        epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
        destroyConfirmed, absenceAge, attachOwner, registryVM, writers,
        wakeRequested, wakeSucceeded
    >>

CheckpointSucceeds(w) ==
    /\ ~ExploreFSM
    /\ instState[w] = "banking"
    /\ instState' = [instState EXCEPT ![w] = "checkpointed"]
    /\ UNCHANGED <<
        epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
        destroyConfirmed, absenceAge, attachOwner, registryVM, writers,
        wakeRequested, wakeSucceeded, bankBackoff, checkpointFailures
    >>

CommitCheckpoint(w) ==
    /\ ~ExploreFSM
    /\ instState[w] = "checkpointed"
    /\ instState' = [instState EXCEPT ![w] = "banked"]
    /\ vmAlive' = [vmAlive EXCEPT ![w] = FALSE]
    /\ attachOwner' = [attachOwner EXCEPT ![w] = NULL]
    /\ registryVM' = [registryVM EXCEPT ![w] = NULL]
    /\ writers' = [writers EXCEPT ![w] = {}]
    /\ bankBackoff' = [bankBackoff EXCEPT ![w] = 0]
    /\ UNCHANGED <<
        epoch, destroyingHistory, brickAlive, ownerReport, destroyConfirmed,
        absenceAge, wakeRequested, wakeSucceeded, checkpointFailures
    >>

AbortCheckpoint(w) ==
    /\ ~ExploreFSM
    /\ instState[w] = "checkpointed"
    /\ instState' = [instState EXCEPT ![w] = "serving"]
    /\ bankBackoff' = [bankBackoff EXCEPT ![w] = 0]
    /\ UNCHANGED <<
        epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
        destroyConfirmed, absenceAge, attachOwner, registryVM, writers,
        wakeRequested, wakeSucceeded, checkpointFailures
    >>

RequestDestroy(w) ==
    /\ instState[w] \in (LiveStates \cup {"banked"})
    /\ instState[w] # "destroying"
    /\ instState' = [instState EXCEPT ![w] = "destroying"]
    /\ destroyingHistory' = destroyingHistory \cup {<<w, epoch[w]>>}
    /\ absenceAge' = [absenceAge EXCEPT ![w] = 0]
    /\ UNCHANGED <<
        epoch, vmAlive, brickAlive, ownerReport, destroyConfirmed, attachOwner,
        registryVM, writers, wakeRequested, wakeSucceeded, bankBackoff,
        checkpointFailures
    >>

CrashVM(w) ==
    /\ vmAlive[w]
    /\ vmAlive' = [vmAlive EXCEPT ![w] = FALSE]
    /\ writers' = [writers EXCEPT ![w] = {}]
    /\ ownerReport' = [ownerReport EXCEPT ![w] = "missing"]
    /\ UNCHANGED <<
        instState, epoch, destroyingHistory, brickAlive, destroyConfirmed,
        absenceAge, attachOwner, registryVM, wakeRequested, wakeSucceeded,
        bankBackoff, checkpointFailures
    >>

CrashBrick(w) ==
    /\ brickAlive[w]
    /\ brickAlive' = [brickAlive EXCEPT ![w] = FALSE]
    /\ vmAlive' = [vmAlive EXCEPT ![w] = FALSE]
    /\ ownerReport' = [ownerReport EXCEPT ![w] = "missing"]
    /\ registryVM' = [registryVM EXCEPT ![w] = NULL]
    /\ attachOwner' = [attachOwner EXCEPT ![w] = NULL]
    /\ writers' = [writers EXCEPT ![w] = {}]
    /\ UNCHANGED <<
        instState, epoch, destroyingHistory, destroyConfirmed, absenceAge,
        wakeRequested, wakeSucceeded, bankBackoff, checkpointFailures
    >>

LoseOwnerReport(w) ==
    /\ ~vmAlive[w]
    /\ ownerReport[w] # "missing"
    /\ ownerReport' = [ownerReport EXCEPT ![w] = "missing"]
    /\ UNCHANGED <<
        instState, epoch, destroyingHistory, vmAlive, brickAlive,
        destroyConfirmed, absenceAge, attachOwner, registryVM, writers,
        wakeRequested, wakeSucceeded, bankBackoff, checkpointFailures
    >>

OwnerReportsAbsent(w) ==
    /\ brickAlive[w]
    /\ ~vmAlive[w]
    /\ ownerReport[w] # "absent"
    /\ ownerReport' = [ownerReport EXCEPT ![w] = "absent"]
    /\ registryVM' = [registryVM EXCEPT ![w] = NULL]
    /\ UNCHANGED <<
        instState, epoch, destroyingHistory, vmAlive, brickAlive,
        destroyConfirmed, absenceAge, attachOwner, writers, wakeRequested,
        wakeSucceeded, bankBackoff, checkpointFailures
    >>

Sweep(w) ==
    /\ \/ /\ instState[w] = "destroying"
           /\ ownerReport[w] = "missing"
           /\ absenceAge[w] < AbsenceBudget
       \/ bankBackoff[w] > 0
    /\ absenceAge' =
        IF instState[w] = "destroying" /\ ownerReport[w] = "missing" /\ absenceAge[w] < AbsenceBudget
        THEN [absenceAge EXCEPT ![w] = @ + 1]
        ELSE absenceAge
    /\ bankBackoff' =
        IF bankBackoff[w] > 0
        THEN [bankBackoff EXCEPT ![w] = @ - 1]
        ELSE bankBackoff
    /\ UNCHANGED <<
        instState, epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
        destroyConfirmed, attachOwner, registryVM, writers, wakeRequested,
        wakeSucceeded, checkpointFailures
    >>

RedriveDestroy(w) ==
    /\ instState[w] = "destroying"
    /\ \/ /\ ownerReport[w] = "present"
           /\ vmAlive[w]
           /\ instState' = [instState EXCEPT ![w] = "destroyed"]
           /\ vmAlive' = [vmAlive EXCEPT ![w] = FALSE]
           /\ destroyConfirmed' = [destroyConfirmed EXCEPT ![w] = TRUE]
           /\ attachOwner' = [attachOwner EXCEPT ![w] = NULL]
           /\ registryVM' = [registryVM EXCEPT ![w] = NULL]
           /\ writers' = [writers EXCEPT ![w] = {}]
       \/ /\ ownerReport[w] = "absent"
           /\ instState' = [instState EXCEPT ![w] = "destroyed"]
           /\ destroyConfirmed' = [destroyConfirmed EXCEPT ![w] = TRUE]
           /\ UNCHANGED <<vmAlive, attachOwner, registryVM, writers>>
       \/ /\ ownerReport[w] = "missing"
           /\ absenceAge[w] = AbsenceBudget
           /\ DestroyingEscapeEnabled
           /\ instState' = [instState EXCEPT ![w] = DestroyingEscapeState]
           /\ UNCHANGED <<vmAlive, destroyConfirmed, attachOwner, registryVM, writers>>
    /\ UNCHANGED <<
        epoch, destroyingHistory, brickAlive, ownerReport, absenceAge,
        wakeRequested, wakeSucceeded, bankBackoff, checkpointFailures
    >>

ValidateRegistry(w) ==
    /\ RegistryValidationEnabled
    /\ registryVM[w] # NULL
    /\ ~vmAlive[w]
    /\ registryVM' = [registryVM EXCEPT ![w] = NULL]
    /\ UNCHANGED <<
        instState, epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
        destroyConfirmed, absenceAge, attachOwner, writers, wakeRequested,
        wakeSucceeded, bankBackoff, checkpointFailures
    >>

ReleaseAttach(w) ==
    /\ instState[w] \in TerminalStates
    /\ attachOwner[w] # NULL
    /\ registryVM[w] # attachOwner[w]
    /\ attachOwner' = [attachOwner EXCEPT ![w] = NULL]
    /\ UNCHANGED <<
        instState, epoch, destroyingHistory, vmAlive, brickAlive, ownerReport,
        destroyConfirmed, absenceAge, registryVM, writers, wakeRequested,
        wakeSucceeded, bankBackoff, checkpointFailures
    >>

Wake(w) ==
    /\ instState[w] \in TerminalStates
    /\ wakeRequested[w]
    /\ ~wakeSucceeded[w]
    /\ attachOwner[w] = NULL
    /\ epoch[w] = 0
    /\ instState' = [instState EXCEPT ![w] = "starting"]
    /\ epoch' = [epoch EXCEPT ![w] = 1]
    /\ vmAlive' = [vmAlive EXCEPT ![w] = TRUE]
    /\ brickAlive' = [brickAlive EXCEPT ![w] = TRUE]
    /\ ownerReport' = [ownerReport EXCEPT ![w] = "present"]
    /\ destroyConfirmed' = [destroyConfirmed EXCEPT ![w] = FALSE]
    /\ absenceAge' = [absenceAge EXCEPT ![w] = 0]
    /\ attachOwner' = [attachOwner EXCEPT ![w] = "new"]
    /\ registryVM' = [registryVM EXCEPT ![w] = "new"]
    /\ writers' = [writers EXCEPT ![w] = {"new"}]
    /\ wakeSucceeded' = [wakeSucceeded EXCEPT ![w] = TRUE]
    /\ UNCHANGED <<destroyingHistory, wakeRequested, bankBackoff, checkpointFailures>>

Next ==
    \/ FSMEdge
    \/ \E w \in Workloads:
        \/ BeginCheckpoint(w)
        \/ CheckpointFails(w)
        \/ CheckpointSucceeds(w)
        \/ CommitCheckpoint(w)
        \/ AbortCheckpoint(w)
        \/ RequestDestroy(w)
        \/ CrashVM(w)
        \/ CrashBrick(w)
        \/ LoseOwnerReport(w)
        \/ OwnerReportsAbsent(w)
        \/ Sweep(w)
        \/ RedriveDestroy(w)
        \/ ValidateRegistry(w)
        \/ ReleaseAttach(w)
        \/ Wake(w)

Spec == Init /\ [][Next]_vars

FairSpec ==
    /\ Spec
    /\ \A w \in Workloads:
        /\ SF_vars(RequestDestroy(w))
        /\ WF_vars(Sweep(w))
        /\ SF_vars(RedriveDestroy(w))
        /\ WF_vars(ValidateRegistry(w))
        /\ WF_vars(ReleaseAttach(w))
        /\ WF_vars(Wake(w))

TypeOK ==
    /\ Workloads # {}
    /\ MaxBackoff \in 1..4
    /\ AbsenceBudget \in 1..3
    /\ ExploreFSM \in BOOLEAN
    /\ DestroyingEscapeEnabled \in BOOLEAN
    /\ DestroyingEscapeState \in {"failed", "destroyed"}
    /\ RegistryValidationEnabled \in BOOLEAN
    /\ Cardinality(Edges) = 37
    /\ instState \in [Workloads -> States]
    /\ epoch \in [Workloads -> Epochs]
    /\ destroyingHistory \subseteq (Workloads \X Epochs)
    /\ vmAlive \in [Workloads -> BOOLEAN]
    /\ brickAlive \in [Workloads -> BOOLEAN]
    /\ ownerReport \in [Workloads -> OwnerReports]
    /\ destroyConfirmed \in [Workloads -> BOOLEAN]
    /\ absenceAge \in [Workloads -> 0..AbsenceBudget]
    /\ attachOwner \in [Workloads -> (OwnerIds \cup {NULL})]
    /\ registryVM \in [Workloads -> (OwnerIds \cup {NULL})]
    /\ writers \in [Workloads -> SUBSET OwnerIds]
    /\ wakeRequested \in [Workloads -> BOOLEAN]
    /\ wakeSucceeded \in [Workloads -> BOOLEAN]
    /\ bankBackoff \in [Workloads -> 0..MaxBackoff]
    /\ checkpointFailures \in [Workloads -> 0..MaxBackoff]

NoResurrection ==
    \A w \in Workloads:
        <<w, epoch[w]>> \in destroyingHistory =>
            (instState[w] = "destroying" \/ instState[w] \notin LiveStates)

SingleWritableAttach ==
    \A w \in Workloads: Cardinality(writers[w]) <= 1

WriterHasAttach ==
    \A w \in Workloads: writers[w] # {} => attachOwner[w] # NULL

NoDestroyBeforeConfirm ==
    \A w \in Workloads: instState[w] = "destroyed" => destroyConfirmed[w]

DestroyIntentPrecedesRecord ==
    \A w \in Workloads:
        instState[w] = "destroying" => ~destroyConfirmed[w]

EventuallyTerminal ==
    \A w \in Workloads:
        (instState[w] \in LiveStates) ~> (instState[w] \in TerminalStates)

TerminalAttachEventuallyReleased ==
    \A w \in Workloads:
        (instState[w] \in TerminalStates /\ attachOwner[w] # NULL)
            ~> (attachOwner[w] = NULL)

TerminalWorkloadEventuallyWakes ==
    \A w \in Workloads:
        (instState[w] \in TerminalStates /\ wakeRequested[w])
            ~> wakeSucceeded[w]

=============================================================================
