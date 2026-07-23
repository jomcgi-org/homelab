-------------------------------- MODULE adoption --------------------------------
(*****************************************************************************)
(* EmberVM VM-lifecycle + adoption protocol (ADR embervm/006, pilot spec).   *)
(*                                                                           *)
(* This models the control plane's two independent views of shared VM state  *)
(* against a live node daemon (noded) and an adversarial scheduler that can   *)
(* crash and restart either side between any two steps. It formalizes the     *)
(* invariants the bug history proves matter: the dispatch restart wedge, the  *)
(* forget-before-kill straggler resurrection, and the reap-would-wipe-fleet   *)
(* guard.                                                                     *)
(*                                                                           *)
(* WORKER AUTHORITY (ADR embervm/014). The node-side variables (vmState,      *)
(* vmNode, vmPrincipal) are the TRUTH: what the node holds is what exists.    *)
(* The control-plane inventory is a reconciled CACHE, at all times and not    *)
(* only across a restart: it is volatile (wiped on CrashCP), it is rebuilt    *)
(* additively from node reports (AdoptInventory), and the CP never destroys or *)
(* asserts state off its own cache when a fresh node report contradicts it     *)
(* (the reap guard reads the node's last accepted report, never the cache      *)
(* alone). Adoption's additive-only reconcile IS the "cache converges toward   *)
(* node truth" rule; nothing here lets the cache overwrite node ground truth.  *)
(*                                                                           *)
(* NODE-CONFIRMED DESTRUCTION (ADR embervm/014 decision 5). Destruction is the *)
(* one carve-out from relaxed consistency: the CP may record an instance       *)
(* destroyed only AFTER its owning node confirms teardown. This spec models    *)
(* that as a two-phase destroy (BeginDestroy records durable intent while the  *)
(* VM is still live on the node; ConfirmDestroy fires only once the node has    *)
(* torn the VM down), and the NoDestroyBeforeConfirm invariant asserts the CP's *)
(* destroyed record never precedes the node's actual teardown.                 *)
(*                                                                           *)
(* PROSE MAP: each PlusCal action abstracts a concrete implementation site.   *)
(* Keep this current with the actions below (the layer-1 vocabulary test      *)
(* asserts the modeled names appear in this file).                           *)
(*                                                                           *)
(*   Prime(n,v,p)        ~ Embervm.Dispatcher deposit path + put_vm_if_unknown/4 *)
(*                          (dispatcher.ex handle_cast({:deposit,...}) ~L294,  *)
(*                          put_vm_if_unknown ~L1015, known_vm_ids ~L1028).    *)
(*   SendStatus(n)       ~ noded WatchNode stream emitting a NodeStatus tagged *)
(*                          with the streamer pid (node_registry.ex           *)
(*                          start_streamer ~L756, WatchNode in node.proto).    *)
(*   RecvStatus(n)       ~ NodeRegistry {:node_status, streamer, status}       *)
(*                          handler + apply_status: the pid/generation guard   *)
(*                          that drops a straggler is the model's gen check    *)
(*                          (forget_streamer ~L799, handle_node_down ~L718     *)
(*                          forget-before-kill comment).                       *)
(*   AdoptInventory(n)   ~ Embervm.Dispatcher.adopt_inventory/1 (dispatcher.ex *)
(*                          ~L995): additive reconcile of node-reported primed *)
(*                          vm_ids into inventory via put_vm_if_unknown.        *)
(*   DispatchWarm(t)     ~ Dispatcher.reserve_vm/4 :warm path (claim from      *)
(*                          inventory) then Assign RPC (node.proto Assign).     *)
(*   DispatchMiss(t)     ~ Dispatcher miss path: Prime a fresh VM, the         *)
(*                          {:vm_primed, pid, vm_id} handler stamping the       *)
(*                          worker meta so known_vm_ids counts it (dispatcher.ex *)
(*                          handle_info({:vm_primed,...}) ~L322), then Assign.  *)
(*   AgeToUnknown(n)     ~ NodeRegistry.evaluate_node_age -> :unknown          *)
(*                          (node_registry.ex ~L672 unknown_after_ms edge).     *)
(*   AgeToDown(n)        ~ NodeRegistry.evaluate_node_age -> :down, which runs  *)
(*                          apply_health_transition -> handle_node_down/2       *)
(*                          (node_registry.ex ~L697/~L729): reassign then       *)
(*                          forget-before-kill.                                 *)
(*   Reap(n,v)           ~ Dispatcher reap of a VM believed orphaned; guarded   *)
(*                          against the node's last accepted report (the        *)
(*                          reap-would-wipe-fleet review catch).                *)
(*   CrashCP / RestartCP ~ control-plane process death + boot: volatile        *)
(*                          inventory/health wiped, durable op-log (taskState)  *)
(*                          survives, boot_sweep re-queues in-flight tasks       *)
(*                          (dispatcher.ex handle_continue(:boot_sweep) ~L283,   *)
(*                          run_sweep -> adopt_inventory).                       *)
(*   CrashNode(n)        ~ node daemon / VM host death: node-side VMs vanish,    *)
(*                          but a NodeStatus already on the wire survives (the   *)
(*                          straggler).                                         *)
(*   AbandonClaim(t)     ~ Dispatcher miss-worker DOWN handler: a mid-miss VM    *)
(*                          died before Assign landed, so drop the claim and     *)
(*                          retry the task (dispatcher.ex :DOWN -> finish_worker  *)
(*                          transport retry).                                    *)
(*   RecycleId(v)        ~ not a code action: models the inexhaustible vm_id      *)
(*                          supply, so a destroyed slot can back a fresh Prime.   *)
(*   Reconnect(n)        ~ NodeRegistry backoff reconnect after a stream ends /   *)
(*                          node-down (node_registry.ex ~L742 schedule_reconnect, *)
(*                          start_streamer): a new streamer under a fresh gen.    *)
(*   Succeed(t)          ~ a task's assigned VM completes: the CP appends the      *)
(*                          durable :*_destroying intent op and issues the         *)
(*                          Destroy RPC, but the VM stays LIVE on the node until    *)
(*                          it confirms teardown (session_manager.ex do_destroy/2   *)
(*                          under EMBERVM_NODE_CONFIRMED_DESTROY: intent op ->       *)
(*                          RPC -> confirmed -> :*_destroyed).                       *)
(*   ConfirmDestroy(v)   ~ noded Destroy/reap completes and returns                 *)
(*                          teardown_confirmed=true (server.go Destroy, reap):       *)
(*                          the node tears the microVM + scratch down, THEN the CP   *)
(*                          appends the durable :*_destroyed record and drops the    *)
(*                          instance. The node teardown always precedes the CP's     *)
(*                          destroyed record, never the reverse (decision 5).        *)
(*****************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Nodes,            \* model set of node ids
    VMs,              \* model set of vm ids
    Tasks,            \* model set of task ids
    Principals,       \* model set of principal (session-lineage) ids
    NULL,             \* the absent value (no node / no vm / forgotten generation)
    AdoptionEnabled,  \* TRUE models adopt_inventory/1 existing; FALSE is the pre-adoption wedge
    ForgetBeforeKill, \* TRUE models the D-R2.7.2 forget-before-kill ordering; FALSE is the buggy order
    AgingEnabled,     \* TRUE lets nodes age out healthy->unknown->down (needed for the
                      \* down-edge and straggler paths). The LIVENESS configs set it
                      \* FALSE: unbounded silence-driven age-out lets the adversary cycle
                      \* a node down forever and starve dispatch, which is a scheduler
                      \* artifact (a node reporting status stays healthy), not a real
                      \* wedge. The liveness property is about the CP crash-restart, not
                      \* node aging, so disabling age-out there is faithful and sound.
    \* Crash budgets + cyclic generation size, set per cfg, keep the state space
    \* finite. The rich SAFETY config uses both crash budgets to explore
    \* interleavings; the LIVENESS configs (positive dispatch + wedge) use lean
    \* budgets because the expensive temporal check needs a small graph and the
    \* properties it proves (dispatch progresses; the wedge strands it) do not
    \* require node death, only the CP crash-restart. (VM-id recycling is
    \* unconditional, modeling an inexhaustible id supply; see RecycleId.)
    MaxCPCrashes,     \* how many control-plane crash-restarts a behavior may take
    MaxNodeCrashes,   \* how many node host crashes a behavior may take
    MaxGen            \* size of the cyclic (re)connect generation set (Reconnect)

\* Status channel depth: a bounded per-node FIFO (a straggler lives here).
ChanDepth == 2

Health == {"starting", "healthy", "unknown", "down"}

\* A fixed initial principal per task so every task starts submitted and the
\* initial state is unique. Spreads tasks across principals when there are enough.
InitPrincipal ==
    LET seq == CHOOSE f \in [Tasks -> Principals] : TRUE
    IN seq

(*--fair algorithm adoption

variables
    \* -- node side (durable across a CP crash, wiped by a node crash) --------
    \* Which node hosts a VM, its lifecycle state, and the principal it was
    \* primed under. "free" = not yet primed anywhere.
    vmState = [v \in VMs |-> "free"],
    vmNode  = [v \in VMs |-> NULL],
    vmPrincipal = [v \in VMs |-> NULL],

    \* -- status channel (per node, bounded FIFO of NodeStatus reports) -------
    \* Each message is [gen |-> g, primed |-> set, assigned |-> set]. A message
    \* carries the streamer generation the node was connected under; it survives
    \* a node crash (that is precisely the straggler). Depth bounded to ChanDepth.
    statusCh = [n \in Nodes |-> << >>],

    \* -- CP side (volatile unless noted) ------------------------------------
    cpAlive = TRUE,
    health = [n \in Nodes |-> "starting"],
    \* The generation the CP currently accepts status under; NULL means forgotten
    \* (streamer pid dropped). A fresh streamer bumps this on (re)connect.
    streamerGen = [n \in Nodes |-> 0],
    \* The generation a node was LAST connected under before being forgotten (NULLed
    \* on the down-edge). The buggy forget-before-kill order (ForgetBeforeKill=FALSE)
    \* re-accepts a straggler tagged with this stale generation.
    lastGen = [n \in Nodes |-> NULL],
    \* The primed pool: a set of <<node, vm>> pairs the dispatcher can assign.
    inventory = {},
    \* Just-claimed miss VMs: in known_vm_ids (dedup basis) but not yet in
    \* inventory, i.e. the {:vm_primed,...} window DispatchMiss opens.
    inflightMeta = {},
    \* The last status report the CP ACCEPTED from each node; the reap guard
    \* reads it. NULL before the first accept.
    lastReport = [n \in Nodes |-> NULL],

    \* -- durable task state (mirrors the op-log; survives a CP crash) --------
    \* Every task is submitted at init under some principal (the liveness property
    \* is over submitted tasks; an unsubmitted task is not a dispatch obligation).
    \* CHOOSE fixes a principal assignment so the initial state is unique.
    taskState = [t \in Tasks |-> "queued"],  \* queued | assigned | succeeded
    taskVM = [t \in Tasks |-> NULL],
    taskPrincipal = [t \in Tasks |-> InitPrincipal[t]],

    \* -- node-confirmed destruction (durable, mirrors the op-log) -------------
    \* The CP's :*_destroying intent set (BeginDestroy appended it, before the
    \* Destroy RPC, so a CP crash mid-destroy resumes the destroy). Durable.
    destroying = {},
    \* The CP's :*_destroyed records (ConfirmDestroy appended them, ONLY after the
    \* node confirmed teardown). Durable. NoDestroyBeforeConfirm asserts every
    \* member's VM is already torn down on the node (vmState = "destroyed").
    cpDestroyed = {},

    \* -- history + budgets ---------------------------------------------------
    \* Set when a reap ever destroyed a VM the node still reported (bug witness).
    reapedLive = FALSE,
    cpCrashes = 0,
    nodeCrashes = 0,
    \* Monotonic generation source so each (re)connect gets a distinct gen and a
    \* straggler's stale gen never collides with a fresh one.
    genCtr = 1;

define
    \* VMs currently primed-and-idle on a node per the CP's inventory.
    InventoryVMs == { p[2] : p \in inventory }
    \* VMs reserved by an in-flight assign worker: assigned to a live task and no
    \* longer parked in inventory. In the implementation these are counted by
    \* known_vm_ids/1 via the worker's meta.vm_id (dispatcher.ex ~L1032). Omitting
    \* them lets adoption re-adopt a VM a warm dispatch just claimed, re-adding it
    \* to inventory for a SECOND task: the double-assign TLC finds without this.
    \* Every VM a task currently holds a claim on: assigned VMs AND a miss VM a
    \* still-"queued" task claimed in DispatchMiss part 1 but has not yet assigned.
    \* In the implementation the miss worker holds meta.vm_id from the moment it
    \* primes, so known_vm_ids counts it across the whole prime->assign window; a
    \* claim keyed only on "assigned" tasks would let adoption re-add a mid-miss VM
    \* to inventory for a second task (a double-assign TLC finds without this).
    ReservedVMs == { taskVM[t] : t \in { u \in Tasks : taskVM[u] # NULL } }
    \* Every vm_id the dispatcher holds: inventory + in-flight miss meta + in-flight
    \* assign reservations. This is the full known_vm_ids/1 dedup basis.
    KnownVMs == (InventoryVMs \cup inflightMeta) \cup ReservedVMs

    \* A live task occupies its VM.
    LiveTasks == { t \in Tasks : taskState[t] = "assigned" }


    \* -- Invariants ---------------------------------------------------------
    TypeOK ==
        /\ vmState \in [VMs -> {"free", "primed", "assigned", "destroying", "destroyed"}]
        /\ health \in [Nodes -> Health]
        /\ taskState \in [Tasks -> {"queued", "assigned", "succeeded"}]
        /\ cpCrashes <= MaxCPCrashes
        /\ nodeCrashes <= MaxNodeCrashes
        /\ \A n \in Nodes : Len(statusCh[n]) <= ChanDepth
        /\ destroying \subseteq VMs
        /\ cpDestroyed \subseteq VMs

    \* No two live tasks ever share a VM. The second clause is the real safety
    \* content: whenever an assigned task's VM is STILL LIVE on a node it is in
    \* "assigned" state (never left "primed", i.e. never dispatched twice). A VM
    \* destroyed out from under a live task (a node crash before the age-out
    \* reassigns) is a legitimate transient, not a double-assign, so it is excluded.
    NoDoubleAssign ==
        /\ \A t1, t2 \in LiveTasks :
             (t1 # t2) => (taskVM[t1] # taskVM[t2] \/ taskVM[t1] = NULL)
        /\ \A t \in LiveTasks :
             (taskVM[t] # NULL /\ vmState[taskVM[t]] \in {"primed", "assigned"})
               => vmState[taskVM[t]] = "assigned"

    \* No VM appears twice in inventory (set semantics give the first half free),
    \* and never in both inventory and the in-flight miss meta.
    AdoptIdempotent == InventoryVMs \cap inflightMeta = {}

    \* A forgotten node (gen NULL) can only be healthy again via a fresh-gen
    \* status. This is forget-before-kill: a straggler must not resurrect it.
    NoResurrection ==
        \A n \in Nodes : streamerGen[n] = NULL => health[n] # "healthy"

    \* A reap never destroyed a VM the node still reported live.
    NoReapLive == reapedLive = FALSE

    \* No LIVE task ever occupies a VM primed under a different principal. Scoped
    \* to assigned tasks: a succeeded task keeps its historical taskVM pointer while
    \* its VM is destroyed and recycled under another principal, which is not an
    \* isolation breach (the assignment is over), so it is excluded.
    PrincipalIsolation ==
        \A t \in LiveTasks :
            taskVM[t] # NULL => vmPrincipal[taskVM[t]] = taskPrincipal[t]

    \* The node-confirmed destruction guarantee (ADR embervm/014 decision 5): the
    \* CP records an instance destroyed only AFTER its owning node has torn it
    \* down. Every VM the CP has appended a :*_destroyed record for is already
    \* "destroyed" on the node (the ground-truth vmState). The buggy inverse (CP
    \* asserts destroyed while the VM is still live on the node) is exactly what
    \* worker authority forbids: only the node that performed the teardown may
    \* truthfully assert it happened.
    NoDestroyBeforeConfirm ==
        \A v \in cpDestroyed : vmState[v] = "destroyed"

    \* A destroy in flight (intent recorded, node not yet confirmed) has NOT been
    \* recorded destroyed by the CP: the durable :*_destroying and :*_destroyed
    \* records are mutually exclusive for a given VM until confirmation moves it.
    DestroyIntentPrecedesRecord ==
        \A v \in destroying : v \notin cpDestroyed

    \* -- Temporal property --------------------------------------------------
    \* Every task that was submitted eventually reaches a terminal-or-assigned
    \* state, ACROSS a CP crash-restart. With AdoptionEnabled=FALSE this fails
    \* (the historical restart wedge: the primed pool is stranded).
    EventuallyDispatched ==
        \A t \in Tasks : (taskState[t] = "queued") ~> (taskState[t] \in {"assigned", "succeeded"})
end define;

macro pushStatus(n, msg) begin
    \* Append to the per-node FIFO, dropping the oldest if at depth bound (a real
    \* bounded mailbox / stream buffer; loss of the oldest, never of ordering).
    if Len(statusCh[n]) < ChanDepth then
        statusCh[n] := Append(statusCh[n], msg);
    else
        statusCh[n] := Append(Tail(statusCh[n]), msg);
    end if;
end macro;

begin
Run:
    while TRUE do
        either
            \* -- Prime(n, v, p): CP primes a fresh VM on n under principal p. ---
            with n \in Nodes, v \in VMs, p \in Principals do
                await cpAlive /\ vmState[v] = "free";
                \* put_vm_if_unknown guard: skip a vm_id we already hold.
                await v \notin KnownVMs;
                vmState[v] := "primed" ||
                vmNode[v] := n ||
                vmPrincipal[v] := p;
                inventory := inventory \cup {<<n, v>>};
            end with;

        or
            \* -- RecycleId(v): a destroyed VM slot returns to the free pool. -----
            \* VM ids in reality are drawn from an inexhaustible supply; the finite
            \* VMs constant is only a bound on how many are live AT ONCE. Recycling a
            \* destroyed slot back to "free" models "the node can always prime a
            \* fresh vm_id" without an unbounded id set, so crash budgets do not
            \* strand queued tasks against an exhausted pool (a modeling artifact,
            \* not a real limit). The slot forgets its old node/principal.
            \* Always available (no budget): VM ids are drawn from an inexhaustible
            \* supply, so a destroyed slot can always return to "free" for a fresh
            \* prime. This keeps priming always-eventually-possible (liveness needs it)
            \* while the state stays finite: vmState is a bounded domain and recycling
            \* just toggles destroyed -> free, adding no counter. A recycle BUDGET was a
            \* monotonic bound that stranded queued tasks once exhausted (an artifact,
            \* not a real limit), so there is none.
            with v \in VMs do
                await vmState[v] = "destroyed" /\ v \notin KnownVMs;
                vmState[v] := "free" || vmNode[v] := NULL || vmPrincipal[v] := NULL;
                \* The slot forgets its destroy history too: a recycled id backs a
                \* FRESH instance, so its old :*_destroyed / :*_destroying records no
                \* longer apply. Dropping it from cpDestroyed keeps NoDestroyBeforeConfirm
                \* about the CURRENT occupant (else a re-primed slot would trip it).
                destroying := destroying \ {v};
                cpDestroyed := cpDestroyed \ {v};
            end with;

        or
            \* -- SendStatus(n): node emits a report under its current gen. ------
            with n \in Nodes do
                await streamerGen[n] # NULL;  \* a connected streamer exists
                pushStatus(n, [ gen |-> streamerGen[n],
                                primed |-> { v \in VMs : vmNode[v] = n /\ vmState[v] = "primed" },
                                assigned |-> { v \in VMs : vmNode[v] = n /\ vmState[v] = "assigned" } ]);
            end with;

        or
            \* -- RecvStatus(n): CP pops one report and applies the gen guard. ---
            with n \in Nodes do
                await cpAlive /\ statusCh[n] # << >>;
                with msg = Head(statusCh[n]) do
                    statusCh[n] := Tail(statusCh[n]);
                    \* Accept guard. Correct (forget-before-kill) order: accept only
                    \* under the current, non-NULL generation. The buggy order
                    \* (ForgetBeforeKill=FALSE) also accepts a straggler whose gen
                    \* matches the node's last-known generation even though the pid
                    \* was already forgotten (streamerGen NULL): the kill happened
                    \* but the forget did not precede it.
                    if \/ (streamerGen[n] # NULL /\ msg.gen = streamerGen[n])
                       \/ (~ForgetBeforeKill /\ streamerGen[n] = NULL /\ msg.gen = lastGen[n] /\ lastGen[n] # NULL) then
                        health[n] := "healthy";
                        lastReport[n] := msg;
                        \* AdoptInventory: additive reconcile of reported primed VMs.
                        \* Skip any vm the CP already holds (known_vm_ids: inventory,
                        \* in-flight miss meta, or an in-flight assign reservation) AND
                        \* any the CP has already ASSIGNED on this node: a just-claimed
                        \* warm VM is still reported "primed" by the node until the
                        \* Assign lands, and re-adopting it would let a second task claim
                        \* the same VM (the known_vm_ids race the adopt_inventory comment
                        \* calls out, dispatcher.ex ~L989).
                        if AdoptionEnabled then
                            inventory := inventory \cup
                                { <<n, v>> : v \in { w \in msg.primed :
                                    w \notin KnownVMs /\ vmState[w] # "assigned" } };
                        end if;
                    end if;
                end with;
            end with;

        or
            \* -- DispatchWarm(t): assign a warm VM from inventory. --------------
            with t \in Tasks, pair \in inventory do
                await cpAlive /\ taskState[t] = "queued";
                await health[pair[1]] = "healthy";
                await vmPrincipal[pair[2]] = taskPrincipal[t];
                inventory := inventory \ {pair};
                taskVM[t] := pair[2];
                taskState[t] := "assigned";
                vmState[pair[2]] := "assigned";
            end with;

        or
            \* -- DispatchMiss part 1: prime a fresh VM into the miss window. ----
            \* Splits Prime and Assign so adoption can interleave between them:
            \* exactly the window known_vm_ids protects. inflightMeta holds the
            \* just-claimed vm_id (the {:vm_primed,...} stamp).
            with t \in Tasks, v \in VMs, n \in Nodes do
                await cpAlive /\ taskState[t] = "queued";
                await health[n] = "healthy" /\ vmState[v] = "free";
                await v \notin KnownVMs;
                vmState[v] := "primed" ||
                vmNode[v] := n ||
                vmPrincipal[v] := taskPrincipal[t];
                inflightMeta := inflightMeta \cup {v};
                taskVM[t] := v;
            end with;

        or
            \* -- DispatchMiss part 2: land the Assign for a claimed miss VM. ----
            \* Keyed off the claimed VM still being primed for this task on a healthy
            \* node, not on inflightMeta membership: a state wipe (CP crash) can clear
            \* inflightMeta while the durable claim (taskVM) survives, and the worker
            \* still completes its assign. inflightMeta is the known_vm_ids dedup
            \* bookkeeping; the assign completes whenever the VM is still ours.
            with t \in Tasks do
                await cpAlive /\ taskState[t] = "queued" /\ taskVM[t] # NULL;
                \* Nested so health[vmNode[..]] is never reached when vmNode is NULL
                \* (TLC does not reliably short-circuit /\ over a bad function apply).
                await vmState[taskVM[t]] = "primed";
                await vmNode[taskVM[t]] # NULL;
                await IF vmNode[taskVM[t]] = NULL THEN FALSE
                      ELSE health[vmNode[taskVM[t]]] = "healthy";
                taskState[t] := "assigned";
                vmState[taskVM[t]] := "assigned";
                inflightMeta := inflightMeta \ {taskVM[t]};
            end with;

        or
            \* -- AbandonClaim(t): a queued task's claimed miss VM is gone (destroyed
            \* by a node crash, or off-node) before part 2 landed: drop the dangling
            \* claim so the task re-dispatches fresh. In the implementation the miss
            \* worker owns the VM end to end; if the VM/node dies the worker fails and
            \* the task retries with no VM (dispatcher.ex DOWN handler retries the
            \* task). This is that reset, and it is what keeps a mid-miss task from
            \* stranding on a claim it can never complete.
            with t \in Tasks do
                await cpAlive /\ taskState[t] = "queued" /\ taskVM[t] # NULL;
                await \/ vmState[taskVM[t]] # "primed"
                      \/ vmNode[taskVM[t]] = NULL
                      \/ (vmNode[taskVM[t]] # NULL /\ health[vmNode[taskVM[t]]] = "down");
                \* The miss worker owned this VM; abandoning the claim destroys it
                \* (a still-"primed" one leaks otherwise). Then drop the claim.
                inflightMeta := inflightMeta \ {taskVM[t]};
                if vmState[taskVM[t]] = "primed" then
                    vmState[taskVM[t]] := "destroyed" || vmNode[taskVM[t]] := NULL;
                end if;
                taskVM[t] := NULL;
            end with;

        or
            \* -- AgeToUnknown(n): health tick to :unknown (capacity retracts). --
            \* Aging is SILENCE-driven (evaluate_node_age keys off time-since-last-
            \* ACCEPTED status). Left unguarded here so the resurrection precondition
            \* stays reachable (a node can age down with a straggler still in flight).
            \* The liveness configs set MaxNodeCrashes = 0 and never force a healthy
            \* node down, so aging does not starve dispatch there; the safety and
            \* resurrection configs need the full age-out path.
            with n \in Nodes do
                await AgingEnabled /\ cpAlive /\ health[n] \in {"healthy", "starting"};
                health[n] := "unknown";
            end with;

        or
            \* -- AgeToDown(n): the down edge. Reassign, then forget-before-kill. -
            with n \in Nodes do
                await AgingEnabled /\ cpAlive /\ health[n] \in {"unknown", "starting", "healthy"};
                health[n] := "down";
                \* ReassignNode: every task assigned on n returns to queued and drops
                \* its claim, and n's entries leave inventory, BEFORE any later adoption
                \* can re-add them (reassign-before-adopt). A silent age-out does NOT
                \* destroy the node's VMs (the node may still be alive, just quiet), so
                \* an assigned VM on n reverts to an idle "primed" warm VM: when the node
                \* reconnects and reports, adoption reclaims it. That mirrors the pool
                \* surviving an age-out and being re-adopted on recovery.
                taskState := [ t \in Tasks |->
                    IF taskVM[t] # NULL /\ vmNode[taskVM[t]] = n /\ taskState[t] = "assigned"
                    THEN "queued" ELSE taskState[t] ];
                taskVM := [ t \in Tasks |->
                    IF taskVM[t] # NULL /\ vmNode[taskVM[t]] = n /\ taskState[t] = "assigned"
                    THEN NULL ELSE taskVM[t] ];
                vmState := [ v \in VMs |->
                    IF vmNode[v] = n /\ vmState[v] = "assigned" THEN "primed" ELSE vmState[v] ];
                inventory := { p \in inventory : p[1] # n };
                inflightMeta := { v \in inflightMeta : vmNode[v] # n };
                lastReport[n] := NULL;
                \* forget-before-kill: drop the pid (gen NULL) so a straggler already
                \* on the wire is rejected by RecvStatus. When ForgetBeforeKill=FALSE
                \* we model the buggy order by leaving genCtr-1 acceptable in
                \* RecvStatus above; here we still NULL the gen (the "kill" happened),
                \* but the buggy accept path re-admits the stale gen (recorded here).
                lastGen[n] := streamerGen[n] ||
                streamerGen[n] := NULL;
            end with;

        or
            \* -- Reconnect(n): a fresh streamer connects under a new generation. -
            \* Reaches a DOWN node too: handle_node_down schedules a backoff reconnect
            \* (node_registry.ex ~L742 schedule_reconnect), so a down node is not
            \* permanently dead, it recovers when its daemon's stream re-registers.
            \* Recovery resets health to "starting"; a fresh accepted status then ages
            \* it back to healthy and re-adopts its still-primed pool.
            with n \in Nodes do
                await cpAlive /\ streamerGen[n] = NULL;
                \* Generations CYCLE through a small finite set (1..MaxGen) rather than
                \* increasing without bound: this keeps the state space finite WITHOUT
                \* a state constraint (a constraint is unsound under liveness checking,
                \* Specifying Systems 14.3.5) AND keeps Reconnect always enabled, so a
                \* down node can always recover (liveness needs "always eventually
                \* reconnect"; a monotonic cap would strand it once exhausted). MaxGen
                \* only needs to exceed the number of generations a straggler can span
                \* (channel depth + budgets), so a stale gen never collides with the
                \* current one within a behavior.
                streamerGen[n] := genCtr ||
                health[n] := IF health[n] = "down" THEN "starting" ELSE health[n];
                genCtr := (genCtr % MaxGen) + 1;
            end with;

        or
            \* -- Reap(n, v): CP garbage-collects a stale inventory entry for a VM
            \* the node no longer hosts. The reap-would-wipe-fleet review catch was
            \* precisely the decision NOT to destroy a VM on report-absence (a report
            \* can predate a fresh Prime): adopt_inventory is additive only and never
            \* drops on a status read (dispatcher.ex ~L989), so a genuinely gone VM
            \* self-corrects, it is never eagerly killed off a stale report. This
            \* models that safe GC: reap only fires when the VM is ALREADY gone from
            \* the node (destroyed / off-node), so it can never wipe a live VM.
            \* reapedLive witnesses the dangerous case; NoReapLive asserts the guard
            \* keeps it unreachable.
            with n \in Nodes, v \in VMs do
                await cpAlive /\ <<n, v>> \in inventory;
                \* The VM is gone from the node's ground truth (a node crash or a
                \* prior reap destroyed it) yet a stale inventory entry lingers.
                await vmNode[v] # n \/ vmState[v] = "destroyed";
                \* Witness: had the guard admitted a still-live VM, this would fire.
                if vmNode[v] = n /\ vmState[v] \in {"primed", "assigned"} then
                    reapedLive := TRUE;
                end if;
                inventory := { p \in inventory : p[2] # v };
            end with;

        or
            \* -- CrashCP: control-plane death wipes volatile state. ------------
            await cpAlive /\ cpCrashes < MaxCPCrashes;
            cpAlive := FALSE;
            cpCrashes := cpCrashes + 1;
            \* Volatile CP state is lost: inventory, in-flight miss meta, health,
            \* accepted reports, and the accepted generations all reset on boot.
            inventory := {};
            inflightMeta := {};

        or
            \* -- RestartCP: boot. Health resets to starting, gens are refreshed, -
            \* durable taskState survives, boot_sweep re-queues in-flight tasks. --
            await ~cpAlive;
            cpAlive := TRUE;
            health := [n \in Nodes |-> "starting"];
            streamerGen := [n \in Nodes |-> NULL];
            lastGen := [n \in Nodes |-> NULL];
            lastReport := [n \in Nodes |-> NULL];
            \* boot_sweep: an assigned task whose VM the fresh CP no longer knows (it
            \* lost inventory on crash) is re-queued and its claim dropped, so it can
            \* be re-dispatched. Its VM, still alive on the node, reverts to an idle
            \* "primed" warm VM: the fresh CP re-discovers it via adoption on the next
            \* accepted status (the node reports it primed again), which is exactly the
            \* pool-reclaim adoption exists for. All taskVM claims are dropped (any
            \* mid-miss worker died with the CP); its VM, now idle "primed", is
            \* likewise reclaimable by adoption.
            taskState := [ t \in Tasks |->
                IF taskState[t] = "assigned" THEN "queued" ELSE taskState[t] ];
            vmState := [ v \in VMs |->
                IF vmState[v] = "assigned" THEN "primed" ELSE vmState[v] ];
            taskVM := [ t \in Tasks |-> NULL ];

        or
            \* -- CrashNode(n): node host death. VMs vanish; wire messages remain. -
            with n \in Nodes do
                await nodeCrashes < MaxNodeCrashes;
                nodeCrashes := nodeCrashes + 1;
                \* Node-side VMs are destroyed. In-flight statusCh messages survive
                \* (the straggler). streamerGen is CP-side and untouched here: the CP
                \* only learns of the death by age-out (AgeToDown), which is where
                \* forget-before-kill runs. Assigned tasks on those VMs are orphaned
                \* until the down edge reassigns them.
                vmState := [ v \in VMs |-> IF vmNode[v] = n THEN "destroyed" ELSE vmState[v] ];
                vmNode := [ v \in VMs |-> IF vmNode[v] = n THEN NULL ELSE vmNode[v] ];
            end with;

        or
            \* -- Succeed(t): an assigned task completes and BEGINS destroy. -----
            \* Node-confirmed destruction (ADR embervm/014 decision 5): the task
            \* is done, so the CP appends the durable :*_destroying intent op and
            \* dispatches Destroy, but the VM stays LIVE on the node until it
            \* confirms teardown (ConfirmDestroy). The CP does NOT record the VM
            \* destroyed here: asserting destroyed before the node tore it down is
            \* exactly the drift NoDestroyBeforeConfirm forbids. Guarded so a VM is
            \* not double-entered into the intent set.
            with t \in Tasks do
                await cpAlive /\ taskState[t] = "assigned" /\ taskVM[t] # NULL;
                await taskVM[t] \notin destroying;
                taskState[t] := "succeeded";
                \* The VM moves to the node-side "destroying" state: still RESIDENT on
                \* the node (not torn down), but no longer assignable. Because every
                \* claim/adopt/revert guard keys off "primed"/"assigned"/"free", a
                \* "destroying" VM is automatically ineligible for AdoptInventory,
                \* DispatchWarm/Miss, and the assigned->primed reversion on the down
                \* edge / CP restart: it cannot be handed to a second task while its
                \* destroy is in flight. It stays owned by this doomed lifecycle until
                \* ConfirmDestroy (or a node crash) tears it down.
                vmState[taskVM[t]] := "destroying";
                destroying := destroying \cup {taskVM[t]};
            end with;

        or
            \* -- ConfirmDestroy(v): node completes teardown, THEN the CP records
            \* destroyed. This is the Destroy RPC returning teardown_confirmed=true:
            \* the node-side teardown (vmState -> "destroyed") and the CP's durable
            \* :*_destroyed record land together, with the teardown logically first.
            \* If a node crash already destroyed the VM (vmState "destroyed", off
            \* node), teardown is idempotent; either way cpDestroyed only ever gains a
            \* VM whose node ground truth is "destroyed" in this same step, so
            \* NoDestroyBeforeConfirm holds. The intent is cleared as it is fulfilled.
            with v \in destroying do
                await cpAlive;
                vmState[v] := "destroyed" ||
                vmNode[v] := NULL;
                destroying := destroying \ {v};
                cpDestroyed := cpDestroyed \cup {v};
            end with;
        end either;
    end while;
end algorithm; *)
\* BEGIN TRANSLATION (chksum(pcal) = "9e0ffc06" /\ chksum(tla) = "c5cbca3")
VARIABLES vmState, vmNode, vmPrincipal, statusCh, cpAlive, health, 
          streamerGen, lastGen, inventory, inflightMeta, lastReport, 
          taskState, taskVM, taskPrincipal, destroying, cpDestroyed, 
          reapedLive, cpCrashes, nodeCrashes, genCtr

(* define statement *)
InventoryVMs == { p[2] : p \in inventory }











ReservedVMs == { taskVM[t] : t \in { u \in Tasks : taskVM[u] # NULL } }


KnownVMs == (InventoryVMs \cup inflightMeta) \cup ReservedVMs


LiveTasks == { t \in Tasks : taskState[t] = "assigned" }



TypeOK ==
    /\ vmState \in [VMs -> {"free", "primed", "assigned", "destroying", "destroyed"}]
    /\ health \in [Nodes -> Health]
    /\ taskState \in [Tasks -> {"queued", "assigned", "succeeded"}]
    /\ cpCrashes <= MaxCPCrashes
    /\ nodeCrashes <= MaxNodeCrashes
    /\ \A n \in Nodes : Len(statusCh[n]) <= ChanDepth
    /\ destroying \subseteq VMs
    /\ cpDestroyed \subseteq VMs






NoDoubleAssign ==
    /\ \A t1, t2 \in LiveTasks :
         (t1 # t2) => (taskVM[t1] # taskVM[t2] \/ taskVM[t1] = NULL)
    /\ \A t \in LiveTasks :
         (taskVM[t] # NULL /\ vmState[taskVM[t]] \in {"primed", "assigned"})
           => vmState[taskVM[t]] = "assigned"



AdoptIdempotent == InventoryVMs \cap inflightMeta = {}



NoResurrection ==
    \A n \in Nodes : streamerGen[n] = NULL => health[n] # "healthy"


NoReapLive == reapedLive = FALSE





PrincipalIsolation ==
    \A t \in LiveTasks :
        taskVM[t] # NULL => vmPrincipal[taskVM[t]] = taskPrincipal[t]








NoDestroyBeforeConfirm ==
    \A v \in cpDestroyed : vmState[v] = "destroyed"




DestroyIntentPrecedesRecord ==
    \A v \in destroying : v \notin cpDestroyed





EventuallyDispatched ==
    \A t \in Tasks : (taskState[t] = "queued") ~> (taskState[t] \in {"assigned", "succeeded"})


vars == << vmState, vmNode, vmPrincipal, statusCh, cpAlive, health, 
           streamerGen, lastGen, inventory, inflightMeta, lastReport, 
           taskState, taskVM, taskPrincipal, destroying, cpDestroyed, 
           reapedLive, cpCrashes, nodeCrashes, genCtr >>

Init == (* Global variables *)
        /\ vmState = [v \in VMs |-> "free"]
        /\ vmNode = [v \in VMs |-> NULL]
        /\ vmPrincipal = [v \in VMs |-> NULL]
        /\ statusCh = [n \in Nodes |-> << >>]
        /\ cpAlive = TRUE
        /\ health = [n \in Nodes |-> "starting"]
        /\ streamerGen = [n \in Nodes |-> 0]
        /\ lastGen = [n \in Nodes |-> NULL]
        /\ inventory = {}
        /\ inflightMeta = {}
        /\ lastReport = [n \in Nodes |-> NULL]
        /\ taskState = [t \in Tasks |-> "queued"]
        /\ taskVM = [t \in Tasks |-> NULL]
        /\ taskPrincipal = [t \in Tasks |-> InitPrincipal[t]]
        /\ destroying = {}
        /\ cpDestroyed = {}
        /\ reapedLive = FALSE
        /\ cpCrashes = 0
        /\ nodeCrashes = 0
        /\ genCtr = 1

Next == /\ \/ /\ \E n \in Nodes:
                   \E v \in VMs:
                     \E p \in Principals:
                       /\ cpAlive /\ vmState[v] = "free"
                       /\ v \notin KnownVMs
                       /\ /\ vmNode' = [vmNode EXCEPT ![v] = n]
                          /\ vmPrincipal' = [vmPrincipal EXCEPT ![v] = p]
                          /\ vmState' = [vmState EXCEPT ![v] = "primed"]
                       /\ inventory' = (inventory \cup {<<n, v>>})
              /\ UNCHANGED <<statusCh, cpAlive, health, streamerGen, lastGen, inflightMeta, lastReport, taskState, taskVM, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E v \in VMs:
                   /\ vmState[v] = "destroyed" /\ v \notin KnownVMs
                   /\ /\ vmNode' = [vmNode EXCEPT ![v] = NULL]
                      /\ vmPrincipal' = [vmPrincipal EXCEPT ![v] = NULL]
                      /\ vmState' = [vmState EXCEPT ![v] = "free"]
                   /\ destroying' = destroying \ {v}
                   /\ cpDestroyed' = cpDestroyed \ {v}
              /\ UNCHANGED <<statusCh, cpAlive, health, streamerGen, lastGen, inventory, inflightMeta, lastReport, taskState, taskVM, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E n \in Nodes:
                   /\ streamerGen[n] # NULL
                   /\ IF Len(statusCh[n]) < ChanDepth
                         THEN /\ statusCh' = [statusCh EXCEPT ![n] = Append(statusCh[n], ([ gen |-> streamerGen[n],
                                                                                            primed |-> { v \in VMs : vmNode[v] = n /\ vmState[v] = "primed" },
                                                                                            assigned |-> { v \in VMs : vmNode[v] = n /\ vmState[v] = "assigned" } ]))]
                         ELSE /\ statusCh' = [statusCh EXCEPT ![n] = Append(Tail(statusCh[n]), ([ gen |-> streamerGen[n],
                                                                                                  primed |-> { v \in VMs : vmNode[v] = n /\ vmState[v] = "primed" },
                                                                                                  assigned |-> { v \in VMs : vmNode[v] = n /\ vmState[v] = "assigned" } ]))]
              /\ UNCHANGED <<vmState, vmNode, vmPrincipal, cpAlive, health, streamerGen, lastGen, inventory, inflightMeta, lastReport, taskState, taskVM, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E n \in Nodes:
                   /\ cpAlive /\ statusCh[n] # << >>
                   /\ LET msg == Head(statusCh[n]) IN
                        /\ statusCh' = [statusCh EXCEPT ![n] = Tail(statusCh[n])]
                        /\ IF \/ (streamerGen[n] # NULL /\ msg.gen = streamerGen[n])
                              \/ (~ForgetBeforeKill /\ streamerGen[n] = NULL /\ msg.gen = lastGen[n] /\ lastGen[n] # NULL)
                              THEN /\ health' = [health EXCEPT ![n] = "healthy"]
                                   /\ lastReport' = [lastReport EXCEPT ![n] = msg]
                                   /\ IF AdoptionEnabled
                                         THEN /\ inventory' = (         inventory \cup
                                                               { <<n, v>> : v \in { w \in msg.primed :
                                                                   w \notin KnownVMs /\ vmState[w] # "assigned" } })
                                         ELSE /\ TRUE
                                              /\ UNCHANGED inventory
                              ELSE /\ TRUE
                                   /\ UNCHANGED << health, inventory, 
                                                   lastReport >>
              /\ UNCHANGED <<vmState, vmNode, vmPrincipal, cpAlive, streamerGen, lastGen, inflightMeta, taskState, taskVM, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E t \in Tasks:
                   \E pair \in inventory:
                     /\ cpAlive /\ taskState[t] = "queued"
                     /\ health[pair[1]] = "healthy"
                     /\ vmPrincipal[pair[2]] = taskPrincipal[t]
                     /\ inventory' = inventory \ {pair}
                     /\ taskVM' = [taskVM EXCEPT ![t] = pair[2]]
                     /\ taskState' = [taskState EXCEPT ![t] = "assigned"]
                     /\ vmState' = [vmState EXCEPT ![pair[2]] = "assigned"]
              /\ UNCHANGED <<vmNode, vmPrincipal, statusCh, cpAlive, health, streamerGen, lastGen, inflightMeta, lastReport, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E t \in Tasks:
                   \E v \in VMs:
                     \E n \in Nodes:
                       /\ cpAlive /\ taskState[t] = "queued"
                       /\ health[n] = "healthy" /\ vmState[v] = "free"
                       /\ v \notin KnownVMs
                       /\ /\ vmNode' = [vmNode EXCEPT ![v] = n]
                          /\ vmPrincipal' = [vmPrincipal EXCEPT ![v] = taskPrincipal[t]]
                          /\ vmState' = [vmState EXCEPT ![v] = "primed"]
                       /\ inflightMeta' = (inflightMeta \cup {v})
                       /\ taskVM' = [taskVM EXCEPT ![t] = v]
              /\ UNCHANGED <<statusCh, cpAlive, health, streamerGen, lastGen, inventory, lastReport, taskState, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E t \in Tasks:
                   /\ cpAlive /\ taskState[t] = "queued" /\ taskVM[t] # NULL
                   /\ vmState[taskVM[t]] = "primed"
                   /\ vmNode[taskVM[t]] # NULL
                   /\ IF vmNode[taskVM[t]] = NULL THEN FALSE
                      ELSE health[vmNode[taskVM[t]]] = "healthy"
                   /\ taskState' = [taskState EXCEPT ![t] = "assigned"]
                   /\ vmState' = [vmState EXCEPT ![taskVM[t]] = "assigned"]
                   /\ inflightMeta' = inflightMeta \ {taskVM[t]}
              /\ UNCHANGED <<vmNode, vmPrincipal, statusCh, cpAlive, health, streamerGen, lastGen, inventory, lastReport, taskVM, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E t \in Tasks:
                   /\ cpAlive /\ taskState[t] = "queued" /\ taskVM[t] # NULL
                   /\ \/ vmState[taskVM[t]] # "primed"
                      \/ vmNode[taskVM[t]] = NULL
                      \/ (vmNode[taskVM[t]] # NULL /\ health[vmNode[taskVM[t]]] = "down")
                   /\ inflightMeta' = inflightMeta \ {taskVM[t]}
                   /\ IF vmState[taskVM[t]] = "primed"
                         THEN /\ /\ vmNode' = [vmNode EXCEPT ![taskVM[t]] = NULL]
                                 /\ vmState' = [vmState EXCEPT ![taskVM[t]] = "destroyed"]
                         ELSE /\ TRUE
                              /\ UNCHANGED << vmState, vmNode >>
                   /\ taskVM' = [taskVM EXCEPT ![t] = NULL]
              /\ UNCHANGED <<vmPrincipal, statusCh, cpAlive, health, streamerGen, lastGen, inventory, lastReport, taskState, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E n \in Nodes:
                   /\ AgingEnabled /\ cpAlive /\ health[n] \in {"healthy", "starting"}
                   /\ health' = [health EXCEPT ![n] = "unknown"]
              /\ UNCHANGED <<vmState, vmNode, vmPrincipal, statusCh, cpAlive, streamerGen, lastGen, inventory, inflightMeta, lastReport, taskState, taskVM, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E n \in Nodes:
                   /\ AgingEnabled /\ cpAlive /\ health[n] \in {"unknown", "starting", "healthy"}
                   /\ health' = [health EXCEPT ![n] = "down"]
                   /\ taskState' =          [ t \in Tasks |->
                                   IF taskVM[t] # NULL /\ vmNode[taskVM[t]] = n /\ taskState[t] = "assigned"
                                   THEN "queued" ELSE taskState[t] ]
                   /\ taskVM' =       [ t \in Tasks |->
                                IF taskVM[t] # NULL /\ vmNode[taskVM[t]] = n /\ taskState'[t] = "assigned"
                                THEN NULL ELSE taskVM[t] ]
                   /\ vmState' =        [ v \in VMs |->
                                 IF vmNode[v] = n /\ vmState[v] = "assigned" THEN "primed" ELSE vmState[v] ]
                   /\ inventory' = { p \in inventory : p[1] # n }
                   /\ inflightMeta' = { v \in inflightMeta : vmNode[v] # n }
                   /\ lastReport' = [lastReport EXCEPT ![n] = NULL]
                   /\ /\ lastGen' = [lastGen EXCEPT ![n] = streamerGen[n]]
                      /\ streamerGen' = [streamerGen EXCEPT ![n] = NULL]
              /\ UNCHANGED <<vmNode, vmPrincipal, statusCh, cpAlive, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E n \in Nodes:
                   /\ cpAlive /\ streamerGen[n] = NULL
                   /\ /\ health' = [health EXCEPT ![n] = IF health[n] = "down" THEN "starting" ELSE health[n]]
                      /\ streamerGen' = [streamerGen EXCEPT ![n] = genCtr]
                   /\ genCtr' = (genCtr % MaxGen) + 1
              /\ UNCHANGED <<vmState, vmNode, vmPrincipal, statusCh, cpAlive, lastGen, inventory, inflightMeta, lastReport, taskState, taskVM, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes>>
           \/ /\ \E n \in Nodes:
                   \E v \in VMs:
                     /\ cpAlive /\ <<n, v>> \in inventory
                     /\ vmNode[v] # n \/ vmState[v] = "destroyed"
                     /\ IF vmNode[v] = n /\ vmState[v] \in {"primed", "assigned"}
                           THEN /\ reapedLive' = TRUE
                           ELSE /\ TRUE
                                /\ UNCHANGED reapedLive
                     /\ inventory' = { p \in inventory : p[2] # v }
              /\ UNCHANGED <<vmState, vmNode, vmPrincipal, statusCh, cpAlive, health, streamerGen, lastGen, inflightMeta, lastReport, taskState, taskVM, destroying, cpDestroyed, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ cpAlive /\ cpCrashes < MaxCPCrashes
              /\ cpAlive' = FALSE
              /\ cpCrashes' = cpCrashes + 1
              /\ inventory' = {}
              /\ inflightMeta' = {}
              /\ UNCHANGED <<vmState, vmNode, vmPrincipal, statusCh, health, streamerGen, lastGen, lastReport, taskState, taskVM, destroying, cpDestroyed, reapedLive, nodeCrashes, genCtr>>
           \/ /\ ~cpAlive
              /\ cpAlive' = TRUE
              /\ health' = [n \in Nodes |-> "starting"]
              /\ streamerGen' = [n \in Nodes |-> NULL]
              /\ lastGen' = [n \in Nodes |-> NULL]
              /\ lastReport' = [n \in Nodes |-> NULL]
              /\ taskState' =          [ t \in Tasks |->
                              IF taskState[t] = "assigned" THEN "queued" ELSE taskState[t] ]
              /\ vmState' =        [ v \in VMs |->
                            IF vmState[v] = "assigned" THEN "primed" ELSE vmState[v] ]
              /\ taskVM' = [ t \in Tasks |-> NULL ]
              /\ UNCHANGED <<vmNode, vmPrincipal, statusCh, inventory, inflightMeta, destroying, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E n \in Nodes:
                   /\ nodeCrashes < MaxNodeCrashes
                   /\ nodeCrashes' = nodeCrashes + 1
                   /\ vmState' = [ v \in VMs |-> IF vmNode[v] = n THEN "destroyed" ELSE vmState[v] ]
                   /\ vmNode' = [ v \in VMs |-> IF vmNode[v] = n THEN NULL ELSE vmNode[v] ]
              /\ UNCHANGED <<vmPrincipal, statusCh, cpAlive, health, streamerGen, lastGen, inventory, inflightMeta, lastReport, taskState, taskVM, destroying, cpDestroyed, reapedLive, cpCrashes, genCtr>>
           \/ /\ \E t \in Tasks:
                   /\ cpAlive /\ taskState[t] = "assigned" /\ taskVM[t] # NULL
                   /\ taskVM[t] \notin destroying
                   /\ taskState' = [taskState EXCEPT ![t] = "succeeded"]
                   /\ vmState' = [vmState EXCEPT ![taskVM[t]] = "destroying"]
                   /\ destroying' = (destroying \cup {taskVM[t]})
              /\ UNCHANGED <<vmNode, vmPrincipal, statusCh, cpAlive, health, streamerGen, lastGen, inventory, inflightMeta, lastReport, taskVM, cpDestroyed, reapedLive, cpCrashes, nodeCrashes, genCtr>>
           \/ /\ \E v \in destroying:
                   /\ cpAlive
                   /\ /\ vmNode' = [vmNode EXCEPT ![v] = NULL]
                      /\ vmState' = [vmState EXCEPT ![v] = "destroyed"]
                   /\ destroying' = destroying \ {v}
                   /\ cpDestroyed' = (cpDestroyed \cup {v})
              /\ UNCHANGED <<vmPrincipal, statusCh, cpAlive, health, streamerGen, lastGen, inventory, inflightMeta, lastReport, taskState, taskVM, reapedLive, cpCrashes, nodeCrashes, genCtr>>
        /\ UNCHANGED taskPrincipal

Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(Next)

\* END TRANSLATION

(*****************************************************************************)
(* Fairness for the liveness property. WF_vars(Next) alone (what the fair    *)
(* algorithm emits) only forces SOME enabled branch to keep firing, which a   *)
(* scheduler can satisfy by cycling status traffic on one node forever while  *)
(* never dispatching a queued task or bringing a node healthy. The liveness   *)
(* claim EventuallyDispatched needs per-action fairness on exactly the steps   *)
(* that make dispatch progress. These named action predicates re-expose the   *)
(* progress branches of Next so strong fairness can be attached to each; they  *)
(* are guards only (their effect is whatever Next does when that branch runs). *)
(*                                                                           *)
(* Strong (not weak) fairness because each is only INTERMITTENTLY enabled:    *)
(* e.g. RecvStatus is disabled whenever the channel is momentarily empty, so  *)
(* WF would let it be skipped forever across those gaps; SF forces it to fire  *)
(* since it is enabled infinitely often. The adversarial actions (CrashCP,    *)
(* CrashNode, Reap, AgeToUnknown/Down) are deliberately given NO fairness: the *)
(* checker is free never to crash, and their crash budgets bound how often    *)
(* they can, so liveness must hold in spite of, not because of, them.         *)
(*****************************************************************************)

\* Each predicate below identifies a step in which a specific PROGRESS BRANCH
\* actually fired, by a before/after fingerprint on the state, so strong fairness
\* pins to the branch itself, not merely to any Next step taken while the branch
\* was enabled (that weaker form is vacuously satisfiable by unrelated steps and
\* does NOT force progress). The node/task-scoped ones are PARAMETERIZED so
\* fairness can be quantified per node/per task: a single \E-form would let one
\* busy node's traffic discharge the whole obligation while another node starves
\* (that exact livelock: all VMs primed on a node stuck in "starting" while a
\* different, empty node stays healthy, so dispatch never fires).

\* Node n emitted a fresh status onto its channel (SendStatus(n)).
DidSend(n)      == Len(statusCh'[n]) > Len(statusCh[n])
\* Node n's status was accepted, driving it healthy (RecvStatus(n) accept path).
DidRecv(n)      == health[n] # "healthy" /\ health'[n] = "healthy"
\* Node n reconnected under a fresh generation (Reconnect(n)).
DidReconnect(n) == streamerGen[n] = NULL /\ streamerGen'[n] # NULL
\* Task t was dispatched: moved queued -> assigned (warm or miss part 2).
DidDispatch(t)  == taskState[t] = "queued" /\ taskState'[t] = "assigned"
\* A miss VM was just claimed for some task (DispatchMiss part 1): inflightMeta
\* grew. Task-agnostic (the claim precedes the assign, which DidDispatch covers).
DidClaimMiss    == inflightMeta \subseteq inflightMeta' /\ inflightMeta' # inflightMeta
\* The crashed control plane booted (RestartCP).
DidRestart      == cpAlive' /\ ~cpAlive
\* A destroyed VM slot returned to the free pool (RecycleId), so a fresh prime is
\* always eventually possible for a queued task waiting on capacity.
DidRecycle      == \E v \in VMs : vmState[v] = "destroyed" /\ vmState'[v] = "free"
\* A queued task dropped a dead claimed miss VM (AbandonClaim), freeing it to
\* re-dispatch: taskVM went non-NULL -> NULL while the task stayed queued.
DidAbandon      == \E t \in Tasks : taskState[t] = "queued" /\ taskVM[t] # NULL /\ taskVM'[t] = NULL

\* FairSpec conjoins strong fairness on each progress branch to the safety spec.
\* Strong (not weak) because each branch is only intermittently enabled (empty
\* channel, momentarily no warm VM, node not yet healthy), so WF could skip it
\* across the gaps; SF forces it whenever it is enabled infinitely often. Node-
\* and task-scoped fairness is quantified with \A so EVERY node/task makes
\* progress, not just one. The adversarial actions (CrashCP, CrashNode, Reap,
\* AgeToUnknown/Down) get NO fairness: the checker may never crash, and the crash
\* budgets bound how often it can, so liveness must hold in spite of them.
FairSpec ==
    /\ Spec
    /\ \A n \in Nodes : SF_vars(Next /\ DidSend(n))
    /\ \A n \in Nodes : SF_vars(Next /\ DidRecv(n))
    /\ \A n \in Nodes : SF_vars(Next /\ DidReconnect(n))
    /\ \A t \in Tasks : SF_vars(Next /\ DidDispatch(t))
    /\ SF_vars(Next /\ DidClaimMiss)
    /\ SF_vars(Next /\ DidRestart)
    /\ SF_vars(Next /\ DidRecycle)
    /\ SF_vars(Next /\ DidAbandon)
===============================================================================
