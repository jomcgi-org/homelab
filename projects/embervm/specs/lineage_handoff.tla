----------------------------- MODULE lineage_handoff -----------------------------
(*****************************************************************************)
(* Session workspace lineage handoff, issue #4701.                           *)
(*                                                                           *)
(* This is a protocol model, not an implementation-conformance proof. The    *)
(* positive mode states the required handoff contract. Four source-grounded  *)
(* switches expose where the current implementation is weaker, and one       *)
(* isolated mutation proves the no-loss check observes deletion after export *)
(* initiation rather than completion. README.md records those limitations.    *)
(*                                                                           *)
(* Concrete action map:                                                       *)
(*   BeginDestroy       SessionStore :begin_exact_destroy / session_destroying*)
(*                      and the terminal session paths in SessionManager.      *)
(*   RecordRelinquish   NodeService.RetireVolume -> WriteRetirementIntent.     *)
(*   Terminalize        SessionStore.transition to expired, evicted,           *)
(*                      destroyed, or failed.                                  *)
(*   StartExport        RetireVolume enqueue and the async export worker.      *)
(*   CompleteExport     exportWithKeys success, before completeRetirement.     *)
(*   RemoveLocal        completeRetirement -> DeleteSession.                   *)
(*   BeginHeir          validate_restore_lineage plus the restore-in-flight    *)
(*                      claim in SessionManager.create.                        *)
(*   FinishHeir         RestoreArtifact, Prime, then register_and_start.        *)
(*   CrashCP/RecoverCP  SessionManager process loss and durable-store rebuild. *)
(*   CrashNode/RecoverNode noded process loss, retirement-intent sweep restart.*)
(*   ReconnectOld       the required generation comparison on reconnect.       *)
(*                                                                           *)
(* Abstraction limits: one lineage and one workspace are modeled; heirs are   *)
(* finite identities and generation numbers are bounded. A crash loses only   *)
(* process memory. Durable session rows, the retirement marker, the local      *)
(* volume, and a completed S3 object survive. CrashNode is therefore a daemon  *)
(* crash, not arbitrary disk loss. S3 completion is atomic at meta.json-last   *)
(* publication. The scheduler may interleave every enabled action and is not   *)
(* assumed fair. No assumption states any invariant: guards selected by the    *)
(* constants are the protocol under test, and negative configs remove one      *)
(* guard at a time. Network partitions, Byzantine stores, corrupt media, and   *)
(* simultaneous physical loss of local storage and S3 are outside scope.       *)
(*****************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
    Heirs,
    NULL,
    MaxGeneration,
    MaxCPCrashes,
    MaxNodeCrashes,
    RequireExportCompletion,
    RequireDurableRelinquish,
    DurableHeirClaim,
    RequireTerminalPredecessor,
    CompareGenerationOnReconnect

InitialHeir == CHOOSE h \in Heirs : TRUE
OtherHeirs == Heirs \ {InitialHeir}

HolderStates == {"absent", "live", "destroying", "terminal"}
ExportPhases == {"none", "started", "complete"}
Generations == 0..MaxGeneration

VARIABLES
    holderState,
    holderGeneration,
    latestHolder,
    lineageGeneration,
    attempts,
    attemptBase,
    volatileClaims,
    durableClaim,
    relinquished,
    localExists,
    localGeneration,
    exportPhase,
    exportGeneration,
    cpAlive,
    nodeAlive,
    cpCrashes,
    nodeCrashes,
    inheritedFromNonterminal,
    handoffWithoutRelinquish,
    divergenceDetected,
    silentMerge

vars == <<
    holderState, holderGeneration, latestHolder, lineageGeneration,
    attempts, attemptBase, volatileClaims, durableClaim, relinquished,
    localExists, localGeneration, exportPhase, exportGeneration,
    cpAlive, nodeAlive, cpCrashes, nodeCrashes,
    inheritedFromNonterminal, handoffWithoutRelinquish,
    divergenceDetected, silentMerge
>>

Init ==
    /\ Cardinality(Heirs) >= 3
    /\ MaxGeneration >= 2
    /\ holderState = [h \in Heirs |-> IF h = InitialHeir THEN "live" ELSE "absent"]
    /\ holderGeneration = [h \in Heirs |-> IF h = InitialHeir THEN 1 ELSE 0]
    /\ latestHolder = InitialHeir
    /\ lineageGeneration = 1
    /\ attempts = {}
    /\ attemptBase = [h \in Heirs |-> 0]
    /\ volatileClaims = {}
    /\ durableClaim = NULL
    /\ relinquished = FALSE
    /\ localExists = TRUE
    /\ localGeneration = 1
    /\ exportPhase = "none"
    /\ exportGeneration = 0
    /\ cpAlive = TRUE
    /\ nodeAlive = TRUE
    /\ cpCrashes = 0
    /\ nodeCrashes = 0
    /\ inheritedFromNonterminal = FALSE
    /\ handoffWithoutRelinquish = FALSE
    /\ divergenceDetected = FALSE
    /\ silentMerge = FALSE

BeginDestroy(h) ==
    /\ cpAlive
    /\ holderState[h] = "live"
    /\ holderState' = [holderState EXCEPT ![h] = "destroying"]
    /\ UNCHANGED <<
        holderGeneration, latestHolder, lineageGeneration, attempts,
        attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, exportPhase, exportGeneration,
        cpAlive, nodeAlive, cpCrashes, nodeCrashes,
        inheritedFromNonterminal, handoffWithoutRelinquish,
        divergenceDetected, silentMerge
        >>

RecordRelinquish(h) ==
    /\ nodeAlive
    /\ holderState[h] = "destroying"
    /\ ~relinquished
    /\ relinquished' = TRUE
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim,
        localExists, localGeneration, exportPhase, exportGeneration,
        cpAlive, nodeAlive, cpCrashes, nodeCrashes,
        inheritedFromNonterminal, handoffWithoutRelinquish,
        divergenceDetected, silentMerge
        >>

Terminalize(h) ==
    /\ cpAlive
    /\ holderState[h] = "destroying"
    /\ (~RequireDurableRelinquish \/ relinquished)
    /\ holderState' = [holderState EXCEPT ![h] = "terminal"]
    /\ UNCHANGED <<
        holderGeneration, latestHolder, lineageGeneration, attempts,
        attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, exportPhase, exportGeneration,
        cpAlive, nodeAlive, cpCrashes, nodeCrashes,
        inheritedFromNonterminal, handoffWithoutRelinquish,
        divergenceDetected, silentMerge
        >>

StartExport ==
    /\ nodeAlive
    /\ relinquished
    /\ localExists
    /\ exportPhase # "started"
    /\ exportGeneration < localGeneration
    /\ exportPhase' = "started"
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, exportGeneration,
        cpAlive, nodeAlive, cpCrashes, nodeCrashes,
        inheritedFromNonterminal, handoffWithoutRelinquish,
        divergenceDetected, silentMerge
        >>

CompleteExport ==
    /\ nodeAlive
    /\ exportPhase = "started"
    /\ localExists
    /\ exportPhase' = "complete"
    /\ exportGeneration' = localGeneration
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, cpAlive, nodeAlive, cpCrashes,
        nodeCrashes, inheritedFromNonterminal, handoffWithoutRelinquish,
        divergenceDetected, silentMerge
        >>

RemoveLocal ==
    /\ nodeAlive
    /\ relinquished
    /\ localExists
    /\ IF RequireExportCompletion
          THEN exportPhase = "complete" /\ exportGeneration = localGeneration
          ELSE exportPhase = "started"
    /\ localExists' = FALSE
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim, relinquished,
        localGeneration, exportPhase, exportGeneration,
        cpAlive, nodeAlive, cpCrashes, nodeCrashes,
        inheritedFromNonterminal, handoffWithoutRelinquish,
        divergenceDetected, silentMerge
        >>

BeginHeir(h) ==
    /\ h \in OtherHeirs
    /\ cpAlive
    /\ holderState[h] = "absent"
    /\ h \notin attempts
    /\ lineageGeneration < MaxGeneration
    /\ localExists \/ exportPhase = "complete"
    /\ (~RequireTerminalPredecessor \/ holderState[latestHolder] = "terminal")
    /\ (~RequireDurableRelinquish \/ relinquished)
    /\ durableClaim = NULL
    /\ volatileClaims = {}
    /\ attempts' = attempts \cup {h}
    /\ attemptBase' = [attemptBase EXCEPT ![h] = lineageGeneration]
    /\ durableClaim' = IF DurableHeirClaim THEN h ELSE NULL
    /\ volatileClaims' = IF DurableHeirClaim THEN {} ELSE {h}
    /\ inheritedFromNonterminal' =
        (inheritedFromNonterminal \/ (holderState[latestHolder] # "terminal"))
    /\ handoffWithoutRelinquish' = (handoffWithoutRelinquish \/ ~relinquished)
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        localExists, localGeneration, exportPhase, exportGeneration,
        relinquished, cpAlive, nodeAlive, cpCrashes, nodeCrashes,
        divergenceDetected, silentMerge
        >>

FinishHeir(h) ==
    /\ h \in attempts
    /\ nodeAlive
    /\ localExists \/ exportPhase = "complete"
    /\ attemptBase[h] < MaxGeneration
    /\ holderState' = [holderState EXCEPT ![h] = "live"]
    /\ holderGeneration' =
        [holderGeneration EXCEPT ![h] = attemptBase[h] + 1]
    /\ latestHolder' = h
    /\ lineageGeneration' = attemptBase[h] + 1
    /\ attempts' = attempts \ {h}
    /\ volatileClaims' = volatileClaims \ {h}
    /\ durableClaim' = IF durableClaim = h THEN NULL ELSE durableClaim
    /\ relinquished' = FALSE
    /\ localExists' = TRUE
    /\ localGeneration' = attemptBase[h] + 1
    /\ UNCHANGED <<
        attemptBase, exportPhase, exportGeneration, cpAlive, nodeAlive,
        cpCrashes, nodeCrashes, inheritedFromNonterminal,
        handoffWithoutRelinquish, divergenceDetected, silentMerge
        >>

CrashCP ==
    /\ cpAlive
    /\ cpCrashes < MaxCPCrashes
    /\ cpAlive' = FALSE
    /\ cpCrashes' = cpCrashes + 1
    /\ volatileClaims' = {}
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, durableClaim, relinquished,
        localExists, localGeneration, exportPhase, exportGeneration,
        nodeAlive, nodeCrashes, inheritedFromNonterminal,
        handoffWithoutRelinquish, divergenceDetected, silentMerge
        >>

RecoverCP ==
    /\ ~cpAlive
    /\ cpAlive' = TRUE
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, exportPhase, exportGeneration,
        nodeAlive, cpCrashes, nodeCrashes, inheritedFromNonterminal,
        handoffWithoutRelinquish, divergenceDetected, silentMerge
        >>

CrashNode ==
    /\ nodeAlive
    /\ nodeCrashes < MaxNodeCrashes
    /\ nodeAlive' = FALSE
    /\ nodeCrashes' = nodeCrashes + 1
    /\ exportPhase' = IF exportPhase = "started" THEN "none" ELSE exportPhase
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, exportGeneration,
        cpAlive, cpCrashes, inheritedFromNonterminal,
        handoffWithoutRelinquish, divergenceDetected, silentMerge
        >>

RecoverNode ==
    /\ ~nodeAlive
    /\ nodeAlive' = TRUE
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, exportPhase, exportGeneration,
        cpAlive, cpCrashes, nodeCrashes, inheritedFromNonterminal,
        handoffWithoutRelinquish, divergenceDetected, silentMerge
        >>

ReconnectOld(h) ==
    /\ cpAlive
    /\ nodeAlive
    /\ holderState[h] = "terminal"
    /\ holderGeneration[h] < holderGeneration[latestHolder]
    /\ holderState[latestHolder] = "live"
    /\ IF CompareGenerationOnReconnect
          THEN divergenceDetected' = TRUE /\ UNCHANGED silentMerge
          ELSE silentMerge' = TRUE /\ UNCHANGED divergenceDetected
    /\ UNCHANGED <<
        holderState, holderGeneration, latestHolder, lineageGeneration,
        attempts, attemptBase, volatileClaims, durableClaim, relinquished,
        localExists, localGeneration, exportPhase, exportGeneration,
        cpAlive, nodeAlive, cpCrashes, nodeCrashes,
        inheritedFromNonterminal, handoffWithoutRelinquish
        >>

Next ==
    \/ \E h \in Heirs : BeginDestroy(h)
    \/ \E h \in Heirs : RecordRelinquish(h)
    \/ \E h \in Heirs : Terminalize(h)
    \/ StartExport
    \/ CompleteExport
    \/ RemoveLocal
    \/ \E h \in OtherHeirs : BeginHeir(h)
    \/ \E h \in OtherHeirs : FinishHeir(h)
    \/ CrashCP
    \/ RecoverCP
    \/ CrashNode
    \/ RecoverNode
    \/ \E h \in Heirs : ReconnectOld(h)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ holderState \in [Heirs -> HolderStates]
    /\ holderGeneration \in [Heirs -> Generations]
    /\ latestHolder \in Heirs
    /\ lineageGeneration \in Generations
    /\ attempts \subseteq OtherHeirs
    /\ attemptBase \in [Heirs -> Generations]
    /\ volatileClaims \subseteq OtherHeirs
    /\ durableClaim \in Heirs \cup {NULL}
    /\ relinquished \in BOOLEAN
    /\ localExists \in BOOLEAN
    /\ localGeneration \in Generations
    /\ exportPhase \in ExportPhases
    /\ exportGeneration \in Generations
    /\ cpAlive \in BOOLEAN
    /\ nodeAlive \in BOOLEAN
    /\ cpCrashes \in 0..MaxCPCrashes
    /\ nodeCrashes \in 0..MaxNodeCrashes
    /\ inheritedFromNonterminal \in BOOLEAN
    /\ handoffWithoutRelinquish \in BOOLEAN
    /\ divergenceDetected \in BOOLEAN
    /\ silentMerge \in BOOLEAN

WorkspaceAvailable == localExists \/ exportPhase = "complete"

NeverTwoLiveHeirs ==
    Cardinality({h \in Heirs : holderState[h] \in {"live", "destroying"}}) <= 1

TerminalPredecessorOnly == ~inheritedFromNonterminal

CommonAncestorDivergenceDetected == ~silentMerge

DurableRelinquishBeforeHandoff == ~handoffWithoutRelinquish

=============================================================================
