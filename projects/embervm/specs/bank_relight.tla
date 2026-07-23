------------------------------ MODULE bank_relight ------------------------------
(*****************************************************************************)
(* EmberVM bank/relight generation-pairing protocol (ADR embervm/006          *)
(* protocol 2), modeled under the post-ADR-embervm/014 consistency rules.     *)
(*                                                                           *)
(* This is the second spec of the ADR 006 pilot, added by the ADR 014 PR 5    *)
(* TLA follow-through. Where adoption.tla models the VM-lifecycle + adoption   *)
(* protocol, this models the stateful bank/relight warm-pairing protocol: a    *)
(* workload's on-disk VOLUME carries a monotonic generation; a BANKED snapshot *)
(* bundle stamps the volume generation it was captured at (the pair key); a    *)
(* WARM relight is only legal when the banked bundle's stamp still matches the  *)
(* current volume generation (snapshot_generation == volume.generation).       *)
(*                                                                           *)
(* WORKER AUTHORITY (ADR embervm/014). The node is the source of truth for     *)
(* the volume's generation; the control plane's stored generation is a cache   *)
(* reconciled from node volume reports (upsert_volume). Node reports can LAG    *)
(* (a node still reporting the pre-bank generation, or under fleet co-location  *)
(* a sibling brick's stale report). The MONOTONIC FLOOR (PR #3770) is the rule  *)
(* that a lagging report never regresses the stored pair-key generation below   *)
(* its current value; a genuine forward divergence still lands.                 *)
(*                                                                           *)
(* NODE-CONFIRMED DESTRUCTION (ADR embervm/014 decision 5). As in adoption.tla, *)
(* the CP records an instance destroyed only after the node confirms teardown.  *)
(*                                                                           *)
(* SINGLE-USE ISOLATED LANE (ADR embervm/014 decision 6, carried into ADR       *)
(* embervm/015). An isolated instance is single-use: it is never banked, never  *)
(* relit, never returned to a warm pool, never snapshotted. The spec makes that *)
(* a checkable property rather than an operational convention.                  *)
(*                                                                           *)
(* PROSE MAP: each PlusCal action abstracts a concrete implementation site.     *)
(* Keep this current with the actions below (the layer-1 vocabulary test        *)
(* asserts the modeled names appear across the spec suite).                     *)
(*                                                                           *)
(*   StartFresh          ~ StatefulManager cold-boot / first start: a fresh      *)
(*                          running instance on a volume at its current gen       *)
(*                          (stateful_store.ex start/2, StartStateful).           *)
(*   WriteVolume         ~ the guest writes, bumping the on-disk volume           *)
(*                          generation (stateful_store bump_volume_generation;     *)
(*                          an ABORT/relight also bumps, ADR 006 protocol 2).      *)
(*   Bank               ~ StopStateful BANK: capture a snapshot, stamping          *)
(*                          snapshot_generation := volume.generation as the         *)
(*                          pair key (stateful_manager finish_bank_active,          *)
(*                          session_banked / stateful_banked op).                    *)
(*   RelightWarm        ~ StartStateful RELIGHT: resume the banked snapshot in       *)
(*                          place, legal ONLY when the pair is still valid            *)
(*                          (do_pair_valid?/2: snapshot_generation == volume.gen);     *)
(*                          the durable completion op is session_relit / stateful_relit. *)
(*   ColdBootBroken     ~ StartStateful COLD wake when the pair broke: warmth is       *)
(*                          discarded, a fresh boot on the current volume gen           *)
(*                          (stateful_state banked -> cold_booting, pair_broken).        *)
(*   NodeReportLagging  ~ a node volume report carrying a (possibly stale)              *)
(*                          generation onto the report channel (refresh_volume_facts).    *)
(*   UpsertVolume       ~ StatefulStore.upsert_volume/3 folding a node report into        *)
(*                          the stored generation; the MONOTONIC FLOOR (PR #3770)          *)
(*                          keeps the stored gen from regressing below its current         *)
(*                          value (handle_call({:upsert_volume,...}) ~L805).                *)
(*   EvictBrokenPair    ~ eager_evict_broken_pairs: a banked bundle whose pair broke        *)
(*                          (snapshot_generation != volume.generation) is evicted           *)
(*                          (stateful_store do_eager_evict_broken_pairs, session_evicted).    *)
(*   BeginDestroy       ~ StatefulManager begin_destroy: durable session_destroying           *)
(*                          intent op, VM stays resident until node confirms                    *)
(*                          teardown_confirmed=true (ADR 014 decision 5).                        *)
(*   ConfirmDestroy     ~ noded Destroy/reap returns teardown_confirmed=true, THEN the CP        *)
(*                          appends the durable :*_destroyed record (session_destroyed).          *)
(*****************************************************************************)
EXTENDS Naturals, Sequences

CONSTANTS
    MaxGen,          \* generation is capped at this to keep the state space finite
    MonotonicFloor,  \* TRUE models the PR #3770 stored-generation floor; FALSE is the
                     \* pre-#3770 regression (a lagging report rewinds the stored gen)
    Isolated,        \* TRUE models a single-use isolated-lane workload (ADR 015): the
                     \* bank / relight / pool-return transitions must never fire on it
    NULL             \* the absent value (no banked bundle)

\* A bounded FIFO of pending node volume reports (each is a reported generation).
ChanDepth == 2

(*--fair algorithm bank_relight

variables
    \* -- node ground truth (the volume) --------------------------------------
    \* The on-disk volume generation. Bumped by guest writes; a bank stamps a
    \* copy into bankedGen. This is the source of truth the CP cache reconciles to.
    volGen = 1,

    \* -- control-plane cache + banked bundle ---------------------------------
    \* The CP's STORED pair-key generation (a cache of volGen, reconciled via
    \* UpsertVolume). The monotonic floor keeps it from regressing below itself.
    storedGen = 1,
    \* The banked snapshot bundle's stamped snapshot_generation (the pair key), or
    \* NULL when nothing is banked. pair_valid? is bankedGen = storedGen.
    bankedGen = NULL,

    \* -- instance FSM --------------------------------------------------------
    \* none | running | banking | banked | relighting | destroying | destroyed.
    \* Mirrors stateful_state's live/banked/terminal machine (the subset protocol
    \* 2 needs). "banked" holds a snapshot + volume, no live VM.
    instState = "none",

    \* -- node report channel (bounded FIFO of reported generations) ----------
    reportCh = << >>,

    \* -- node-confirmed destruction (durable) --------------------------------
    \* TRUE once the CP has recorded the instance destroyed. NoDestroyBeforeConfirm
    \* asserts this is only set when the node has actually torn the instance down.
    cpDestroyed = FALSE,
    \* TRUE while a destroy is in flight (durable :*_destroying intent, node not yet
    \* confirmed). The VM is still resident (instState = "destroying").
    destroyIntent = FALSE,

    \* -- bug witnesses -------------------------------------------------------
    \* Set TRUE if a relight EVER resumed a snapshot stale in ground truth (the
    \* resumed bundle's stamp # the true volGen). NoStaleRelight asserts it stays
    \* FALSE: with the floor on, a CP-valid pair is always truly valid.
    staleRelight = FALSE,
    \* The high-water mark of storedGen ever observed. GenerationNeverRegresses
    \* asserts storedGen never drops below it (the monotonic-floor property).
    storedGenHW = 1;

define
    \* The pair is valid when a banked bundle's stamp equals the current stored
    \* generation (stateful_store do_pair_valid?/2). NULL bundle => no valid pair.
    PairValid == bankedGen # NULL /\ bankedGen = storedGen

    \* -- Invariants ---------------------------------------------------------
    TypeOK ==
        /\ volGen \in 1..MaxGen
        /\ storedGen \in 1..MaxGen
        /\ storedGenHW \in 1..MaxGen
        /\ bankedGen \in (1..MaxGen) \cup {NULL}
        /\ instState \in {"none", "running", "banking", "banked",
                          "relighting", "destroying", "destroyed"}
        /\ Len(reportCh) <= ChanDepth
        /\ cpDestroyed \in BOOLEAN
        /\ destroyIntent \in BOOLEAN

    \* No wake resumes a stale snapshot: a warm relight only ever resumed a bundle
    \* whose stamp matched the TRUE volume generation. staleRelight is set by
    \* RelightWarm iff it resumed a bundle with resumedGen # volGen (CP-believed
    \* valid but stale in ground truth). With the monotonic floor on, a CP-valid
    \* pair is always truly valid, so this stays FALSE.
    NoStaleRelight == staleRelight = FALSE

    \* The stored pair-key generation never regresses below its own high-water mark.
    \* This is the PR #3770 monotonic-floor guarantee; the pre-#3770 model
    \* (MonotonicFloor = FALSE) lets a lagging node report rewind it, which this
    \* invariant catches (the recurring demo-postgres pair_broken flap root cause).
    GenerationNeverRegresses == storedGen >= storedGenHW

    \* A single-use isolated instance is never reused: it never reaches a banking,
    \* banked, or relighting state (the reuse lifecycle), and never has a banked
    \* bundle. The Bank / RelightWarm guards structurally forbid these for an
    \* isolated workload (ADR embervm/014 decision 6 / ADR 015). A model that
    \* dropped the ~Isolated guard would let an isolated instance reach "banked"
    \* and this state-reachability invariant would catch it immediately.
    SingleUseNeverReused ==
        Isolated =>
            /\ instState \notin {"banking", "banked", "relighting"}
            /\ bankedGen = NULL

    \* The CP records the instance destroyed only after the node confirmed teardown:
    \* cpDestroyed implies the instance reached the terminal node-torn-down state.
    \* (ADR embervm/014 decision 5, the same carve-out adoption.tla's
    \* NoDestroyBeforeConfirm asserts for the task-lifecycle surface.)
    NoDestroyBeforeConfirm == cpDestroyed => (instState = "destroyed")
end define;

macro pushReport(g) begin
    if Len(reportCh) < ChanDepth then
        reportCh := Append(reportCh, g);
    else
        reportCh := Append(Tail(reportCh), g);
    end if;
end macro;

begin
Run:
    while TRUE do
        either
            \* -- StartFresh: a fresh running instance on the current volume gen. -
            with dummy \in {0} do
                await instState = "none";
                instState := "running";
            end with;

        or
            \* -- WriteVolume: guest write bumps the on-disk volume generation. ---
            \* Only a live (running) instance writes. Bounded by MaxGen to stay
            \* finite (the generation supply is logically unbounded; the cap is a
            \* model bound, like adoption.tla's cyclic gen).
            with dummy \in {0} do
                await instState = "running" /\ volGen < MaxGen;
                volGen := volGen + 1;
            end with;

        or
            \* -- Bank: capture a snapshot, stamping the current volume gen. ------
            \* Structurally forbidden for a single-use isolated instance (~Isolated
            \* guard): an isolated workload is never banked. The pair key stamps the
            \* TRUE volume generation the daemon just banked (StopStatefulResponse
            \* .generation is node-reported, not the CP cache): bankedGen := volGen,
            \* not storedGen. That makes a later CP-cache regression (floor off) a
            \* DETECTABLE stale pair rather than a bundle stamped at the stale cache.
            with dummy \in {0} do
                await instState = "running" /\ ~Isolated;
                instState := "banked" ||
                bankedGen := volGen;
            end with;

        or
            \* -- RelightWarm: resume the banked snapshot (WARM wake). ------------
            \* Guarded by the CP's pair-validity BELIEF (PairValid: bankedGen ==
            \* storedGen), exactly the do_pair_valid?/2 check the code routes on. A
            \* CP-broken pair falls to ColdBootBroken instead. Forbidden for an
            \* isolated instance (never relit; reusedIsolated witnesses the guard).
            \*
            \* staleRelight is set when this warm resume actually resumed a bundle
            \* whose generation does not match the TRUE volume generation (bankedGen
            \* != volGen), i.e. the CP believed the pair valid but it was stale in
            \* ground truth. With the monotonic floor ON, a CP-valid pair is always
            \* truly valid, so this stays FALSE. Without the floor, a regressed
            \* storedGen can make a stale bundle look CP-valid, and this catches the
            \* wake resuming a stale snapshot.
            \* An isolated instance never reaches "banked" (Bank refuses it), so the
            \* ~Isolated guard here is belt-and-braces: relight is a reuse transition.
            with resumedGen = bankedGen do
                await instState = "banked" /\ PairValid /\ ~Isolated;
                \* resumedGen is the bundle's stamped generation (captured before it
                \* is cleared). A wake is STALE when that stamp does not match the TRUE
                \* volume generation, even though the CP believed the pair valid
                \* (PairValid, on a possibly-regressed storedGen).
                if resumedGen # volGen then
                    staleRelight := TRUE;
                end if;
                instState := "running" ||
                bankedGen := NULL;
            end with;

        or
            \* -- ColdBootBroken: broken-pair wake discards warmth, fresh boot. ---
            \* The banked bundle is evicted and the instance boots fresh on the
            \* current volume generation. Legal wake path when the pair broke.
            with dummy \in {0} do
                await instState = "banked" /\ ~PairValid;
                instState := "running" ||
                bankedGen := NULL;
            end with;

        or
            \* -- NodeReportLagging: a node volume report (possibly stale gen). ---
            \* The node reports the volume generation it currently sees. It may LAG
            \* the true volGen (a stale report; under co-location a sibling brick's
            \* older view). Any generation from 1..volGen is a plausible report.
            with g \in 1..volGen do
                pushReport(g);
            end with;

        or
            \* -- UpsertVolume: fold one node report into the stored generation. --
            \* The MONOTONIC FLOOR (PR #3770): a report below the current stored gen
            \* does NOT regress it (MonotonicFloor = TRUE). A forward report advances
            \* it. Without the floor (MonotonicFloor = FALSE) the stored gen takes the
            \* reported value even when lower, regressing it below a just-banked
            \* bundle's stamp: the pre-#3770 pair_broken flap.
            with g = Head(reportCh) do
                await reportCh # << >>;
                reportCh := Tail(reportCh);
                \* newGen is the folded stored generation: with the floor, a report
                \* below the current stored gen does not regress it; without it, the
                \* reported value wins even when lower (the pre-#3770 regression).
                with newGen = IF MonotonicFloor /\ g < storedGen THEN storedGen ELSE g do
                    storedGen := newGen;
                    \* Track the high-water mark so GenerationNeverRegresses catches a
                    \* regression (stored gen dropping below a value it already reached).
                    storedGenHW := IF newGen > storedGenHW THEN newGen ELSE storedGenHW;
                end with;
            end with;

        or
            \* -- EvictBrokenPair: a banked bundle whose pair broke is evicted. ---
            \* eager_evict_broken_pairs: a broken pair (stamp != stored gen) is
            \* evicted through the durable path, leaving no banked bundle. Models the
            \* warm-bundle loss the monotonic floor is meant to prevent (with the
            \* floor on, a lagging report never breaks a fresh pair, so this fires
            \* far less; without it, the flap evicts good bundles).
            with dummy \in {0} do
                await instState = "banked" /\ bankedGen # NULL /\ bankedGen # storedGen;
                bankedGen := NULL ||
                instState := "none";
            end with;

        or
            \* -- BeginDestroy: durable destroy intent; VM stays resident. --------
            \* Node-confirmed destruction (ADR 014 decision 5): the CP appends the
            \* :*_destroying intent and dispatches Destroy, but does NOT record the
            \* instance destroyed until the node confirms (ConfirmDestroy).
            with dummy \in {0} do
                await instState \in {"running", "banked"} /\ ~destroyIntent;
                destroyIntent := TRUE ||
                instState := "destroying";
            end with;

        or
            \* -- ConfirmDestroy: node confirms teardown, THEN the CP records it. --
            \* The node tears the instance down (instState -> "destroyed") and the CP
            \* appends the durable :*_destroyed record together, teardown first, so
            \* NoDestroyBeforeConfirm holds. A banked bundle is gone with the volume.
            with dummy \in {0} do
                await instState = "destroying" /\ destroyIntent;
                instState := "destroyed" ||
                bankedGen := NULL;
                destroyIntent := FALSE ||
                cpDestroyed := TRUE;
            end with;
        end either;
    end while;
end algorithm; *)
\* BEGIN TRANSLATION (chksum(pcal) = "2e3b7e77" /\ chksum(tla) = "2e7cc17a")
VARIABLES volGen, storedGen, bankedGen, instState, reportCh, cpDestroyed, 
          destroyIntent, staleRelight, storedGenHW

(* define statement *)
PairValid == bankedGen # NULL /\ bankedGen = storedGen


TypeOK ==
    /\ volGen \in 1..MaxGen
    /\ storedGen \in 1..MaxGen
    /\ storedGenHW \in 1..MaxGen
    /\ bankedGen \in (1..MaxGen) \cup {NULL}
    /\ instState \in {"none", "running", "banking", "banked",
                      "relighting", "destroying", "destroyed"}
    /\ Len(reportCh) <= ChanDepth
    /\ cpDestroyed \in BOOLEAN
    /\ destroyIntent \in BOOLEAN






NoStaleRelight == staleRelight = FALSE





GenerationNeverRegresses == storedGen >= storedGenHW







SingleUseNeverReused ==
    Isolated =>
        /\ instState \notin {"banking", "banked", "relighting"}
        /\ bankedGen = NULL





NoDestroyBeforeConfirm == cpDestroyed => (instState = "destroyed")


vars == << volGen, storedGen, bankedGen, instState, reportCh, cpDestroyed, 
           destroyIntent, staleRelight, storedGenHW >>

Init == (* Global variables *)
        /\ volGen = 1
        /\ storedGen = 1
        /\ bankedGen = NULL
        /\ instState = "none"
        /\ reportCh = << >>
        /\ cpDestroyed = FALSE
        /\ destroyIntent = FALSE
        /\ staleRelight = FALSE
        /\ storedGenHW = 1

Next == \/ /\ \E dummy \in {0}:
                /\ instState = "none"
                /\ instState' = "running"
           /\ UNCHANGED <<volGen, storedGen, bankedGen, reportCh, cpDestroyed, destroyIntent, staleRelight, storedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ instState = "running" /\ volGen < MaxGen
                /\ volGen' = volGen + 1
           /\ UNCHANGED <<storedGen, bankedGen, instState, reportCh, cpDestroyed, destroyIntent, staleRelight, storedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ instState = "running" /\ ~Isolated
                /\ /\ bankedGen' = volGen
                   /\ instState' = "banked"
           /\ UNCHANGED <<volGen, storedGen, reportCh, cpDestroyed, destroyIntent, staleRelight, storedGenHW>>
        \/ /\ LET resumedGen == bankedGen IN
                /\ instState = "banked" /\ PairValid /\ ~Isolated
                /\ IF resumedGen # volGen
                      THEN /\ staleRelight' = TRUE
                      ELSE /\ TRUE
                           /\ UNCHANGED staleRelight
                /\ /\ bankedGen' = NULL
                   /\ instState' = "running"
           /\ UNCHANGED <<volGen, storedGen, reportCh, cpDestroyed, destroyIntent, storedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ instState = "banked" /\ ~PairValid
                /\ /\ bankedGen' = NULL
                   /\ instState' = "running"
           /\ UNCHANGED <<volGen, storedGen, reportCh, cpDestroyed, destroyIntent, staleRelight, storedGenHW>>
        \/ /\ \E g \in 1..volGen:
                IF Len(reportCh) < ChanDepth
                   THEN /\ reportCh' = Append(reportCh, g)
                   ELSE /\ reportCh' = Append(Tail(reportCh), g)
           /\ UNCHANGED <<volGen, storedGen, bankedGen, instState, cpDestroyed, destroyIntent, staleRelight, storedGenHW>>
        \/ /\ LET g == Head(reportCh) IN
                /\ reportCh # << >>
                /\ reportCh' = Tail(reportCh)
                /\ LET newGen == IF MonotonicFloor /\ g < storedGen THEN storedGen ELSE g IN
                     /\ storedGen' = newGen
                     /\ storedGenHW' = (IF newGen > storedGenHW THEN newGen ELSE storedGenHW)
           /\ UNCHANGED <<volGen, bankedGen, instState, cpDestroyed, destroyIntent, staleRelight>>
        \/ /\ \E dummy \in {0}:
                /\ instState = "banked" /\ bankedGen # NULL /\ bankedGen # storedGen
                /\ /\ bankedGen' = NULL
                   /\ instState' = "none"
           /\ UNCHANGED <<volGen, storedGen, reportCh, cpDestroyed, destroyIntent, staleRelight, storedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ instState \in {"running", "banked"} /\ ~destroyIntent
                /\ /\ destroyIntent' = TRUE
                   /\ instState' = "destroying"
           /\ UNCHANGED <<volGen, storedGen, bankedGen, reportCh, cpDestroyed, staleRelight, storedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ instState = "destroying" /\ destroyIntent
                /\ /\ bankedGen' = NULL
                   /\ instState' = "destroyed"
                /\ /\ cpDestroyed' = TRUE
                   /\ destroyIntent' = FALSE
           /\ UNCHANGED <<volGen, storedGen, reportCh, staleRelight, storedGenHW>>

Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(Next)

\* END TRANSLATION 
=============================================================================
