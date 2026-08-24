--------------------------- MODULE generation_issuance ---------------------------
(*****************************************************************************)
(* EmberVM generation issuance authority (issue #4700, ADR embervm/006        *)
(* protocol 4): blessing, wake grants, quarantine, and checkpoint-abort       *)
(* auto-heal, modeled as one protocol with an adversarial scheduler.          *)
(*                                                                           *)
(* The control plane is the SOLE issuer of a stateful volume's generation     *)
(* (R7, ADR embervm/011 standing decision 4). ARCHITECTURE.md's "Generation   *)
(* blessing and quarantine" section names exactly three legitimate issuance    *)
(* shapes, and this spec models all three plus the reconciliation that judges  *)
(* every other advance:                                                        *)
(*                                                                           *)
(*   1. CP-ISSUED PRE-DISPATCH: every wake durably blesses the next           *)
(*      generation (op-log-before-dispatch fence) before the attach.           *)
(*   2. CHECKPOINT-ABORT AUTO-HEAL: noded's resolve-timeout auto-abort         *)
(*      self-bumps by exactly +1 on the SAME vm_id; a durable                  *)
(*      checkpoint_dispatched{workload, vm_id, generation} record lets the     *)
(*      control plane prove the +1 was its own and bless it instead of         *)
(*      quarantining (ADR embervm/017).                                        *)
(*   3. DELEGATED ADVANCEMENT: every wake grants the anchor a durable bounded  *)
(*      blessing lease (wake_grant{floor, ceiling}; the built form has no       *)
(*      expiry yet, expiry is the decided Fork B remainder), the anchor's      *)
(*      activator consumes generations from the range during control-plane     *)
(*      absence, and an exhausted grant degrades to the unblessable self-bump  *)
(*      that fenced-writer adoption backfills on return (ADR embervm/014).      *)
(*                                                                           *)
(* THE QUARANTINE LADDER is the implementation's update_quarantine/4 cond,     *)
(* order for order: auto-heal on a matching record first, then fail closed     *)
(* under ANY unresolved checkpoint context (a non-matching jump is the         *)
(* second-writer hazard, quarantined EVEN FROM THE ANCHOR), then fenced-writer *)
(* anchor adoption, then fail-closed quarantine, and finally the clear rule    *)
(* with the a42c819d7 clear-suppression shield while quarantined.              *)
(*                                                                           *)
(* WORKER AUTHORITY (ADR embervm/014). The node-side ledger (volGen, the       *)
(* blessed wire marker) is ground truth; the CP's blessedGen watermark is a    *)
(* durable ledger the reports reconcile. Node reports can LAG (any generation   *)
(* from 1..volGen is a plausible report) and the adversary can forge a SECOND  *)
(* WRITER's advance (the split-brain shape ADR 017 discriminates), within a     *)
(* budget. A CP crash loses exactly the volatile state (quarantine flag,       *)
(* in-flight bless, unwritten dispatch record: the narrow fail-closed window)  *)
(* and rebuilds un-quarantined, exactly like the ETS rebuild.                   *)
(*                                                                           *)
(* ISSUE #4700'S FOUR INVARIANT BULLETS map to the checks below:               *)
(*   "No advancement outside a covering grant or a provable own-checkpoint is  *)
(*    ever blessed; everything else quarantines on sight"                      *)
(*     -> NoFalseQuarantine (its constructive dual: the ladder never           *)
(*        quarantines a grant-covered advance, a provable own-checkpoint, or   *)
(*        the anchor's own lag; the blessing arms themselves carry the         *)
(*        signature guards by construction, quota.tla's DenialAudited style).  *)
(*   "A quarantined volume never serves" -> NoServeQuarantined.                *)
(*   "Auto-heal blesses only checkpoints the CP can prove were its own"        *)
(*     -> the HealSignature guard (by construction) and NoFalseQuarantine's    *)
(*        heal half (disabling the heal must not silently quarantine the       *)
(*        provable case: that is the bug).                                     *)
(*   "A grant changes who may issue a generation, never who may write a        *)
(*    volume" -> grants are issued only to the anchor (GrantLease is keyed     *)
(*        to AnchorNode) and consumed only by the anchor's own wake; a         *)
(*        non-anchor advance exists only as the budgeted RogueSecondWriter     *)
(*        attack, which issues nothing and only ever yields quarantine         *)
(*        evidence. LeaseWellFormed pins the grant's shape.                    *)
(*                                                                           *)
(* EXCLUDED (deliberately): the ADR embervm/037 brick silence gate (node-local *)
(* self-advance stops while silenced; orthogonal to who may issue), export/    *)
(* archive gating on blessing, handoffs (the anchor never moves here), the     *)
(* activator self-bump pairwise interaction with the activator spec (out of    *)
(* scope per the issue), and the pair-key/bank-relight machinery               *)
(* (bank_relight.tla's protocol).                                              *)
(*                                                                           *)
(* PROSE MAP: each PlusCal action abstracts a concrete implementation site.    *)
(* Keep this current with the actions below.                                   *)
(*                                                                           *)
(*   RequestWake         ~ a parked wake for the scaled-to-zero workload       *)
(*                          (ARCHITECTURE.md "Wake path"); plan_wake/2 refuses  *)
(*                          to place ANY wake while quarantined                 *)
(*                          (stateful_manager.ex plan_wake ~L909,               *)
(*                          {:error, :volume_quarantined}).                     *)
(*   BlessWake           ~ StatefulManager.bless_wake_generation/3: issue       *)
(*                          next_blessed_generation and append the durable      *)
(*                          generation_blessed op BEFORE dispatch (the fence,   *)
(*                          stateful_manager.ex ~L646-688;                      *)
(*                          StatefulStore.bless_generation/3 handle_call        *)
(*                          ~L1062, do_bless_append ~L1097; the watermark       *)
(*                          never-regresses guard ~L1065-1080). next_blessed    *)
(*                          skips outstanding lease ranges                      *)
(*                          (handle_call_next_blessed ~L1732,                   *)
(*                          Enum.max([current + 1 | ends])).                    *)
(*   DispatchAttach      ~ noded StartStateful attachGeneration: RecordBlessed   *)
(*                          of the CP-issued value, strictly past the ledger     *)
(*                          (noded/server/stateful.go attachGeneration ~L105;    *)
(*                          noded/volume/volume.go RecordBlessed ~L335,          *)
(*                          gen <= cur refused ~L340).                           *)
(*   ActivatorWake       ~ the node-local activator's writable attach during     *)
(*                          control-plane absence (stateful.go attachGeneration  *)
(*                          activatorOrigin ~L113): ConsumeGenerationFromLease   *)
(*                          with its ledger clamp (volume.go ~L374, clamp        *)
(*                          ~L399-401), else the unblessable self-bump fallback  *)
(*                          (BumpGeneration ~L296, marker left behind).          *)
(*   GrantLease          ~ StatefulStore.ensure_blessing_lease/3 ->              *)
(*                          append_blessing_lease: start = max(watermark+1,      *)
(*                          reported+1), width @blessing_lease_size             *)
(*                          (stateful_store.ex ~L1034, ~L1748-1758); anchor-only *)
(*                          (the volume row's node_id match ~L1036-1047).        *)
(*   ExpireGrant         ~ the decided Fork B expires_at (ARCHITECTURE.md:       *)
(*                          "Decided direction: lease expiry"); not yet built,  *)
(*                          modeled as an undrained grant dying during CP       *)
(*                          absence.                                             *)
(*   PauseForCheckpoint  ~ the interruptible-bank checkpoint pause               *)
(*                          (noded markCheckpointed; the {:checkpoint_done}      *)
(*                          report, ADR embervm/008).                            *)
(*   RecordCheckpointDispatch ~ Embervm.StatefulSweeper.finish_checkpoint        *)
(*                          stamping checkpoint_generation + vm_id and calling  *)
(*                          StatefulStore.record_checkpoint_dispatch/4           *)
(*                          (stateful_sweeper.ex ~L1217; store handle_call       *)
(*                          ~L1132). The crash between pause and record is the   *)
(*                          narrow fail-closed window ADR 017 keeps manual.      *)
(*   ResolveAbortByCP    ~ the CP-driven ABORT resolve: the CP issues the        *)
(*                          generation and noded RecordBlesseds it               *)
(*                          (stateful.go abortCheckpoint ~L617 blessedGeneration *)
(*                          # 0 arm; store clear_checkpoint_dispatch ~L1152).     *)
(*   AutoAbortCheckpoint ~ noded's resolve-timeout backstop: blessedGeneration 0,  *)
(*                          BumpGeneration advances by exactly +1 leaving the    *)
(*                          blessed marker behind (stateful.go ~L656-677,        *)
(*                          recordAbortGeneration ~L649; volume.go BumpGeneration *)
(*                          ~L296, TestBumpGenerationLeavesBlessedMarkerBehind).  *)
(*   ReportVolume        ~ the periodic refresh_volume_facts scrape carrying     *)
(*                          {generation, generation_blessed} onto the CP         *)
(*                          (stateful_manager.ex reconcile ~L2475-2477); the     *)
(*                          model lets the adversary claim any lagged            *)
(*                          generation and either wire bit, an                  *)
(*                          over-approximation of straggler + rogue reports.     *)
(*   FoldReport          ~ StatefulStore.upsert_volume/3's monotonic floor then  *)
(*                          update_quarantine/4 (stateful_store.ex ~L960-999,   *)
(*                          ~L1869-1994): the cond ladder this spec copies,      *)
(*                          including checkpoint_abort_signature? ~L2012,        *)
(*                          auto_heal_checkpoint_abort ~L2026, and the           *)
(*                          clear-suppression shield (commit a42c819d7).       *)
(*   CrashCP / RestartCP ~ control-plane death + boot: volatile state lost,      *)
(*                          durable ledger + dispatch records survive via op-log *)
(*                          replay, quarantine flag rebuilt FALSE                *)
(*                          (store moduledoc: "a rebuild always starts           *)
(*                          un-quarantined").                                   *)
(*   RogueSecondWriter   ~ the genuine split-brain shape: a SECOND writer on a   *)
(*                          foreign brick jumps the ledger past +1 under a new   *)
(*                          vm_id (ADR 017 Context; budgeted attack input).     *)
(*****************************************************************************)
EXTENDS Naturals, Sequences

CONSTANTS
    Nodes,             \* model set of node ids (the anchor plus at most one peer)
    AnchorNode,        \* the brick holding the authoritative volume (the Longhorn
                       \* RWO single writer); grants go only here
    NULL,              \* the absent value (no grant / no record vm / no live vm)
    MaxGen,            \* generation cap: a model bound keeping the space finite
                       \* (the real ledger is uint64-unbounded; see the cfg notes)
    LeaseSize,         \* wake-grant width in generations. Built default is 50
                       \* (@blessing_lease_size) and the decided gap budget is k=4;
                       \* the committed cfgs shrink it to 1 so exhaustion boundaries
                       \* stay reachable inside MaxGen.
    GrantExpires,      \* TRUE lets an undrained grant expire during CP absence
                       \* (the decided expires_at half of Fork B); FALSE models the
                       \* built behavior (today's leases never expire)
    AutoHealEnabled,   \* TRUE models the ADR embervm/017 auto-heal branch; FALSE
                       \* models the fail-closed-both-ways posture it replaced,
                       \* where the CP cannot tell its own +1 from a rogue's
    AdoptFencedWriter, \* TRUE models ADR embervm/014 fenced-writer anchor
                       \* adoption; FALSE is the naive fence that deadlocked the
                       \* recurring demo-postgres quarantine after a CP roll
    MaxCPCrashes,      \* how many control-plane crash-restarts a behavior may take
    MaxAutoAborts,     \* how many resolve-timeout auto-aborts a behavior may take
    MaxRogueReports,   \* how many second-writer advances the adversary may forge
    MaxWakes,          \* how many wake requests may be parked at once
    AdversarialReports \* TRUE: reports may claim any lagged generation and wire
                       \* bit (straggler + misattribution over-approximation, the
                       \* SAFETY mode). FALSE: nodes stream only their CURRENT
                       \* truth (the periodic 2s WatchNode scrape), the honest-
                       \* environment abstraction the LIVENESS config checks
                       \* under: letting the adversary rotate junk reports
                       \* forever can starve the one truthful report by channel
                       \* overflow, a scheduler artifact (adoption.tla's
                       \* AgingEnabled precedent), not a protocol wedge.

\* Bounded FIFO of pending volume reports; a straggler report lives here.
ChanDepth == 2

\* Instance identities. The workload is a SINGLETON (decision 3): "vm1" is the
\* anchor's one live VM; "rogue" is a second writer's fresh vm_id.
VmIds == {"vm1", "rogue"}

(*--fair algorithm generation_issuance

variables
    \* -- node ground truth (the volume ledger) -------------------------------
    \* The on-disk generation ledger. Advanced by RecordBlessed (blessed) and
    \* BumpGeneration (unblessed); there is no un-bump.
    volGen = 1,
    \* The generation_blessed wire fact for the CURRENT ledger value: TRUE iff
    \* the last advance went through RecordBlessed; BumpGeneration leaves the
    \* old marker behind, so a self-bump reads unblessed.
    volBlessed = TRUE,
    \* Which vm_id holds the writable attach (the singleton's live VM; "rogue"
    \* after a second-writer advance).
    liveVm = "vm1",
    \* running | paused (checkpointed, interruptible bank).
    instState = "running",

    \* -- report channel (bounded FIFO) ---------------------------------------
    \* Each message is [g |-> reported gen, b |-> claimed blessed bit,
    \* n |-> reporting node]. Survives nothing special: reports are recomputed
    \* from the ledger, so a CP crash discards the channel (rebuild).
    reportCh = << >>,

    \* -- control plane --------------------------------------------------------
    cpAlive = TRUE,
    \* The durable blessing watermark (the generation_blessed op-log ledger,
    \* replayed on boot). 1 = the workload's first wake was blessed pre-history.
    blessedGen = 1,
    \* Volatile quarantine flag: derived from reports, rebuilt FALSE on boot.
    quarantined = FALSE,
    \* The blessed generation awaiting its attach dispatch (the fence window
    \* between op-log append and the StartStateful RPC). Volatile: a crash
    \* kills the wake worker and wastes the blessed number, harmlessly.
    attachPending = 0,

    \* -- the wake grant (blessing lease) --------------------------------------
    \* Durable range [leaseCur, leaseEnd) pre-authorised to the ANCHOR, cursor
    \* inclusive-start/exclusive-end. NULL/NULL = no grant. Survives a CP crash
    \* (the blessing_lease_granted op reloads on boot and the brick persists
    \* its copy); GrantExpires lets it lapse during CP absence.
    leaseCur = NULL,
    leaseEnd = NULL,

    \* -- the checkpoint-dispatch record (durable, ADR embervm/017) ------------
    \* One unresolved record per workload: the CP dispatched a checkpoint at
    \* generation ckptGen on vm ckptVm and has not resolved it.
    ckptPresent = FALSE,
    ckptVm = NULL,
    ckptGen = 0,
    \* The pause whose dispatch record has NOT been appended yet (in-flight
    \* sweeper work). Volatile: a crash here is the narrow fail-closed window.
    pendingRec = FALSE,
    pendRecVm = NULL,
    pendRecGen = 0,

    \* -- budgets + history ----------------------------------------------------
    wakesPending = 0,
    cpCrashes = 0,
    autoAborts = 0,
    rogueBudget = MaxRogueReports,
    \* Ghost witnesses (not implementation state): servedWhileQuarantined
    \* catches a dropped quarantine guard on any serving path;
    \* falseQuarantineHeal / falseQuarantineLag catch the two historical
    \* false-quarantine bugs the fence must not resurrect.
    servedWhileQuarantined = FALSE,
    falseQuarantineHeal = FALSE,
    falseQuarantineLag = FALSE,
    \* High-water mark of the blessing watermark, so WatermarkNeverRegresses
    \* is stated the way bank_relight.tla states its floor.
    blessedGenHW = 1;

define
    \* An outstanding grant still has consumable generations.
    LeaseUndrained == leaseCur # NULL /\ leaseCur < leaseEnd

    \* The next generation the anchor's wake would draw from the grant, with
    \* ConsumeGenerationFromLease's clamp: a cursor behind the ledger (the CP
    \* blessed past it) skips to ledger + 1 (volume.go ~L399-401).
    NextFromLease == IF leaseCur # NULL /\ leaseCur > volGen THEN leaseCur
                     ELSE volGen + 1

    \* The grant lane is usable for the next wake.
    LeaseServable == LeaseUndrained /\ NextFromLease < leaseEnd

    \* The next generation a CP bless issues: one past the watermark, skipping
    \* an outstanding grant's end (handle_call_next_blessed's
    \* Enum.max([current + 1 | ends]), stateful_store.ex ~L1732-1746).
    NextBlessed == IF LeaseUndrained /\ leaseEnd > blessedGen THEN leaseEnd
                   ELSE blessedGen + 1

    \* A forward-unblessed report: the wire says NOT blessed and the reported
    \* generation is strictly past the watermark (update_quarantine's
    \* forward_unblessed?, ~L1873-1874).
    ForwardUnblessed(g, b) == ~b /\ g > blessedGen

    \* The benign checkpoint-abort fingerprint (checkpoint_abort_signature?,
    \* ~L2012-2020): an unresolved record exists, the report is exactly one
    \* past the recorded checkpoint generation, and the recorded vm_id is
    \* still the live VM (the auto-abort resumes the SAME process image).
    HealSignature(g) == ckptPresent /\ g = ckptGen + 1
                        /\ liveVm # NULL /\ ckptVm = liveVm

    \* -- Invariants ---------------------------------------------------------

    TypeOK ==
        /\ volGen \in 1..MaxGen
        /\ volBlessed \in BOOLEAN
        /\ blessedGen \in 1..MaxGen
        /\ blessedGenHW \in 1..MaxGen
        /\ liveVm \in VmIds \cup {NULL}
        /\ instState \in {"running", "paused"}
        /\ cpCrashes <= MaxCPCrashes
        /\ autoAborts <= MaxAutoAborts
        /\ rogueBudget \in 0..MaxRogueReports
        /\ wakesPending \in 0..MaxWakes
        /\ attachPending \in 0..MaxGen
        /\ quarantined \in BOOLEAN
        /\ servedWhileQuarantined \in BOOLEAN
        /\ falseQuarantineHeal \in BOOLEAN
        /\ falseQuarantineLag \in BOOLEAN
        /\ leaseCur \in {NULL} \cup 1..MaxGen
        /\ leaseEnd \in {NULL} \cup 1..MaxGen
        /\ ckptPresent \in BOOLEAN
        /\ ckptVm \in VmIds \cup {NULL}
        /\ ckptGen \in 0..MaxGen
        /\ pendingRec \in BOOLEAN
        /\ pendRecVm \in VmIds \cup {NULL}
        /\ pendRecGen \in 0..MaxGen
        /\ AnchorNode \in Nodes
        /\ reportCh \in Seq([g : 1..MaxGen, b : BOOLEAN, n : Nodes])
        /\ Len(reportCh) <= ChanDepth

    \* The blessing watermark never regresses below a value it reached (the
    \* bless_generation monotonicity guard, ~L1065-1080; every ladder advance
    \* is guarded by reported > watermark).
    WatermarkNeverRegresses == blessedGen >= blessedGenHW

    \* A quarantined volume never serves (plan_wake refuses; no serving path
    \* may bypass the flag). Tripwire style, like adoption.tla's NoReapLive.
    NoServeQuarantined == servedWhileQuarantined = FALSE

    \* The fence never quarantines an advancement the CP can account for:
    \* neither its own provably-resumed checkpoint (the heal half) nor its
    \* anchor's own watermark lag (the adoption half). This is the invariant
    \* the two negative cfgs trip, once per historical bug.
    NoFalseQuarantine ==
        /\ falseQuarantineHeal = FALSE
        /\ falseQuarantineLag = FALSE

    \* A grant, when one exists, is a well-formed anchor-scoped range inside
    \* the generation cap: the ceiling is bounded and the cursor never passes
    \* it (a FULLY DRAINED grant stays recorded with cursor = ceiling, exactly
    \* like the store keeps the row until the next ensure_blessing_lease
    \* regrant). Grants are issued only to the anchor and consumed only by the
    \* anchor's own wake; they change who may ISSUE a generation, never who
    \* may WRITE the volume (the writable attach stays fenced to the single
    \* writer throughout).
    LeaseWellFormed ==
        leaseCur # NULL =>
            /\ leaseEnd # NULL
            /\ leaseCur <= leaseEnd
            /\ leaseEnd <= MaxGen

    \* -- Temporal property --------------------------------------------------
    \* Every parked wake eventually drains under fair scheduling of the
    \* progress actions (FairSpec below), ACROSS a control-plane crash-restart,
    \* a resolve-timeout auto-abort, and grant exhaustion. Two disjuncts scope
    \* the claim to the protocol, not to model artifacts:
    \*   blessedGen >= MaxGen: the generation supply cap is a MODEL bound (real
    \*     generations are uint64-unbounded), so exhaustion is a terminal.
    \*   quarantined /\ ckptPresent: the ADR embervm/017 MANUAL REMAINDER, a
    \*     designed outcome: an unresolved dispatch record whose context does
    \*     not add up (a jump the record cannot prove) fails closed on purpose,
    \*     and recovery is the runbook break-glass, a human decision outside
    \*     the protocol. What liveness still promises here is sharp: the fence
    \*     never parks a wake in that state unless a provably unresolvable
    \*     record context exists (NoFalseQuarantine), and it NEVER parks one
    \*     behind a mere watermark lag (the adoption half).
    EventuallyServed ==
        (wakesPending > 0) ~>
            (wakesPending = 0 \/ blessedGen >= MaxGen
             \/ (quarantined /\ ckptPresent))
end define;

macro AdvanceWatermark(g) begin
    blessedGen := g;
    blessedGenHW := IF g > blessedGenHW THEN g ELSE blessedGenHW;
end macro;

macro pushReport(msg) begin
    \* Append to the FIFO, dropping the oldest if at depth bound (loss of the
    \* oldest, never of ordering).
    if Len(reportCh) < ChanDepth then
        reportCh := Append(reportCh, msg);
    else
        reportCh := Append(Tail(reportCh), msg);
    end if;
end macro;

begin
Run:
    while TRUE do
        either
            \* -- RequestWake: a caller parks a wake. Admission is bounded by
            \* the model's park depth (QueueDepth-style bound, like quota.tla).
            with dummy \in {0} do
                await wakesPending < MaxWakes;
                wakesPending := wakesPending + 1;
            end with;

        or
            \* -- BlessWake: the CP wake path issues and durably records the
            \* next blessed generation BEFORE dispatch (op-log-before-dispatch
            \* fence). Guards mirror plan_wake + bless_wake_generation: a
            \* quarantined volume is never placed (refusal, no bless), the
            \* manager serializes one wake at a time (single-flight), and the
            \* monotonicity guard only compares against the CP's OWN watermark
            \* (~L1065-1080): the issued value MAY sit at or below the node
            \* ledger (the watermark lagged a leased/self advanced brick).
            \* Issuing it anyway is faithful: the durable bless lands
            \* regardless, noded's RecordBlessed then refuses the attach
            \* (DispatchAttach's failure arm), and the watermark having
            \* advanced is what lets the next bless skip past the ledger,
            \* the same way append_blessing_lease's reported+1 regrant term
            \* catches a brick that ran ahead.
            with gen = NextBlessed do
                await cpAlive /\ ~quarantined /\ instState = "running";
                await wakesPending > 0 /\ attachPending = 0;
                await gen <= MaxGen;
                \* Durable append first, then the dispatch intent: a crash
                \* between the two leaves an unused blessed number, which the
                \* fence comment calls harmless (stateful_manager.ex ~L650).
                AdvanceWatermark(gen);
                attachPending := gen;
            end with;

        or
            \* -- DispatchAttach: the boot RPC lands; noded records the CP-
            \* issued value into the ledger (strictly monotonic) and the
            \* volume serves. This is the ONE place a wake turns into service,
            \* and it is guarded on the quarantine flag (NoServeQuarantined's
            \* tripwire). When the issued value is NOT past the node ledger
            \* (the watermark lagged behind a self-bump), noded's RecordBlessed
            \* REFUSES it and the boot fails: single-flight releases (the
            \* parked wake stays parked), attachPending clears, and the sweep
            \* re-plans once FoldReport adoption has caught the watermark up.
            with dummy \in {0} do
                await cpAlive /\ attachPending > 0;
                await ~quarantined;
                if attachPending > volGen then
                    if quarantined then
                        servedWhileQuarantined := TRUE;
                    end if;
                    volGen := attachPending ||
                    volBlessed := TRUE ||
                    liveVm := "vm1" ||
                    instState := "running" ||
                    wakesPending := wakesPending - 1 ||
                    attachPending := 0;
                else
                    \* RecordBlessed refused (gen <= cur): failed dispatch,
                    \* retry after reconciliation.
                    attachPending := 0;
                end if;
            end with;

        or
            \* -- ActivatorWake: the anchor's node-local wake during
            \* control-plane absence. Grant lane first: consume the next
            \* generation from the lease (clamped past the ledger), recorded
            \* blessed. Fallback: the unblessable self-bump, exactly +1,
            \* blessed marker left behind (the physical attach fence is what
            \* keeps it single-writer; adoption backfills the watermark on
            \* return). Either way one parked wake is served.
            with dummy \in {0} do
                await ~cpAlive /\ wakesPending > 0 /\ ~quarantined;
                await instState = "running";
                either
                    \* Grant lane (delegated advancement).
                    with g = NextFromLease do
                        await LeaseUndrained /\ g < leaseEnd;
                        volGen := g ||
                        volBlessed := TRUE ||
                        leaseCur := g + 1 ||
                        wakesPending := wakesPending - 1;
                    end with;
                or
                    \* Exhausted/absent grant: the self-bump fallback.
                    await ~LeaseServable /\ volGen < MaxGen;
                    volGen := volGen + 1 ||
                    volBlessed := FALSE ||
                    wakesPending := wakesPending - 1;
                end either;
            end with;

        or
            \* -- GrantLease: ensure_blessing_lease grants the anchor a fresh
            \* range when none is outstanding: start past both the watermark
            \* and the reported ledger, sized LeaseSize. Anchor-scoped by
            \* construction (the volume-row node_id match); sized inside the
            \* model's generation cap so a grant never points past MaxGen.
            with s = IF blessedGen >= volGen THEN blessedGen + 1
                     ELSE volGen + 1 do
                await cpAlive /\ ~quarantined;
                await ~LeaseUndrained /\ s + LeaseSize <= MaxGen;
                leaseCur := s ||
                leaseEnd := s + LeaseSize;
            end with;

        or
            \* -- ExpireGrant: the decided expires_at: an undrained grant
            \* lapses during control-plane absence (modeled; not yet built).
            with dummy \in {0} do
                await GrantExpires /\ ~cpAlive /\ LeaseUndrained;
                leaseCur := NULL || leaseEnd := NULL;
            end with;

        or
            \* -- PauseForCheckpoint: the interruptible-bank checkpoint pauses
            \* the VM. Only taken with ledger headroom so the resolve (which
            \* must advance the generation) stays executable inside MaxGen.
            with dummy \in {0} do
                await cpAlive /\ instState = "running" /\ liveVm # NULL;
                await ~ckptPresent /\ ~pendingRec /\ attachPending = 0;
                await volGen < MaxGen /\ blessedGen < MaxGen;
                instState := "paused" ||
                pendRecVm := liveVm ||
                pendRecGen := volGen ||
                pendingRec := TRUE;
            end with;

        or
            \* -- RecordCheckpointDispatch: the CP durably appends the
            \* checkpoint_dispatched record (write-through). A crash before
            \* this lands is the narrow window ADR 017 leaves fail-closed.
            with dummy \in {0} do
                await cpAlive /\ pendingRec;
                ckptPresent := TRUE ||
                ckptVm := pendRecVm ||
                ckptGen := pendRecGen ||
                pendingRec := FALSE;
            end with;

        or
            \* -- ResolveAbortByCP: the CP drives the abort: it ISSUES the
            \* next generation (bless + record in one durable step) and the
            \* dispatch record is consumed so a resolved checkpoint can never
            \* auto-heal a later unrelated +1. Unlike the wake path this is
            \* not gated on the quarantine flag (a resolve is recovery of
            \* live work, not a new placement), and a successful bless clears
            \* quarantine by definition (do_bless_append). If the issued value
            \* is not past the node ledger, noded's RecordBlessed REFUSES it
            \* and the abort PROCEEDS at the current generation with the wire
            \* marker left behind: the CP-side bless still landed durably,
            \* which is exactly abortCheckpoint's genErr -> "proceeding"
            \* branch (stateful.go ~L618-622).
            with gen = NextBlessed do
                await cpAlive /\ instState = "paused";
                await gen <= MaxGen;
                AdvanceWatermark(gen);
                volGen := IF gen > volGen THEN gen ELSE volGen ||
                volBlessed := gen > volGen ||
                quarantined := FALSE ||
                ckptPresent := FALSE ||
                pendingRec := FALSE ||
                pendRecVm := NULL ||
                pendRecGen := 0 ||
                instState := "running";
            end with;

        or
            \* -- AutoAbortCheckpoint: noded's resolve-timeout backstop, NOT
            \* gated by the silence timeout (resuming live work). blessedGeneration
            \* 0: BumpGeneration advances the ledger by exactly +1 on the SAME
            \* vm_id and leaves the blessed marker behind, so the next report
            \* reads forward-unblessed. The CP's record is NOT consumed (the
            \* node does not know it); the heal consumes it.
            with dummy \in {0} do
                await instState = "paused" /\ autoAborts < MaxAutoAborts;
                await volGen < MaxGen;
                volGen := volGen + 1 ||
                volBlessed := FALSE ||
                instState := "running" ||
                autoAborts := autoAborts + 1;
            end with;

        or
            \* -- ReportVolume: a node scrapes onto the report channel.
            \* AdversarialReports (safety mode): the adversary picks the
            \* reporter, any LAGGED generation, and the claimed wire bit: an
            \* over-approximation of straggler + misattributed reports,
            \* conservative for the invariants. Truthful mode (liveness): the
            \* periodic scrape carries only each node's CURRENT view.
            if AdversarialReports then
                with g \in 1..volGen, b \in BOOLEAN, n \in Nodes do
                    pushReport([g |-> g, b |-> b, n |-> n]);
                end with;
            else
                with n \in Nodes do
                    pushReport([g |-> volGen, b |-> volBlessed, n |-> n]);
                end with;
            end if;

        or
            \* -- FoldReport: the CP folds one report: upsert_volume's
            \* monotonic floor keeps the stored pair-key behind the scenes
            \* (protocol 2's concern, omitted here) and update_quarantine's
            \* ladder runs in the implementation's exact order.
            with msg = Head(reportCh) do
                await cpAlive /\ reportCh # << >>;
                reportCh := Tail(reportCh);
                with fwd = ForwardUnblessed(msg.g, msg.b) do
                    if fwd /\ AutoHealEnabled /\ HealSignature(msg.g) then
                        \* AUTO-HEAL (ADR embervm/017): bless the reported
                        \* generation forward through the same durable write
                        \* path as any bless, consume the record.
                        AdvanceWatermark(msg.g);
                        quarantined := FALSE ||
                        ckptPresent := FALSE;
                    elsif fwd /\ ckptPresent then
                        \* Fail closed under a LIVE checkpoint context: a jump
                        \* that does not match the record is the second-writer
                        \* hazard, quarantined EVEN FROM THE ANCHOR. Witness:
                        \* if the signature DID match, this quarantine is the
                        \* historical false-positive (pre-017 posture).
                        if HealSignature(msg.g) then
                            falseQuarantineHeal := TRUE;
                        end if;
                        quarantined := TRUE;
                    elsif fwd /\ msg.n = AnchorNode then
                        \* Fenced-writer adoption (ADR embervm/014): no
                        \* checkpoint context and the reporter IS the anchor,
                        \* so the single writer ran ahead of a rewound
                        \* watermark. Adopt: advance the watermark, clear.
                        \* Witness for the pre-adoption fence that deadlocked
                        \* demo-postgres instead.
                        if AdoptFencedWriter then
                            AdvanceWatermark(msg.g);
                            quarantined := FALSE;
                        else
                            falseQuarantineLag := TRUE;
                            quarantined := TRUE;
                        end if;
                    elsif fwd then
                        \* A forward-unblessed jump from a NON-anchor node is
                        \* genuine split-brain evidence: fail closed. Correct,
                        \* and never witnessed as false.
                        quarantined := TRUE;
                    else
                        \* Not forward-unblessed: an agreeing or at/below-
                        \* watermark report clears the flag, EXCEPT a blessed
                        \* report past the watermark while already quarantined
                        \* (lease-derived or self-claimed, uncorroborated):
                        \* the a42c819d7 clear-suppression shield keeps it.
                        if quarantined /\ msg.b /\ msg.g > blessedGen then
                            quarantined := TRUE;
                        else
                            quarantined := FALSE;
                        end if;
                    end if;
                end with;
            end with;

        or
            \* -- CrashCP: control-plane death. Volatile state is lost: the
            \* in-flight bless (unused blessed number, harmless), the unwritten
            \* dispatch record (the narrow fail-closed window), and the report
            \* channel. Durable ledger, grants, and dispatch records survive.
            await cpAlive /\ cpCrashes < MaxCPCrashes;
            cpAlive := FALSE ||
            cpCrashes := cpCrashes + 1 ||
            attachPending := 0 ||
            pendingRec := FALSE ||
            pendRecVm := NULL ||
            pendRecGen := 0 ||
            reportCh := << >>;

        or
            \* -- RestartCP: boot. The ETS rebuild starts UN-quarantined (the
            \* flag is a report-derived fact, never durable); the next report
            \* re-derives it.
            await ~cpAlive;
            cpAlive := TRUE ||
            quarantined := FALSE;

        or
            \* -- RogueSecondWriter: the attack the fence exists for: a second
            \* writer on a foreign brick jumps the ledger PAST +1 under a NEW
            \* vm_id while the control plane is absent. Issues nothing; its
            \* reports only ever yield (correct) quarantine evidence.
            with dummy \in {0} do
                await ~cpAlive /\ rogueBudget > 0 /\ instState # "paused";
                await volGen + 2 <= MaxGen;
                volGen := volGen + 2 ||
                volBlessed := FALSE ||
                liveVm := "rogue" ||
                rogueBudget := rogueBudget - 1;
            end with;
        end either;
    end while;
end algorithm; *)
\* BEGIN TRANSLATION
VARIABLES volGen, volBlessed, liveVm, instState, reportCh, cpAlive, 
          blessedGen, quarantined, attachPending, leaseCur, leaseEnd, 
          ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, 
          wakesPending, cpCrashes, autoAborts, rogueBudget, 
          servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, 
          blessedGenHW

(* define statement *)
LeaseUndrained == leaseCur # NULL /\ leaseCur < leaseEnd




NextFromLease == IF leaseCur # NULL /\ leaseCur > volGen THEN leaseCur
                 ELSE volGen + 1


LeaseServable == LeaseUndrained /\ NextFromLease < leaseEnd




NextBlessed == IF LeaseUndrained /\ leaseEnd > blessedGen THEN leaseEnd
               ELSE blessedGen + 1




ForwardUnblessed(g, b) == ~b /\ g > blessedGen





HealSignature(g) == ckptPresent /\ g = ckptGen + 1
                    /\ liveVm # NULL /\ ckptVm = liveVm



TypeOK ==
    /\ volGen \in 1..MaxGen
    /\ volBlessed \in BOOLEAN
    /\ blessedGen \in 1..MaxGen
    /\ blessedGenHW \in 1..MaxGen
    /\ liveVm \in VmIds \cup {NULL}
    /\ instState \in {"running", "paused"}
    /\ cpCrashes <= MaxCPCrashes
    /\ autoAborts <= MaxAutoAborts
    /\ rogueBudget \in 0..MaxRogueReports
    /\ wakesPending \in 0..MaxWakes
    /\ attachPending \in 0..MaxGen
    /\ quarantined \in BOOLEAN
    /\ servedWhileQuarantined \in BOOLEAN
    /\ falseQuarantineHeal \in BOOLEAN
    /\ falseQuarantineLag \in BOOLEAN
    /\ leaseCur \in {NULL} \cup 1..MaxGen
    /\ leaseEnd \in {NULL} \cup 1..MaxGen
    /\ ckptPresent \in BOOLEAN
    /\ ckptVm \in VmIds \cup {NULL}
    /\ ckptGen \in 0..MaxGen
    /\ pendingRec \in BOOLEAN
    /\ pendRecVm \in VmIds \cup {NULL}
    /\ pendRecGen \in 0..MaxGen
    /\ AnchorNode \in Nodes
    /\ reportCh \in Seq([g : 1..MaxGen, b : BOOLEAN, n : Nodes])
    /\ Len(reportCh) <= ChanDepth




WatermarkNeverRegresses == blessedGen >= blessedGenHW



NoServeQuarantined == servedWhileQuarantined = FALSE





NoFalseQuarantine ==
    /\ falseQuarantineHeal = FALSE
    /\ falseQuarantineLag = FALSE









LeaseWellFormed ==
    leaseCur # NULL =>
        /\ leaseEnd # NULL
        /\ leaseCur <= leaseEnd
        /\ leaseEnd <= MaxGen
















EventuallyServed ==
    (wakesPending > 0) ~>
        (wakesPending = 0 \/ blessedGen >= MaxGen
         \/ (quarantined /\ ckptPresent))


vars == << volGen, volBlessed, liveVm, instState, reportCh, cpAlive, 
           blessedGen, quarantined, attachPending, leaseCur, leaseEnd, 
           ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, 
           wakesPending, cpCrashes, autoAborts, rogueBudget, 
           servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, 
           blessedGenHW >>

Init == (* Global variables *)
        /\ volGen = 1
        /\ volBlessed = TRUE
        /\ liveVm = "vm1"
        /\ instState = "running"
        /\ reportCh = << >>
        /\ cpAlive = TRUE
        /\ blessedGen = 1
        /\ quarantined = FALSE
        /\ attachPending = 0
        /\ leaseCur = NULL
        /\ leaseEnd = NULL
        /\ ckptPresent = FALSE
        /\ ckptVm = NULL
        /\ ckptGen = 0
        /\ pendingRec = FALSE
        /\ pendRecVm = NULL
        /\ pendRecGen = 0
        /\ wakesPending = 0
        /\ cpCrashes = 0
        /\ autoAborts = 0
        /\ rogueBudget = MaxRogueReports
        /\ servedWhileQuarantined = FALSE
        /\ falseQuarantineHeal = FALSE
        /\ falseQuarantineLag = FALSE
        /\ blessedGenHW = 1

Next == \/ /\ \E dummy \in {0}:
                /\ wakesPending < MaxWakes
                /\ wakesPending' = wakesPending + 1
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, reportCh, cpAlive, blessedGen, quarantined, attachPending, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ LET gen == NextBlessed IN
                /\ cpAlive /\ ~quarantined /\ instState = "running"
                /\ wakesPending > 0 /\ attachPending = 0
                /\ gen <= MaxGen
                /\ blessedGen' = gen
                /\ blessedGenHW' = (IF gen > blessedGenHW THEN gen ELSE blessedGenHW)
                /\ attachPending' = gen
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, reportCh, cpAlive, quarantined, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag>>
        \/ /\ \E dummy \in {0}:
                /\ cpAlive /\ attachPending > 0
                /\ ~quarantined
                /\ IF attachPending > volGen
                      THEN /\ IF quarantined
                                 THEN /\ servedWhileQuarantined' = TRUE
                                 ELSE /\ TRUE
                                      /\ UNCHANGED servedWhileQuarantined
                           /\ /\ attachPending' = 0
                              /\ instState' = "running"
                              /\ liveVm' = "vm1"
                              /\ volBlessed' = TRUE
                              /\ volGen' = attachPending
                              /\ wakesPending' = wakesPending - 1
                      ELSE /\ attachPending' = 0
                           /\ UNCHANGED << volGen, volBlessed, liveVm, 
                                           instState, wakesPending, 
                                           servedWhileQuarantined >>
           /\ UNCHANGED <<reportCh, cpAlive, blessedGen, quarantined, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, cpCrashes, autoAborts, rogueBudget, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ ~cpAlive /\ wakesPending > 0 /\ ~quarantined
                /\ instState = "running"
                /\ \/ /\ LET g == NextFromLease IN
                           /\ LeaseUndrained /\ g < leaseEnd
                           /\ /\ leaseCur' = g + 1
                              /\ volBlessed' = TRUE
                              /\ volGen' = g
                              /\ wakesPending' = wakesPending - 1
                   \/ /\ ~LeaseServable /\ volGen < MaxGen
                      /\ /\ volBlessed' = FALSE
                         /\ volGen' = volGen + 1
                         /\ wakesPending' = wakesPending - 1
                      /\ UNCHANGED leaseCur
           /\ UNCHANGED <<liveVm, instState, reportCh, cpAlive, blessedGen, quarantined, attachPending, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ LET s == IF blessedGen >= volGen THEN blessedGen + 1
                       ELSE volGen + 1 IN
                /\ cpAlive /\ ~quarantined
                /\ ~LeaseUndrained /\ s + LeaseSize <= MaxGen
                /\ /\ leaseCur' = s
                   /\ leaseEnd' = s + LeaseSize
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, reportCh, cpAlive, blessedGen, quarantined, attachPending, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ GrantExpires /\ ~cpAlive /\ LeaseUndrained
                /\ /\ leaseCur' = NULL
                   /\ leaseEnd' = NULL
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, reportCh, cpAlive, blessedGen, quarantined, attachPending, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ cpAlive /\ instState = "running" /\ liveVm # NULL
                /\ ~ckptPresent /\ ~pendingRec /\ attachPending = 0
                /\ volGen < MaxGen /\ blessedGen < MaxGen
                /\ /\ instState' = "paused"
                   /\ pendRecGen' = volGen
                   /\ pendRecVm' = liveVm
                   /\ pendingRec' = TRUE
           /\ UNCHANGED <<volGen, volBlessed, liveVm, reportCh, cpAlive, blessedGen, quarantined, attachPending, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ cpAlive /\ pendingRec
                /\ /\ ckptGen' = pendRecGen
                   /\ ckptPresent' = TRUE
                   /\ ckptVm' = pendRecVm
                   /\ pendingRec' = FALSE
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, reportCh, cpAlive, blessedGen, quarantined, attachPending, leaseCur, leaseEnd, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ LET gen == NextBlessed IN
                /\ cpAlive /\ instState = "paused"
                /\ gen <= MaxGen
                /\ blessedGen' = gen
                /\ blessedGenHW' = (IF gen > blessedGenHW THEN gen ELSE blessedGenHW)
                /\ /\ ckptPresent' = FALSE
                   /\ instState' = "running"
                   /\ pendRecGen' = 0
                   /\ pendRecVm' = NULL
                   /\ pendingRec' = FALSE
                   /\ quarantined' = FALSE
                   /\ volBlessed' = (gen > volGen)
                   /\ volGen' = (IF gen > volGen THEN gen ELSE volGen)
           /\ UNCHANGED <<liveVm, reportCh, cpAlive, attachPending, leaseCur, leaseEnd, ckptVm, ckptGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag>>
        \/ /\ \E dummy \in {0}:
                /\ instState = "paused" /\ autoAborts < MaxAutoAborts
                /\ volGen < MaxGen
                /\ /\ autoAborts' = autoAborts + 1
                   /\ instState' = "running"
                   /\ volBlessed' = FALSE
                   /\ volGen' = volGen + 1
           /\ UNCHANGED <<liveVm, reportCh, cpAlive, blessedGen, quarantined, attachPending, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ IF AdversarialReports
                 THEN /\ \E g \in 1..volGen:
                           \E b \in BOOLEAN:
                             \E n \in Nodes:
                               IF Len(reportCh) < ChanDepth
                                  THEN /\ reportCh' = Append(reportCh, ([g |-> g, b |-> b, n |-> n]))
                                  ELSE /\ reportCh' = Append(Tail(reportCh), ([g |-> g, b |-> b, n |-> n]))
                 ELSE /\ \E n \in Nodes:
                           IF Len(reportCh) < ChanDepth
                              THEN /\ reportCh' = Append(reportCh, ([g |-> volGen, b |-> volBlessed, n |-> n]))
                              ELSE /\ reportCh' = Append(Tail(reportCh), ([g |-> volGen, b |-> volBlessed, n |-> n]))
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, cpAlive, blessedGen, quarantined, attachPending, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ LET msg == Head(reportCh) IN
                /\ cpAlive /\ reportCh # << >>
                /\ reportCh' = Tail(reportCh)
                /\ LET fwd == ForwardUnblessed(msg.g, msg.b) IN
                     IF fwd /\ AutoHealEnabled /\ HealSignature(msg.g)
                        THEN /\ blessedGen' = msg.g
                             /\ blessedGenHW' = (IF (msg.g) > blessedGenHW THEN (msg.g) ELSE blessedGenHW)
                             /\ /\ ckptPresent' = FALSE
                                /\ quarantined' = FALSE
                             /\ UNCHANGED << falseQuarantineHeal, 
                                             falseQuarantineLag >>
                        ELSE /\ IF fwd /\ ckptPresent
                                   THEN /\ IF HealSignature(msg.g)
                                              THEN /\ falseQuarantineHeal' = TRUE
                                              ELSE /\ TRUE
                                                   /\ UNCHANGED falseQuarantineHeal
                                        /\ quarantined' = TRUE
                                        /\ UNCHANGED << blessedGen, 
                                                        falseQuarantineLag, 
                                                        blessedGenHW >>
                                   ELSE /\ IF fwd /\ msg.n = AnchorNode
                                              THEN /\ IF AdoptFencedWriter
                                                         THEN /\ blessedGen' = msg.g
                                                              /\ blessedGenHW' = (IF (msg.g) > blessedGenHW THEN (msg.g) ELSE blessedGenHW)
                                                              /\ quarantined' = FALSE
                                                              /\ UNCHANGED falseQuarantineLag
                                                         ELSE /\ falseQuarantineLag' = TRUE
                                                              /\ quarantined' = TRUE
                                                              /\ UNCHANGED << blessedGen, 
                                                                              blessedGenHW >>
                                              ELSE /\ IF fwd
                                                         THEN /\ quarantined' = TRUE
                                                         ELSE /\ IF quarantined /\ msg.b /\ msg.g > blessedGen
                                                                    THEN /\ quarantined' = TRUE
                                                                    ELSE /\ quarantined' = FALSE
                                                   /\ UNCHANGED << blessedGen, 
                                                                   falseQuarantineLag, 
                                                                   blessedGenHW >>
                                        /\ UNCHANGED falseQuarantineHeal
                             /\ UNCHANGED ckptPresent
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, cpAlive, attachPending, leaseCur, leaseEnd, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined>>
        \/ /\ cpAlive /\ cpCrashes < MaxCPCrashes
           /\ /\ attachPending' = 0
              /\ cpAlive' = FALSE
              /\ cpCrashes' = cpCrashes + 1
              /\ pendRecGen' = 0
              /\ pendRecVm' = NULL
              /\ pendingRec' = FALSE
              /\ reportCh' = << >>
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, blessedGen, quarantined, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, wakesPending, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ ~cpAlive
           /\ /\ cpAlive' = TRUE
              /\ quarantined' = FALSE
           /\ UNCHANGED <<volGen, volBlessed, liveVm, instState, reportCh, blessedGen, attachPending, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, rogueBudget, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>
        \/ /\ \E dummy \in {0}:
                /\ ~cpAlive /\ rogueBudget > 0 /\ instState # "paused"
                /\ volGen + 2 <= MaxGen
                /\ /\ liveVm' = "rogue"
                   /\ rogueBudget' = rogueBudget - 1
                   /\ volBlessed' = FALSE
                   /\ volGen' = volGen + 2
           /\ UNCHANGED <<instState, reportCh, cpAlive, blessedGen, quarantined, attachPending, leaseCur, leaseEnd, ckptPresent, ckptVm, ckptGen, pendingRec, pendRecVm, pendRecGen, wakesPending, cpCrashes, autoAborts, servedWhileQuarantined, falseQuarantineHeal, falseQuarantineLag, blessedGenHW>>

Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(Next)

\* END TRANSLATION

(*****************************************************************************)
(* Fairness for the liveness configs. The adversarial actions (RequestWake,   *)
(* GrantLease, ExpireGrant, PauseForCheckpoint, RecordCheckpointDispatch,      *)
(* AutoAbortCheckpoint, CrashCP, RogueSecondWriter) get NO fairness: the       *)
(* checker may never fire them, their budgets bound how often they can, and    *)
(* liveness must hold in spite of them, not because of them. The progress      *)
(* branches get STRONG fairness, attached per branch through before/after      *)
(* fingerprints so the obligation pins to the branch itself and cannot be      *)
(* discharged by unrelated steps (adoption.tla's FairSpec discipline). Strong, *)
(* not weak, because each branch is only intermittently enabled (an empty      *)
(* channel, a paused VM, the CP down), so WF could skip it across the gaps.    *)
(*                                                                           *)
(* Report pushes are quantified per CONTENT (generation, wire bit, reporter):  *)
(* a single push-fairness obligation could be discharged forever by junk       *)
(* reports while the one truthful report the reconciliation needs never lands. *)
(* Per-content SF forces every distinct report to keep flowing, which is what  *)
(* the real streamer does; the bounded channel depth then guarantees a queued  *)
(* report is folded within a bounded number of folds.                          *)
(*****************************************************************************)

\* A report with exactly this content was appended to the channel (either
\* onto room, or displacing the oldest entry at depth). The depth guard on
\* each arm keeps Tail off the empty sequence, mirroring pushReport's IF.
DidPush(g, b, n) ==
    LET m == [g |-> g, b |-> b, n |-> n] IN
        \/ /\ Len(reportCh) < ChanDepth
           /\ reportCh' = Append(reportCh, m)
        \/ /\ Len(reportCh) = ChanDepth
           /\ reportCh' = Append(Tail(reportCh), m)

\* A report was folded (only FoldReport shrinks the channel).
DidFold == Len(reportCh') < Len(reportCh)

\* A bless was issued ahead of its attach (the fence window opened):
\* attachPending moved 0 -> gen (only BlessWake does that).
DidBless == attachPending' > attachPending

\* A parked wake was served (only DispatchAttach's success arm and
\* ActivatorWake drain the park).
DidServe == wakesPending' = wakesPending - 1

\* An issued bless reached a DISPATCH OUTCOME: the attach either landed or
\* noded refused it and single-flight released (attachPending cleared for a
\* fresh bless). Without this obligation a refused value could sit in
\* attachPending forever while reports cycle, starving the retry that would
\* skip the ledger. Both arms are control-plane-owned progress, so strong
\* fairness is the same discipline as DidBless.
DidDispatchOutcome == attachPending # 0 /\ attachPending' # attachPending

\* The crashed control plane booted.
DidRestart == ~cpAlive /\ cpAlive'

\* A paused checkpoint was resolved by the CP drive (blessing forward). This
\* fingerprint separates ResolveAbortByCP from AutoAbortCheckpoint, which also
\* unpauses but never advances the blessing watermark: resolve gets fairness,
\* the node-side backstop stays adversarial.
DidResolve == instState' = "running" /\ blessedGen' > blessedGen

FairSpec ==
    /\ Spec
    /\ SF_vars(Next /\ DidBless)
    /\ SF_vars(Next /\ DidDispatchOutcome)
    /\ SF_vars(Next /\ DidServe)
    /\ SF_vars(Next /\ DidFold)
    /\ SF_vars(Next /\ DidRestart)
    /\ SF_vars(Next /\ DidResolve)
    /\ \A g \in 1..MaxGen : \A b \in BOOLEAN : \A n \in Nodes :
          SF_vars(Next /\ DidPush(g, b, n))

=============================================================================
