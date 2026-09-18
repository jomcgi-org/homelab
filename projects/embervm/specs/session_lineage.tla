--------------------------- MODULE session_lineage ---------------------------
(*****************************************************************************
(* Session workspace lineage adoption and handoff (issue #4701).             *)
(*                                                                           *)
(* This is a bounded abstract model of one lineage, one predecessor, two     *)
(* candidate heirs, two bricks, durable retirement/export state, and the     *)
(* reconnect comparison required by ARCHITECTURE.md's ownership-arbitration  *)
(* table. A daemon or control-plane crash may land between every durable     *)
(* step. Such crashes do not destroy node-local NVMe. Physical media loss    *)
(* before the first completed export is outside the claimed guarantee.       *)
(*                                                                           *)
(* ASSUMPTIONS. RetireVolume's marker and a completed S3 meta.json are        *)
(* durable across process crashes. A handoff claim is durable when            *)
(* ExclusiveHeirGuard is enabled. Reconnect comparison is against the        *)
(* recorded common-ancestor generation, not just equality of two scalar      *)
(* heads. The last two are protocol requirements, not claims that the current *)
(* implementation already conforms. The README records the source gaps.      *)
(*                                                                           *)
(* PROSE MAP: each action abstracts a concrete implementation site.           *)
(*                                                                           *)
(*   MutatePredecessor       ~ writes through the session workspace attached  *)
(*                             by noded volume.Manager.AttachLineage          *)
(*                             (volume/volume.go).                            *)
(*   TerminalizePredecessor  ~ session_expired, session_failed,               *)
(*                             session_evicted, or session_destroyed via      *)
(*                             SessionStore.transition (session_manager.ex).  *)
(*   WriteRelinquish         ~ RetireVolume writes .retirement-intent before  *)
(*                             enqueueing export (server/store.go).           *)
(*   BeginExport / CompleteExport                                             *)
(*                           ~ noded export queue; Store.Export writes files   *)
(*                             before meta.json and completeRetirement deletes *)
(*                             local bytes only after success (server/store.go,*)
(*                             noded/store/store.go).                         *)
(*   BeginInheritance(h)     ~ validate_restore_lineage plus the in-flight    *)
(*                             lineage exclusion before a session_created     *)
(*                             row exists (session_manager.ex).               *)
(*   ReleaseSource(h)        ~ node-owned retirement releases the predecessor *)
(*                             workspace before the heir is hydrated.         *)
(*   InstallReleasedHeir(h)  ~ RestoreArtifact then Prime and session_created *)
(*                             (or session_rejoined for an existing session)  *)
(*                             (session_manager.ex, noded/server/server.go).   *)
(*   InstallRemoteHeir(h)    ~ restore from S3 while a disconnected source    *)
(*                             brick retains its local common-ancestor copy.  *)
(*   MutateHeir(h)           ~ writes through the restored heir workspace.    *)
(*   MutateDisconnectedSource                                              *)
(*                           ~ a still-running VM writes on a disconnected    *)
(*                             brick; the silence gate bounds but does not    *)
(*                             kill live VMs (noded/server/server.go).         *)
(*   ReconnectSource         ~ NodeStatus reconnect plus the required         *)
(*                             common-ancestor generation comparison. No      *)
(*                             current session implementation site performs   *)
(*                             this comparison; see README suspected defect.  *)
(*   CrashCP / RestartCP     ~ SessionManager/control-plane restart. Worker   *)
(*                             effects may outlive its volatile MapSet.        *)
(*   CrashNode / RestartNode ~ noded process restart. Local NVMe survives.    *)
*****************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Heirs,
    Nodes,
    Predecessor,
    SourceNode,
    TargetNode,
    MaxGeneration,
    MaxCPCrashes,
    MaxNodeCrashes,
    SafeRelease,
    ExclusiveHeirGuard,
    TerminalPredecessorGuard,
    ReconnectComparison

Actors == Heirs \cup {Predecessor}
SessionStates == {"absent", "live", "terminal"}
ExportStates == {"none", "pending", "complete"}

VARIABLES
    status,
    cpAlive,
    nodeUp,
    localVolume,
    relinquished,
    exportState,
    exportGeneration,
    workers,
    durableClaims,
    volatileClaims,
    released,
    actorGeneration,
    commonGeneration,
    divergenceDetected,
    reconnectedDivergence,
    silentMerge,
    inheritedNonTerminal,
    cpCrashes,
    nodeCrashes

vars == <<status, cpAlive, nodeUp, localVolume, relinquished, exportState,
          exportGeneration, workers, durableClaims, volatileClaims, released,
          actorGeneration, commonGeneration, divergenceDetected,
          reconnectedDivergence, silentMerge, inheritedNonTerminal,
          cpCrashes, nodeCrashes>>

LiveHeirs == {h \in Heirs : status[h] = "live"}
LocalExists == \E n \in Nodes : localVolume[n]
ExportComplete == exportState = "complete"
Diverged(h) ==
    /\ actorGeneration[Predecessor] > commonGeneration[h]
    /\ actorGeneration[h] > commonGeneration[h]

TypeOK ==
    /\ status \in [Actors -> SessionStates]
    /\ cpAlive \in BOOLEAN
    /\ nodeUp \in [Nodes -> BOOLEAN]
    /\ localVolume \in [Nodes -> BOOLEAN]
    /\ relinquished \in BOOLEAN
    /\ exportState \in ExportStates
    /\ exportGeneration \in 0..MaxGeneration
    /\ workers \subseteq Heirs
    /\ durableClaims \subseteq Heirs
    /\ volatileClaims \subseteq Heirs
    /\ released \subseteq Heirs
    /\ actorGeneration \in [Actors -> 0..MaxGeneration]
    /\ commonGeneration \in [Heirs -> 0..MaxGeneration]
    /\ divergenceDetected \in BOOLEAN
    /\ reconnectedDivergence \in BOOLEAN
    /\ silentMerge \in BOOLEAN
    /\ inheritedNonTerminal \in BOOLEAN
    /\ cpCrashes \in 0..MaxCPCrashes
    /\ nodeCrashes \in 0..MaxNodeCrashes

NoWorkspaceLoss == LocalExists \/ ExportComplete

NeverTwoLiveHeirs == Cardinality(LiveHeirs) <= 1

InheritanceOnlyFromTerminal == ~inheritedNonTerminal

ReconnectDetectsDivergence ==
    /\ ~silentMerge
    /\ (reconnectedDivergence => divergenceDetected)

Init ==
    /\ status = [a \in Actors |-> IF a = Predecessor THEN "live" ELSE "absent"]
    /\ cpAlive = TRUE
    /\ nodeUp = [n \in Nodes |-> TRUE]
    /\ localVolume = [n \in Nodes |-> n = SourceNode]
    /\ relinquished = FALSE
    /\ exportState = "none"
    /\ exportGeneration = 0
    /\ workers = {}
    /\ durableClaims = {}
    /\ volatileClaims = {}
    /\ released = {}
    /\ actorGeneration = [a \in Actors |-> 0]
    /\ commonGeneration = [h \in Heirs |-> 0]
    /\ divergenceDetected = FALSE
    /\ reconnectedDivergence = FALSE
    /\ silentMerge = FALSE
    /\ inheritedNonTerminal = FALSE
    /\ cpCrashes = 0
    /\ nodeCrashes = 0

MutatePredecessor ==
    /\ status[Predecessor] = "live"
    /\ nodeUp[SourceNode]
    /\ localVolume[SourceNode]
    /\ actorGeneration[Predecessor] < MaxGeneration
    /\ actorGeneration' = [actorGeneration EXCEPT
                              ![Predecessor] = @ + 1]
    /\ UNCHANGED <<status, cpAlive, nodeUp, localVolume, relinquished,
                    exportState, exportGeneration, workers, durableClaims,
                    volatileClaims, released, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

TerminalizePredecessor ==
    /\ status[Predecessor] = "live"
    /\ status' = [status EXCEPT ![Predecessor] = "terminal"]
    /\ UNCHANGED <<cpAlive, nodeUp, localVolume, relinquished, exportState,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    released, actorGeneration, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

WriteRelinquish ==
    /\ IF TerminalPredecessorGuard
          THEN status[Predecessor] = "terminal"
          ELSE TRUE
    /\ localVolume[SourceNode]
    /\ ~relinquished
    /\ relinquished' = TRUE
    /\ UNCHANGED <<status, cpAlive, nodeUp, localVolume, exportState,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    released, actorGeneration, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

BeginExport ==
    /\ relinquished
    /\ exportState = "none"
    /\ localVolume[SourceNode]
    /\ exportState' = "pending"
    /\ UNCHANGED <<status, cpAlive, nodeUp, localVolume, relinquished,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    released, actorGeneration, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

CompleteExport ==
    /\ exportState = "pending"
    /\ localVolume[SourceNode]
    /\ exportState' = "complete"
    /\ exportGeneration' = actorGeneration[Predecessor]
    /\ UNCHANGED <<status, cpAlive, nodeUp, localVolume, relinquished,
                    workers, durableClaims, volatileClaims, released,
                    actorGeneration, commonGeneration, divergenceDetected,
                    reconnectedDivergence, silentMerge, inheritedNonTerminal,
                    cpCrashes, nodeCrashes>>

BeginInheritance(h) ==
    /\ cpAlive
    /\ h \in Heirs
    /\ status[h] = "absent"
    /\ h \notin workers
    /\ IF TerminalPredecessorGuard
          THEN status[Predecessor] = "terminal"
          ELSE TRUE
    /\ IF SafeRelease
          THEN relinquished
          ELSE TRUE
    /\ IF ExclusiveHeirGuard
          THEN durableClaims = {} /\ LiveHeirs = {}
          ELSE volatileClaims = {}
    /\ workers' = workers \cup {h}
    /\ durableClaims' =
          IF ExclusiveHeirGuard THEN durableClaims \cup {h} ELSE durableClaims
    /\ volatileClaims' =
          IF ExclusiveHeirGuard THEN volatileClaims ELSE volatileClaims \cup {h}
    /\ inheritedNonTerminal' =
          (inheritedNonTerminal \/ status[Predecessor] # "terminal")
    /\ UNCHANGED <<status, cpAlive, nodeUp, localVolume, relinquished,
                    exportState, exportGeneration, released, actorGeneration,
                    commonGeneration, divergenceDetected,
                    reconnectedDivergence, silentMerge, cpCrashes,
                    nodeCrashes>>

ReleaseSource(h) ==
    /\ h \in workers
    /\ h \notin released
    /\ (localVolume[SourceNode] \/ ExportComplete)
    /\ IF SafeRelease
          THEN relinquished /\ ExportComplete
          ELSE TRUE
    /\ released' = released \cup {h}
    /\ localVolume' = [localVolume EXCEPT ![SourceNode] = FALSE]
    /\ UNCHANGED <<status, cpAlive, nodeUp, relinquished, exportState,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    actorGeneration, commonGeneration, divergenceDetected,
                    reconnectedDivergence, silentMerge, inheritedNonTerminal,
                    cpCrashes, nodeCrashes>>

InstallReleasedHeir(h) ==
    /\ cpAlive
    /\ h \in released
    /\ status[h] = "absent"
    /\ (ExportComplete \/ ~SafeRelease)
    /\ status' = [status EXCEPT ![h] = "live"]
    /\ localVolume' = [localVolume EXCEPT ![TargetNode] = TRUE]
    /\ actorGeneration' = [actorGeneration EXCEPT
                              ![h] = IF ExportComplete
                                      THEN exportGeneration
                                      ELSE actorGeneration[Predecessor]]
    /\ commonGeneration' = [commonGeneration EXCEPT ![h] = actorGeneration'[h]]
    /\ workers' = workers \ {h}
    /\ volatileClaims' = volatileClaims \ {h}
    /\ UNCHANGED <<cpAlive, nodeUp, relinquished, exportState,
                    exportGeneration, durableClaims, released,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

InstallRemoteHeir(h) ==
    /\ cpAlive
    /\ h \in workers
    /\ status[h] = "absent"
    /\ ~nodeUp[SourceNode]
    /\ ExportComplete
    /\ status' = [status EXCEPT ![h] = "live"]
    /\ localVolume' = [localVolume EXCEPT ![TargetNode] = TRUE]
    /\ actorGeneration' = [actorGeneration EXCEPT ![h] = exportGeneration]
    /\ commonGeneration' = [commonGeneration EXCEPT ![h] = exportGeneration]
    /\ workers' = workers \ {h}
    /\ volatileClaims' = volatileClaims \ {h}
    /\ UNCHANGED <<cpAlive, nodeUp, relinquished, exportState,
                    exportGeneration, durableClaims, released,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

MutateHeir(h) ==
    /\ h \in LiveHeirs
    /\ nodeUp[TargetNode]
    /\ localVolume[TargetNode]
    /\ actorGeneration[h] < MaxGeneration
    /\ actorGeneration' = [actorGeneration EXCEPT ![h] = @ + 1]
    /\ UNCHANGED <<status, cpAlive, nodeUp, localVolume, relinquished,
                    exportState, exportGeneration, workers, durableClaims,
                    volatileClaims, released, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

MutateDisconnectedSource ==
    /\ ~nodeUp[SourceNode]
    /\ localVolume[SourceNode]
    /\ LiveHeirs # {}
    /\ actorGeneration[Predecessor] < MaxGeneration
    /\ actorGeneration' = [actorGeneration EXCEPT
                              ![Predecessor] = @ + 1]
    /\ UNCHANGED <<status, cpAlive, nodeUp, localVolume, relinquished,
                    exportState, exportGeneration, workers, durableClaims,
                    volatileClaims, released, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

CrashCP ==
    /\ cpAlive
    /\ cpCrashes < MaxCPCrashes
    /\ cpAlive' = FALSE
    /\ cpCrashes' = cpCrashes + 1
    /\ volatileClaims' = {}
    /\ UNCHANGED <<status, nodeUp, localVolume, relinquished, exportState,
                    exportGeneration, workers, durableClaims, released,
                    actorGeneration, commonGeneration, divergenceDetected,
                    reconnectedDivergence, silentMerge, inheritedNonTerminal,
                    nodeCrashes>>

RestartCP ==
    /\ ~cpAlive
    /\ cpAlive' = TRUE
    /\ UNCHANGED <<status, nodeUp, localVolume, relinquished, exportState,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    released, actorGeneration, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

CrashNode(n) ==
    /\ n \in Nodes
    /\ nodeUp[n]
    /\ nodeCrashes < MaxNodeCrashes
    /\ nodeUp' = [nodeUp EXCEPT ![n] = FALSE]
    /\ nodeCrashes' = nodeCrashes + 1
    /\ UNCHANGED <<status, cpAlive, localVolume, relinquished, exportState,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    released, actorGeneration, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes>>

ReconnectSource ==
    /\ ~nodeUp[SourceNode]
    /\ nodeUp' = [nodeUp EXCEPT ![SourceNode] = TRUE]
    /\ LET diverged == \E h \in LiveHeirs : Diverged(h)
       IN /\ reconnectedDivergence' = (reconnectedDivergence \/ diverged)
          /\ divergenceDetected' =
                (divergenceDetected \/ (diverged /\ ReconnectComparison))
          /\ silentMerge' =
                (silentMerge \/ (diverged /\ ~ReconnectComparison))
    /\ UNCHANGED <<status, cpAlive, localVolume, relinquished, exportState,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    released, actorGeneration, commonGeneration,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

RestartNode(n) ==
    /\ n \in Nodes \ {SourceNode}
    /\ ~nodeUp[n]
    /\ nodeUp' = [nodeUp EXCEPT ![n] = TRUE]
    /\ UNCHANGED <<status, cpAlive, localVolume, relinquished, exportState,
                    exportGeneration, workers, durableClaims, volatileClaims,
                    released, actorGeneration, commonGeneration,
                    divergenceDetected, reconnectedDivergence, silentMerge,
                    inheritedNonTerminal, cpCrashes, nodeCrashes>>

Next ==
    \/ MutatePredecessor
    \/ TerminalizePredecessor
    \/ WriteRelinquish
    \/ BeginExport
    \/ CompleteExport
    \/ \E h \in Heirs : BeginInheritance(h)
    \/ \E h \in Heirs : ReleaseSource(h)
    \/ \E h \in Heirs : InstallReleasedHeir(h)
    \/ \E h \in Heirs : InstallRemoteHeir(h)
    \/ \E h \in Heirs : MutateHeir(h)
    \/ MutateDisconnectedSource
    \/ CrashCP
    \/ RestartCP
    \/ \E n \in Nodes : CrashNode(n)
    \/ ReconnectSource
    \/ \E n \in Nodes : RestartNode(n)

Spec == Init /\ [][Next]_vars

=============================================================================
