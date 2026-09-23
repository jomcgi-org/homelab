-------------------------------- MODULE warmth_gc --------------------------------
(***************************************************************************)
(* Issue #4705: warmth GC racing with the DURABLE session-workspace tier.   *)
(*                                                                         *)
(* Scope. `Embervm.S3WarmthGc` sweeps five allowlisted S3 prefix kinds.     *)
(* Four of them (stateful/, session/, serving/, group_set/) are DISPOSABLE  *)
(* CACHES: losing one costs a cold start. One of them,                      *)
(* session-workspace/<workload>/<lineage>/, is DURABLE USER DATA: losing it *)
(* loses work. This spec models one lineage of the durable tier racing      *)
(* against the sweep, plus one workload of the disposable stateful tier,    *)
(* so the two retention contracts can be compared in one state space.       *)
(*                                                                         *)
(* This is a bounded SAFETY model of the current contract. It is not a      *)
(* proof of implementation conformance; see the mapping below and the       *)
(* conformance gaps recorded in README.md.                                  *)
(*                                                                         *)
(* IMPLEMENTATION MAPPING (revision: main @ 42f630326).                     *)
(*                                                                         *)
(*   Retention / eligibility                                                *)
(*     WsEligible          s3_warmth_gc.ex classify/6, :session_workspace   *)
(*                         arm (lineage_referenced, node_reported,          *)
(*                         session_not_expired, younger_than_age_floor).    *)
(*     StEligible          classify/6 :stateful arm plus tier2_protected/4. *)
(*     wsAged / stAged     @default_ttls: 7 days for :session_workspace,    *)
(*                         8 hours for :stateful, against meta.json         *)
(*                         createdAtUnixMs (created_at_map/2).              *)
(*     NewestRefGuard      tier2_protected/4, reachable ONLY through the    *)
(*                         workload_live?/3 arm of classify/6.              *)
(*                                                                         *)
(*   Liveness authority (who may say "this is still referenced")            *)
(*     Referenced          cp_snapshot/1 referenced_lineages, built from    *)
(*                         session_actively_live?/1, i.e. NOT terminal and  *)
(*                         NOT :banked / :parked.                           *)
(*     NodeReported        cp_snapshot/1 reported_lineages, built from      *)
(*                         NodeCapacity session_volumes facts.              *)
(*     ParkedHeld          parked_lineage_expiries + parked_session_not_    *)
(*                         expired?/4: a :parked row holds its lineage      *)
(*                         until the CP expiry deadline passes.             *)
(*     fleetFresh          check_fleet_fresh/1 + NodeCapacity dropping a    *)
(*                         non-dispatchable node's row.                     *)
(*     cpRebuilt           check_uptime/1 (@min_uptime_ms) and              *)
(*                         check_empty_cp_state/7.                          *)
(*                                                                         *)
(*   Lifecycle operations                                                   *)
(*     Park                session_state.ex {:running, :park} => :parking,  *)
(*                         {:parking, :park_complete} => :parked.           *)
(*     ParkDeadlinePasses  wall clock passes SessionStore expires_at before *)
(*                         the CP sweep observes it.                        *)
(*     ExpireSession       session_manager.ex do_sweep/1 expiry arm;        *)
(*                         {:parked, :expire} => :expired.                  *)
(*     BrickGone           noded drainSessionExports (server.go) exports    *)
(*                         every UNATTACHED workspace volume, then          *)
(*                         {:running, :brick_gone_park} => :parked leaves   *)
(*                         the S3 copy as the only copy.                    *)
(*     RetireVolume        session_manager.ex retire_session_volume/2 ->    *)
(*                         noded store.go RetireVolume (refuses while       *)
(*                         lineageAttached), WriteRetirementIntent then     *)
(*                         enqueueExport.                                   *)
(*     ExportFiles /       noded ExportArtifact: data files first,          *)
(*     ExportComplete      meta.json LAST. "complete" == meta.json durable. *)
(*     CompleteRetirement  store.go completeRetirement: local bytes are     *)
(*                         dropped only after the export succeeded.         *)
(*     RestoreLineage      session_manager.ex validate_restore_lineage/4    *)
(*                         (terminal holder required) -> noded              *)
(*                         RestoreArtifact.                                 *)
(*     RelightParked       session_state.ex {:parked, :relight} =>          *)
(*                         :relighting => :running.                         *)
(*     RestorePresence     store.go RestoreArtifact: ArtifactInfo reads the *)
(*                         completeness marker, and an ABSENT               *)
(*                         SESSION_WORKSPACE copy is codes.NotFound, never  *)
(*                         a silent empty volume.                           *)
(*                                                                         *)
(*   Sweep structure                                                        *)
(*     SweepBegin/Abort    run_sweep/1's `with` chain: check_uptime,        *)
(*                         check_fleet_fresh, list_or_abort (a partial      *)
(*                         listing aborts the WHOLE sweep),                 *)
(*                         check_empty_cp_state, persist_manifest.          *)
(*     SweepPlan           build_plan/3 + apply_caps/2.                     *)
(*     SweepDelete*        apply_deletes/3: recheck_live/2 immediately      *)
(*                         before delete_prefix/2, meta.json FIRST so a     *)
(*                         half-delete reads as incomplete, never as        *)
(*                         stale-valid.                                     *)
(*     SweepCrash          any delete failure aborts the remainder          *)
(*                         (apply_deletes/3 :halt), and a CP restart kills  *)
(*                         the sweep with the BEAM.                         *)
(*                                                                         *)
(* ASSUMPTIONS AND DELIBERATE ABSTRACTIONS (a pass means nothing outside    *)
(* these; each is a requirement on the implementation, not a claim about    *)
(* it):                                                                     *)
(*                                                                         *)
(*  A1 recheck_live/2 and the meta.json delete of the SAME prefix are one   *)
(*     atomic step. The implementation runs them sequentially in the GC     *)
(*     process, so a sub-second window exists that this model does not      *)
(*     cover.                                                               *)
(*  A2 FleetRevalidationGuard asserts the fleet-freshness precondition      *)
(*     still holds at plan and delete time. run_sweep/1 checks it ONCE, so  *)
(*     this is an assumption the code does not currently discharge.         *)
(*  A3 ExpiryGuard applies the parked-expiry hold at BOTH plan and recheck  *)
(*     time. classify/6 applies it; recheck_live/2 does NOT.                *)
(*  A4 The disposable stateful tier folds node-reported bundles into        *)
(*     stDesired: both are pure reference holds with identical effect in    *)
(*     classify/6, and the tier is present only to contrast retention       *)
(*     rules.                                                               *)
(*  A5 The stateful recheck is modelled MORE permissively than              *)
(*     recheck_live/2's tier-1 arm (which blocks any tier-1 entry once the  *)
(*     workload is live again). A superset of behaviours cannot weaken a    *)
(*     passing safety result.                                               *)
(*  A6 Only a COMPLETE prefix is planned. Deleting the residue of an        *)
(*     already-incomplete prefix destroys nothing restorable.               *)
(*  A7 Physical media loss is outside the model, as in session_lineage.tla: *)
(*     no protocol preserves bytes after the only copy is destroyed.        *)
(*  A8 One lineage, one workload, unbounded sweep repetition. Per-sweep     *)
(*     caps (@max_prefixes / @max_bytes) only shrink a plan, so omitting    *)
(*     them explores a superset of plans.                                   *)
(***************************************************************************)

EXTENDS FiniteSets

CONSTANTS
    Refs,                   \* stateful snapshot refs of ONE workload (disposable tier)
    NewestRef,              \* the newest ref by created-at in that (vendor, workload)
    ReferenceGuard,         \* hold a referenced or node-reported prefix
    ExpiryGuard,            \* hold a :parked lineage until its CP deadline passes
    AgeFloorGuard,          \* hold a prefix younger than its per-kind TTL
    AbortGuard,             \* abort the whole sweep on an inconsistent inventory
    RestorePresenceGuard,   \* refuse a restore whose store copy is not complete
    NewestRefGuard,         \* retain the newest stateful ref of a LIVE workload
    FleetRevalidationGuard  \* fleet freshness still holds at plan and delete time

ASSUME NewestRef \in Refs

SessStates    == {"live", "parked", "parkedExpired", "terminal"}
DurableStates == {"absent", "partial", "complete"}
GcPhases      == {"idle", "listed", "planned", "deleting"}
ViolKinds     == {"protected", "emptyResume", "inconsistent", "eligibility", "newest"}

VARIABLES
    sess,           \* newest SessionStore holder of the lineage
    localVol,       \* the lineage's local NVMe volume exists on a brick
    durable,        \* the S3 session-workspace/ prefix: absent / partial / complete
    wsAged,         \* prefix older than the 7-day session_workspace floor
    retireIntent,   \* noded .retirement-intent marker is pending
    restoreRefused, \* a restore was refused EXPLICITLY (codes.NotFound)
    fleetFresh,     \* every expected instance is present and fresh in NodeCapacity
    cpRebuilt,      \* CP uptime satisfied and the stores have rebuilt
    stPresent,      \* per-ref stateful S3 prefix present
    stDesired,      \* refs held by a reference (desired or node-reported)
    stAged,         \* per-ref older than the 8-hour stateful floor
    gc,             \* sweep phase
    listedWs,       \* the workspace prefix was present in THIS sweep's listing
    listedSt,       \* the stateful refs present in THIS sweep's listing
    planWs,         \* the workspace prefix is in this sweep's plan
    planSt,         \* the stateful refs in this sweep's plan
    sweepFresh,     \* the CP view backing this sweep's plan was complete
    viol            \* monotone violation witnesses

vars == <<sess, localVol, durable, wsAged, retireIntent, restoreRefused,
          fleetFresh, cpRebuilt, stPresent, stDesired, stAged,
          gc, listedWs, listedSt, planWs, planSt, sweepFresh, viol>>

wsVars == <<sess, localVol, durable, wsAged, retireIntent, restoreRefused>>
stVars == <<stPresent, stDesired, stAged>>
gcVars == <<gc, listedWs, listedSt, planWs, planSt, sweepFresh>>

TypeOK ==
    /\ sess \in SessStates
    /\ localVol \in BOOLEAN
    /\ durable \in DurableStates
    /\ wsAged \in BOOLEAN
    /\ retireIntent \in BOOLEAN
    /\ restoreRefused \in BOOLEAN
    /\ fleetFresh \in BOOLEAN
    /\ cpRebuilt \in BOOLEAN
    /\ stPresent \in [Refs -> BOOLEAN]
    /\ stDesired \in SUBSET Refs
    /\ stAged \in [Refs -> BOOLEAN]
    /\ gc \in GcPhases
    /\ listedWs \in BOOLEAN
    /\ listedSt \in SUBSET Refs
    /\ planWs \in BOOLEAN
    /\ planSt \in SUBSET Refs
    /\ sweepFresh \in BOOLEAN
    /\ viol \in [ViolKinds -> BOOLEAN]

(***************************************************************************)
(* Derived truth.                                                          *)
(*                                                                         *)
(* Protected is GROUND truth: the data is still wanted by a live session,  *)
(* or a brick still holds the only-fast copy. Referenced / NodeReported /  *)
(* ParkedHeld are the control plane's VIEW of it, which is exactly what    *)
(* an incomplete inventory can get wrong.                                  *)
(***************************************************************************)
WorkloadLive == stDesired # {}
Referenced   == cpRebuilt /\ sess = "live"
NodeReported == localVol /\ fleetFresh
ParkedHeld   == cpRebuilt /\ sess = "parked"
Protected    == sess = "live" \/ localVol
CpNotEmpty   == cpRebuilt \/ durable = "absent"
Consistent   == fleetFresh /\ CpNotEmpty
HasWsData    == durable = "complete" \/ localVol

\* A2: the fleet-freshness precondition still holds at plan and delete time.
\* run_sweep/1 evaluates check_fleet_fresh/1 ONCE, before listing.
FleetOK      == ~FleetRevalidationGuard \/ fleetFresh

\* Plan-time eligibility, one arm per cond clause in classify/6.
WsEligible ==
    /\ durable = "complete"
    /\ (~ReferenceGuard \/ (~Referenced /\ ~NodeReported))
    /\ (~ExpiryGuard \/ ~ParkedHeld)
    /\ (~AgeFloorGuard \/ wsAged)

StEligible(r) ==
    /\ stPresent[r]
    /\ (~ReferenceGuard \/ r \notin stDesired)
    /\ (~AgeFloorGuard \/ stAged[r])
    /\ (~NewestRefGuard \/ ~(WorkloadLive /\ r = NewestRef))

\* Delete-time recheck: recheck_live/2 re-reads only what can CHANGE between
\* plan and delete. Age and key parse cannot regress, so they are not re-read.
WsRecheckOK ==
    /\ (~ReferenceGuard \/ (~Referenced /\ ~NodeReported))
    /\ (~ExpiryGuard \/ ~ParkedHeld)

StRecheckOK(r) ==
    /\ (~ReferenceGuard \/ r \notin stDesired)
    /\ (~NewestRefGuard \/ ~(WorkloadLive /\ r = NewestRef))

Mark(k) == [viol EXCEPT ![k] = TRUE]

(***************************************************************************)
(* Durable workspace lifecycle.                                            *)
(***************************************************************************)

Park ==
    /\ sess = "live"
    /\ localVol
    /\ sess' = "parked"
    /\ UNCHANGED <<localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

ParkDeadlinePasses ==
    /\ sess = "parked"
    /\ sess' = "parkedExpired"
    /\ UNCHANGED <<localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* do_sweep expiry, and destroy/fail from a live session.
Terminate ==
    /\ sess \in {"live", "parkedExpired"}
    /\ sess' = "terminal"
    /\ UNCHANGED <<localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* Brick drain: drainSessionExports has already published the workspace, then
\* the session lands on :parked with the S3 copy as its ONLY copy.
BrickGone ==
    /\ sess = "parked"
    /\ localVol
    /\ durable = "complete"
    /\ localVol' = FALSE
    /\ retireIntent' = FALSE
    /\ UNCHANGED <<sess, durable, wsAged, restoreRefused, fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* RetireVolume refuses while the lineage is attached to a live VM.
RetireVolume ==
    /\ sess \in {"parked", "parkedExpired", "terminal"}
    /\ localVol
    /\ ~retireIntent
    /\ retireIntent' = TRUE
    /\ UNCHANGED <<sess, localVol, durable, wsAged, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* Data files first. A fresh export resets the prefix's created-at.
ExportFiles ==
    /\ localVol
    /\ sess \in {"parked", "parkedExpired", "terminal"}
    /\ durable = "absent"
    /\ durable' = "partial"
    /\ wsAged' = FALSE
    /\ UNCHANGED <<sess, localVol, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* meta.json LAST: this is the step that makes the artifact restorable.
ExportComplete ==
    /\ localVol
    /\ durable = "partial"
    /\ durable' = "complete"
    /\ UNCHANGED <<sess, localVol, wsAged, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* completeRetirement drops the local bytes only after a successful export.
CompleteRetirement ==
    /\ retireIntent
    /\ durable = "complete"
    /\ localVol
    /\ localVol' = FALSE
    /\ retireIntent' = FALSE
    /\ UNCHANGED <<sess, durable, wsAged, restoreRefused, fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* A restoring create (terminal holder) or a relight of a parked session. With
\* the presence guard this is disabled unless a complete copy or the local
\* volume survives; a "partial" prefix is NOT data, because ArtifactInfo reads
\* the completeness marker the GC deletes first and the export writes last.
ResumeLineage ==
    /\ sess \in {"parked", "parkedExpired", "terminal"}
    /\ (~RestorePresenceGuard \/ HasWsData)
    /\ sess' = "live"
    /\ viol' = IF HasWsData THEN viol ELSE Mark("emptyResume")
    /\ UNCHANGED <<localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* The explicit refusal: codes.NotFound for an absent SESSION_WORKSPACE copy.
ResumeRefused ==
    /\ sess \in {"parked", "parkedExpired", "terminal"}
    /\ RestorePresenceGuard
    /\ ~HasWsData
    /\ ~restoreRefused
    /\ restoreRefused' = TRUE
    /\ UNCHANGED <<sess, localVol, durable, wsAged, retireIntent,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

AgeWorkspace ==
    /\ ~wsAged
    /\ durable # "absent"
    /\ wsAged' = TRUE
    /\ UNCHANGED <<sess, localVol, durable, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

(***************************************************************************)
(* Fleet and control-plane inventory.                                      *)
(***************************************************************************)

\* NodeCapacity drops a non-dispatchable node's row, so its volumes stop being
\* reported. This does NOT stop an in-flight sweep.
FleetFlips ==
    /\ fleetFresh' = ~fleetFresh
    /\ UNCHANGED <<sess, localVol, durable, wsAged, retireIntent, restoreRefused,
                   cpRebuilt, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

\* A CP restart empties the stores until the op-log replays, and kills any
\* sweep in flight with the BEAM.
CpRestart ==
    /\ cpRebuilt
    /\ cpRebuilt' = FALSE
    /\ gc' = "idle"
    /\ listedWs' = FALSE /\ listedSt' = {}
    /\ planWs' = FALSE /\ planSt' = {} /\ sweepFresh' = TRUE
    /\ UNCHANGED <<sess, localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, viol>>
    /\ UNCHANGED stVars

CpRebuild ==
    /\ ~cpRebuilt
    /\ cpRebuilt' = TRUE
    /\ UNCHANGED <<sess, localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, viol>>
    /\ UNCHANGED stVars /\ UNCHANGED gcVars

(***************************************************************************)
(* Disposable stateful cache tier.                                         *)
(***************************************************************************)

SetStatefulDesired ==
    /\ \E d \in SUBSET Refs : stDesired' = d
    /\ UNCHANGED <<stPresent, stAged>>
    /\ UNCHANGED <<sess, localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED gcVars

AgeStateful ==
    /\ \E r \in Refs : /\ ~stAged[r] /\ stPresent[r]
                       /\ stAged' = [stAged EXCEPT ![r] = TRUE]
    /\ UNCHANGED <<stPresent, stDesired>>
    /\ UNCHANGED <<sess, localVol, durable, wsAged, retireIntent, restoreRefused,
                   fleetFresh, cpRebuilt, viol>>
    /\ UNCHANGED gcVars

(***************************************************************************)
(* The sweep.                                                              *)
(***************************************************************************)

\* check_uptime + check_fleet_fresh, then the S3 LISTing. The candidate set is
\* FIXED here: a prefix created after this step is not in this sweep's listing,
\* which is why listedWs / listedSt are recorded rather than re-derived.
SweepBegin ==
    /\ gc = "idle"
    /\ (~AbortGuard \/ (cpRebuilt /\ fleetFresh))
    /\ gc' = "listed"
    /\ listedWs' = (durable = "complete")
    /\ listedSt' = {r \in Refs : stPresent[r]}
    /\ sweepFresh' = (cpRebuilt /\ fleetFresh)
    /\ UNCHANGED <<planWs, planSt>>
    /\ UNCHANGED wsVars /\ UNCHANGED stVars
    /\ UNCHANGED <<fleetFresh, cpRebuilt, viol>>

\* A list error, a stale fleet, an empty store, or a failed manifest put: the
\* whole sweep aborts and nothing is deleted. Also the delete-failure halt.
SweepAbort ==
    /\ gc # "idle"
    /\ gc' = "idle"
    /\ listedWs' = FALSE /\ listedSt' = {}
    /\ planWs' = FALSE /\ planSt' = {} /\ sweepFresh' = TRUE
    /\ UNCHANGED wsVars /\ UNCHANGED stVars
    /\ UNCHANGED <<fleetFresh, cpRebuilt, viol>>

\* cp_snapshot + check_empty_cp_state + build_plan. The empty-CP guard is what
\* stops a sweep firing mid-rebuild from reading "empty desired set" as
\* "everything is orphaned"; the modelled sweep always lists something, so the
\* guard conservatively requires a rebuilt CP here.
SweepPlan ==
    /\ gc = "listed"
    /\ (~AbortGuard \/ cpRebuilt)
    /\ FleetOK
    /\ gc' = "planned"
    /\ planWs' = (listedWs /\ WsEligible)
    /\ planSt' = {r \in listedSt : StEligible(r)}
    /\ sweepFresh' = (sweepFresh /\ cpRebuilt /\ fleetFresh)
    /\ UNCHANGED <<listedWs, listedSt>>
    /\ UNCHANGED wsVars /\ UNCHANGED stVars
    /\ UNCHANGED <<fleetFresh, cpRebuilt, viol>>

\* meta.json FIRST. Per A1 the recheck is atomic with this step, and this is
\* where every violation witness is recorded: once the completeness marker is
\* gone the prefix is already unrestorable.
SweepDeleteWsMeta ==
    /\ gc \in {"planned", "deleting"}
    /\ planWs
    /\ durable = "complete"
    /\ WsRecheckOK
    /\ FleetOK
    /\ gc' = "deleting"
    /\ durable' = "partial"
    /\ viol' = [viol EXCEPT
                  !["protected"]    = @ \/ Protected,
                  !["inconsistent"] = @ \/ ~sweepFresh,
                  !["eligibility"]  = @ \/ ParkedHeld \/ ~wsAged]
    /\ UNCHANGED <<sess, localVol, wsAged, retireIntent, restoreRefused>>
    /\ UNCHANGED stVars
    /\ UNCHANGED <<listedWs, listedSt, planWs, planSt, sweepFresh, fleetFresh, cpRebuilt>>

SweepDeleteWsRest ==
    /\ gc \in {"planned", "deleting"}
    /\ planWs
    /\ durable = "partial"
    /\ gc' = "deleting"
    /\ durable' = "absent"
    /\ planWs' = FALSE
    /\ UNCHANGED <<sess, localVol, wsAged, retireIntent, restoreRefused>>
    /\ UNCHANGED stVars
    /\ UNCHANGED <<listedWs, listedSt, planSt, sweepFresh, fleetFresh, cpRebuilt, viol>>

SweepDeleteSt ==
    /\ gc \in {"planned", "deleting"}
    /\ FleetOK
    /\ \E r \in planSt :
         /\ stPresent[r]
         /\ StRecheckOK(r)
         /\ stPresent' = [stPresent EXCEPT ![r] = FALSE]
         /\ planSt' = planSt \ {r}
         /\ viol' = [viol EXCEPT
                       !["protected"]    = @ \/ (r \in stDesired),
                       !["inconsistent"] = @ \/ ~sweepFresh,
                       !["eligibility"]  = @ \/ ~stAged[r],
                       !["newest"]       = @ \/ (WorkloadLive /\ r = NewestRef)]
    /\ gc' = "deleting"
    /\ UNCHANGED <<stDesired, stAged>>
    /\ UNCHANGED wsVars
    /\ UNCHANGED <<listedWs, listedSt, planWs, sweepFresh, fleetFresh, cpRebuilt>>

SweepEnd ==
    /\ gc \in {"planned", "deleting"}
    /\ gc' = "idle"
    /\ listedWs' = FALSE /\ listedSt' = {}
    /\ planWs' = FALSE /\ planSt' = {} /\ sweepFresh' = TRUE
    /\ UNCHANGED wsVars /\ UNCHANGED stVars
    /\ UNCHANGED <<fleetFresh, cpRebuilt, viol>>

(***************************************************************************)
(* Spec.                                                                   *)
(***************************************************************************)

Init ==
    /\ sess = "live"
    /\ localVol = TRUE
    /\ durable = "absent"
    /\ wsAged = FALSE
    /\ retireIntent = FALSE
    /\ restoreRefused = FALSE
    /\ fleetFresh = TRUE
    /\ cpRebuilt = TRUE
    /\ stPresent = [r \in Refs |-> TRUE]
    /\ stDesired = Refs
    /\ stAged = [r \in Refs |-> FALSE]
    /\ gc = "idle"
    /\ listedWs = FALSE
    /\ listedSt = {}
    /\ planWs = FALSE
    /\ planSt = {}
    /\ sweepFresh = TRUE
    /\ viol = [k \in ViolKinds |-> FALSE]

Next ==
    \/ Park \/ ParkDeadlinePasses \/ Terminate \/ BrickGone
    \/ RetireVolume \/ ExportFiles \/ ExportComplete \/ CompleteRetirement
    \/ ResumeLineage \/ ResumeRefused \/ AgeWorkspace
    \/ FleetFlips \/ CpRestart \/ CpRebuild
    \/ SetStatefulDesired \/ AgeStateful
    \/ SweepBegin \/ SweepAbort \/ SweepPlan
    \/ SweepDeleteWsMeta \/ SweepDeleteWsRest \/ SweepDeleteSt \/ SweepEnd

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Safety.                                                                 *)
(*                                                                         *)
(* NoProtectedDeletion            the required "protected workspace data    *)
(*                                cannot be deleted" check.                 *)
(* NoSilentEmptyResume            the required "a live session cannot       *)
(*                                silently resume with an empty replacement *)
(*                                workspace after eviction" check.          *)
(* EligibilityRequiresExpiryAndAge revalidates the historical "eligibility   *)
(*                                after session expiry" predicate: expiry    *)
(*                                alone never makes a prefix eligible, the   *)
(*                                per-kind age floor applies independently.  *)
(* NoDeleteOnInconsistentInventory revalidates "no deletion on an aborted    *)
(*                                sweep with an inconsistent inventory".     *)
(* NewestStatefulRetainedWhileLive revalidates the historical newest-        *)
(*                                reference retention predicate, which in    *)
(*                                the CURRENT contract exists only for the   *)
(*                                DISPOSABLE stateful tier and only while    *)
(*                                the workload is live. The durable          *)
(*                                workspace tier has NO recency retention;   *)
(*                                its replacement is reference plus parked-  *)
(*                                expiry plus age, checked above.            *)
(***************************************************************************)

NoProtectedDeletion             == ~viol["protected"]
NoSilentEmptyResume             == ~viol["emptyResume"]
NoDeleteOnInconsistentInventory == ~viol["inconsistent"]
EligibilityRequiresExpiryAndAge == ~viol["eligibility"]
NewestStatefulRetainedWhileLive == ~viol["newest"]

=============================================================================
