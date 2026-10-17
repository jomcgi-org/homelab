------------------------------ MODULE dual_wake ------------------------------
(***************************************************************************
Dual wake, issue #4702. Source: main bf9f3a1338d3b5f809c04e40df491adb2bfaa7ef.

Two bricks and the CP can boot independently. There is no cross-waker lock.
The unit of winning is one local single-flight: followers retain its result,
even after complete deletes the map entry. A duplicate across flights is legal
and must be accounted for. AtMostOneVMPerWorkload is an intentionally refuted
current-main reachability check, not a mutation. This model cannot authorize
retiring either wake path (#4013) or establish implementation conformance.

PROSE MAP (paths relative to projects/embervm).
Every action below has a serving implementation correspondence:
  Init: serving_manager.ex init/rebuild and endpoint_publisher.ex initial render.
  Advertise: node_registry.ex NodeStatus activator_endpoint; advertisements are
    monotone in this episode. Both nodes hold the serving image. Preferred is
    the first configured_id among advertisers, matching node_advertised_activator
    in control/lib/embervm/endpoint_publisher.ex:693-748 (image holders first,
    then READY bases, then any advertiser). Route is the LAST rendered fallback.
  CheckMiss / CheckStraggler: noded/server/activator.go:113-154 ServeHTTP and
    serving_registry.go:299 withHealthyFreshSplice. CheckMiss and Join are separate:
    the documented pre-check/complete/delete/join duplicate window is reachable.
    Live priority-0 and fallback priority-1 routing is ChooseRoute. A request
    that reaches the CP is never silently redirected to another brick.
  CPMiss / CPStraggler: control/lib/embervm/serving_manager.ex:269-305 handle_miss,
    first_published_endpoint and handle_cold_miss. The CP endpoint check and
    flight admission share one serialized callback, unlike the brick pre-check.
  UnavailableCP: connection failure to an absent CP fallback, resolved by Timeout.
  Join: activator.go join (per-node flights); CPMiss uses serving_manager.ex waking.
    Flight id = leader request id, VM id = flight id, so identities never recycle.
    Node choice for CP placement is nondeterministic; brick placement is local.
  Ready: noded/server/serving.go:190 waitServingReady then EnsureDNAT.
  Register: finishServingStart adds servingVMs AFTER Ready, starts the probe,
    signals NodeStatus and returns StartServing. INSTANCE_ORIGIN_ACTIVATOR is
    passed by activator.go boot; StartServing uses INSTANCE_ORIGIN_CONTROL_PLANE.
    INSTANCE_ORIGIN_UNSPECIFIED is treated as CP by activator_origin?; the model
    merges those two non-activator values. No InstanceOrigin surface exists in
    vocabulary.exs, so this header records the classification without adding one.
  Complete / CompleteFailure: activator.go complete closes done then deletes the
    local flight; parked followers still own the old pointer. CP finish_wake,
    finish_created, publish_and_resolve write serving_started/serving_published
    before resolving waiters; errors return 503 and keep the fallback.
    Restriction: the model guards CP Complete with v in s.reported, but
    finish_wake/finish_created (serving_manager.ex:709-770) never check node
    inventory, so a CP VM withdrawn after StartServing returns but before
    wake_done is processed still gets a published row in the code while the
    model takes CompleteFailure. Documented, not closed; no verdict change
    at these bounds.
  Forward / DialResult / Timeout: activator.go:340 proxyBuffered, http.Client.Do
    and HTTP 502/503; tcp_activator.ex and activator_splices.ex have analogous
    bounded dial failures. CP request proxy uses the endpoint returned to its
    parked waiter. Deliver means a request accepted by a ready live guest at
    that instant, not an attempted TCP dial. A VM may crash after delivery.
    A recent healthy probe is not a promise of continued life. DialResult checks
    actual guest state to model transport success versus explicit network error,
    not an invented application readiness check before every send.
  BootFailure: waitServingReady/EnsureDNAT error reaps in serving.go:190-207.
  CrashVM / ExpireProbe / Probe: guest exit and serving/probe.go; healthy reports
    and fresh probe evidence can lag actual death. No restart of a VM identity.
  WithdrawStale: activator.go withdraw and serving_registry.go withdrawIfIdle;
    teardown waits for splice reservations. StopServing DESTROY is the same
    exact-instance teardown abstraction. BANK/snapshot GC are excluded here.
  LeaveCP / ReturnCP: ServingManager process availability and rebuild. Absence
    suspends callbacks; crash loses waking and unbackfilled ETS rows, preserves
    durable ServingStore rows, and can leave an RPC's node effect running.
    serving_store.ex rebuild marks durable published rows healthy initially.
    application.ex:297-339,511 uses rest_for_one with ServingStore BEFORE the
    manager: losing the store also aborts the manager's in-progress reconcile.
    Envoy retains its last rendered routes while the CP is absent.
  BeginReconcile: serving_manager.ex:990 do_reconcile captures NodeCapacity facts.
  AdoptExisting: adopt_one, including the waking exemption, maps row health from
    the captured status. Terminal and banking rows are outside this wake episode.
  AdoptActivator: :1099 adopt_activator_vms and serving_store.ex:559 insert ETS
    :published row (instance_id = vm_id) before backfill_created. Unhealthy facts
    create unhealthy rows. It also adopts a VM reported AFTER CP return mid-boot.
  Backfill: serving_store.ex:608 appends serving_started then serving_published;
    failure leaves the ETS row. Success/failure is nondeterministic. Rebuild
    from a partial durable append still preserves the claiming row, represented
    by durable membership. There is no re-drive in the code: after a failed
    backfill the ETS row exists, so later passes skip it at the
    find_instance_by_vm :none check (adopt_activator_vms fires only on a
    miss), and adopt_one/adopt_live never append. The row stays ETS-only
    until a crash, then is re-adopted. Backfill(d) still permits d = {},
    so the safety results stand, but the mapping does not claim a re-drive.
  ReconcileStep: evict_orphan_snapshots, withdraw_unreported_resident_health,
    redrive_destroying, in that order; abstract no-ops because there are no
    snapshots, pre-existing destroying rows, or missing node facts in this model.
  Orphans: :1314 destroy_orphan_serving_vms, gated ON as in deploy/values.yaml:43,
    skips activator origin, then waking, then any claiming row. StopServing
    DESTROY confirmation is atomic here; RPC failure/redrive belongs to
    adoption.tla/stateful.tla. NodeStatus remains reported until teardown.
  Publish: EndpointPublisher.publish at the END of do_reconcile. CP callbacks
    are serialized with this sequence; node effects and CP crash can interleave
    between stages. Endpoint/route snapshots survive CP absence.

STATE MAP. s is the full finite product, vars is its stuttering/fairness tuple.
  cp, leaves: CP availability and bounded absence/crash count.
  active: each waker's volatile workload flight map (0 = no flight).
  flight: retained per-flight waker and completion result, as above.
  vm: StartServing's exact handle, node placement and boot/ready/dead state.
  reported, healthy, fresh: servingVMs/NodeStatus inventory, observed health,
    probe freshness. Reported inventory can retain a crashed guest.
  rows, rowHealthy, durable: ServingStore ETS ownership/health and op-log rebuild.
  req: parked callers, saved flight pointer, chosen endpoint and response state.
  route, ads, published: Envoy fallback, NodeRegistry activator advertisements
    and the last rendered priority-0 endpoints.
  pc, facts, factsHealthy: do_reconcile program counter and its captured inventory.
  winners: successful StartServing result identities, including the ready result
    before its callback drains waiters. Every flight reserves exactly one id.
  everReady, targeted, unownedUse, badDelivery, reaped: HISTORY for the invariants;
    target evidence never disappears when rows/probes/VMs later change.
  failures: finite VM-crash budget. Boot failure consumes its flight, not a crash.
  returnedBoots, witnesses: HISTORY for return-mid-boot and non-vacuity checks.
    Observe selects one observation per Witness cfg; "none" erases only these
    auxiliary histories in safety/liveness runs. It never restricts a transition.
  stranded: unaccounted handles at a settled, complete-inventory reconcile cut.

PREDICATES. Nodes/Wakers/Requests are the implementation populations above;
  Live/Eligible/Busy describe registry/probe/flight state. Preferred
  describes the deterministic fallback. Settled means no pending RPC, all
  issued requests terminal, and the captured inventory is still complete.
  TypeOK checks the product domains. OneWinnerPerFlight checks each parked
  caller's endpoint against its retained local flight, including followers.
  NoUntrackedServing checks use-time ownership: any target outside the reserved
  successful flight results needs a CP row. PublishAfterReady checks explicit
  history, including failed splice attempts. NoLegitActivatorWakeReaped records
  only orphan-destroy decisions, not legitimate explicit/idle teardown.
  NoDeadSplice checks delivery-time life/readiness and rejects silent failure.
  NoLeak is the bounded settlement safety form: at EVERY complete-inventory
  quiescent reconcile cut each allocated live handle has a row or was destroyed.
  EventualAccounting supplies the stronger eventual claim in the liveness cfg:
  once allocated, each handle eventually has a row or has been destroyed. It
  requires finite outages/crashes, fair RPC completion, return and reconciliation.
  Serialized CP callbacks need strong fairness because idle is intermittent;
  all other progress obligations use weak fairness.
  RequestsResolve requires every parked request eventually deliver or fail.
  Fairness caveat: FairSpec gives weak fairness to Timeout but none to Join,
  Forward, DialResult or UnavailableCP, and Timeout stays enabled in every
  non-terminal request state, so RequestsResolve is discharged by the
  route/connect timeout alone. It is not evidence that delivery progresses:
  a regression that stops forwarding keeps the liveness cfg green.
  NeverOfflineDelivery/NeverReturnAdoption/NeverDuplicateResolved/NeverLoudFailure
  are deliberately false witnesses, not safety requirements.

CLASS CORRESPONDENCE (one row for every action; filenames in server/ are under
  noded/, managers are under control/lib/embervm/):
  Serving action          Stateful counterpart              Group counterpart
  Init/Advertise          stateful_manager init, registry    group_wake_manager init
  ChooseRoute             endpoint_publisher stateful L4    group render (CP fallback)
  CheckMiss/CheckStraggler server/stateful_activator.go       server/group_activator.go
  CPMiss/CPStraggler       stateful_manager handle_wake      group manager handle_wake
  UnavailableCP           CP fallback dial failure          CP fallback dial failure
  Join                    stateful activator flights        group activator flights
  Ready/Register          server/stateful.go finish start    server/group_member.go finish
  Complete                stateful_manager finish_wake      group_wake_manager finish_wake
  CompleteFailure         stateful_manager wake failure     group wake failure
  Forward/DialResult      server/stateful_activator.go L4    server/group_activator.go L4
  Timeout                 stateful wake_timeout, dial bound group wake_timeout, dial bound
  BootFailure             StartStateful failure teardown    member-start failure cleanup
  CrashVM/ExpireProbe/Probe stateful registry/probes         member registry/probes
  WithdrawStale           stateful stale endpoint handling  group stale set handling
  LeaveCP/ReturnCP         stateful manager rebuild          group manager rebuild
  BeginReconcile          stateful_manager do_reconcile     group do_reconcile
  AdoptExisting           stateful adopt_one                group adopt_one
  AdoptActivator          adopt_activator_stateful_vms:3513  DIFFERENT: no equivalent
  Backfill                StatefulStore.backfill_created    DIFFERENT: set/member rows
  ReconcileStep           stateful reconcile helpers        group reconcile helpers
  Orphans                 destroy_orphan_stateful_vms:3458  destroy_orphan_group_vms:914
  Publish                 EndpointPublisher.publish         group publish
These are correspondence sites, not claims that classes share every guard.
Only identical readiness, independent flight, explicit dial failure and origin
skip abstractions carry across. Stateful anchor placement, writable-volume
singleton, stale wake tokens/exact-instance cleanup, checkpoint and generation
issuance/self-bump are EXCLUDED. Group set/member aggregate readiness, adoption
and teardown are EXCLUDED: one VM is insufficient to refine a group. Fork B is
excluded. The L4 fallback's anchor-node choice differs from serving's first
eligible advertiser. No runtime conformance trace or live fault injection.

ASSUMPTIONS/BOUNDS. One nodeLocalWake-enabled serving workload; eligible,
non-silenced nodes with the same image, stable node identities and reliable
eventual status. Two nodes expose cross-brick races; two requests/handles expose
two wakers or two sequential leaders and follower reuse. One CP outage (either
absence or volatile-state-losing crash), one post-ready VM crash, and no handle
reuse suffice for those orderings, not an unbounded proof. Safety splits into
two requests/one CP outage/no VM crash; two requests/no CP outage/one VM crash;
and one request/one CP outage/one VM crash. The three-way combination of two
competing flights plus both kinds of fault is outside these checked bounds.
The one-request liveness cfg includes both fault budgets. Caps (16 boots,
64 parked, 30/min) are abstracted above the two-request bound. Explicit timeout
is included; admission overflow adds only the same loud failure. Readiness,
registration, flight completion, forwarding and delivery are separate actions.
No symmetry: node order affects Preferred. No state constraints truncate TLC.

MUTATIONS. Single-mutation negatives flip exactly one boolean; the composite
unguarded_orphans cfg deliberately disables both redundant guards below.
WrongWinner binds a
parked caller to another successful node result. UnownedPublish bypasses ownership.
EarlyRegister removes waitServingReady ordering. SilentFailure swallows a
transport failure. Leak skips activator adoption while its orphan exemption
remains. ReapActivator makes the destroyer reap activator facts even if owned.
The latter is deliberately stronger than removing ONLY the origin skip.

The origin_skip_disabled and orphans_first cfgs are PASS sensitivity checks.
Current main has redundant defense in depth: the orphan destroyer skips
activator origin, and adopt_activator_vms runs earlier over the same captured
facts. ServingStore.adopt_activator inserts the ETS row before backfill;
backfill failure keeps that row. Removing either guard alone is safe within
this model. The unguarded_orphans composite removes the origin skip AND runs
orphans before adoption, exposing NoLegitActivatorWakeReaped. ReapActivator
remains the stronger destroyer mutation that ignores ownership too. This is
a correspondence finding, not an acceptance gap. It does not make either
implementation guard safe to remove in production.
***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS MaxRequests, MaxLeaves, MaxFailures, Witness,
          WrongWinner, UnownedPublish, ReapActivator, EarlyRegister,
          SilentFailure, Leak, SkipOriginGuard, OrphansFirst

Nodes == {"n1", "n2"}
Wakers == {"CP", "n1", "n2"}
Requests == 1..MaxRequests
VARIABLE s
vars == <<s>>
Live == {v \in Requests : s.vm[v].phase \in {"boot", "ready", "dead"}}
Eligible(w) == {v \in s.reported \cap s.fresh \cap s.healthy :
                 w = "CP" \/ s.vm[v].node = w}
Busy == {v \in Requests : s.flight[v].state = "running"}
Preferred == IF "n1" \in s.ads THEN "n1" ELSE IF "n2" \in s.ads THEN "n2" ELSE "CP"
Observe(events) == s.witnesses \cup (events \cap {Witness})
Settled == /\ Busy = {}
           /\ \A v \in Requests : s.vm[v].phase # "boot"
           /\ \A r \in Requests : s.req[r].state \in {"new", "delivered", "failed", "silent"}
           /\ s.facts = s.reported
           /\ Live \subseteq s.reported
Init == s = [cp |-> "up", leaves |-> 0, failures |-> 0,
             active |-> [w \in Wakers |-> 0],
             flight |-> [r \in Requests |-> [waker |-> "none", state |-> "free"]],
             vm |-> [v \in Requests |-> [node |-> "none", origin |-> "none", phase |-> "empty"]],
             req |-> [r \in Requests |-> [state |-> "new", waker |-> "none", flight |-> 0, target |-> 0]],
             rows |-> {}, rowHealthy |-> {}, durable |-> {},
             reported |-> {}, healthy |-> {}, fresh |-> {}, winners |-> {},
             everReady |-> {}, targeted |-> {}, unownedUse |-> {},
             badDelivery |-> {}, reaped |-> {}, stranded |-> {},
             ads |-> {}, route |-> "CP", published |-> {}, pc |-> "idle", facts |-> {}, factsHealthy |-> {},
             returnedBoots |-> {}, witnesses |-> {}]

Advertise(n) == /\ n \notin s.ads
                /\ s' = [s EXCEPT !.ads = @ \cup {n}]

\* A healthy priority-0 endpoint may also fail between route and dial.
ChooseRoute(r, v) ==
    /\ s.req[r].state = "new" /\ v \in s.published
    /\ s' = [s EXCEPT !.req[r] = [state |-> "dial", waker |-> "none", flight |-> 0, target |-> v],
                     !.targeted = @ \cup {v},
                     !.unownedUse = @ \cup (IF v \in s.rows \cup s.winners THEN {} ELSE {v})]

CheckMiss(r) ==
    /\ s.req[r].state = "new"
    /\ s.route \in Nodes /\ Eligible(s.route) = {}
    /\ s' = [s EXCEPT !.req[r] = [state |-> "checked", waker |-> s.route, flight |-> 0, target |-> 0]]

CPMiss(r, n) ==
    /\ s.req[r].state = "new" /\ s.route = "CP" /\ s.cp = "up" /\ s.pc = "idle"
    /\ s.rows \cap s.rowHealthy = {} /\ n \in Nodes
    /\ IF s.active["CP"] # 0
       THEN s' = [s EXCEPT !.req[r] = [state |-> "parked", waker |-> "CP", flight |-> s.active["CP"], target |-> 0]]
       ELSE /\ s.flight[r].state = "free"
            /\ s' = [s EXCEPT !.active["CP"] = r,
                      !.flight[r] = [waker |-> "CP", state |-> "running"],
                      !.vm[r] = [node |-> n, origin |-> "CP", phase |-> "boot"],
                      !.req[r] = [state |-> "parked", waker |-> "CP", flight |-> r, target |-> 0]]

CPStraggler(r, v) ==
    /\ s.req[r].state = "new" /\ s.route = "CP" /\ s.cp = "up" /\ s.pc = "idle"
    /\ v \in s.rows \cap s.rowHealthy
    /\ s' = [s EXCEPT !.req[r] = [state |-> "dial", waker |-> "CP", flight |-> 0, target |-> v],
                      !.targeted = @ \cup {v}]

UnavailableCP(r) ==
    /\ s.req[r].state = "new" /\ s.route = "CP" /\ s.cp # "up"
    /\ s' = [s EXCEPT !.req[r] = [state |-> "checked", waker |-> "CP", flight |-> 0, target |-> 0]]

CheckStraggler(r, v) ==
    /\ s.req[r].state = "new" /\ s.route \in Nodes /\ v \in Eligible(s.route)
    /\ s' = [s EXCEPT !.req[r] = [state |-> "dial", waker |-> s.route, flight |-> 0, target |-> v],
                     !.targeted = @ \cup {v},
                     !.unownedUse = @ \cup (IF v \in s.rows \cup s.winners THEN {} ELSE {v})]

Join(r, n) ==
    LET w == s.req[r].waker IN
    /\ s.req[r].state = "checked" /\ w \in Nodes
    /\ n = w
    /\ IF s.active[w] # 0
       THEN s' = [s EXCEPT !.req[r].state = "parked", !.req[r].flight = s.active[w]]
       ELSE /\ s.flight[r].state = "free"
            /\ s' = [s EXCEPT !.active[w] = r,
                      !.flight[r] = [waker |-> w, state |-> "running"],
                      !.vm[r] = [node |-> n, origin |-> "ACTIVATOR", phase |-> "boot"],
                      !.req[r].state = "parked", !.req[r].flight = r]

Ready(v) == /\ s.vm[v].phase = "boot"
            /\ s' = [s EXCEPT !.vm[v].phase = "ready", !.everReady = @ \cup {v}]

Register(v) == /\ v \notin s.reported
               /\ s.vm[v].phase \in {"ready", "dead"} \/ (EarlyRegister /\ s.vm[v].phase = "boot")
               /\ s' = [s EXCEPT !.reported = @ \cup {v}, !.fresh = @ \cup {v},
                                  !.healthy = @ \cup {v}, !.winners = @ \cup {v}]

Complete(v) ==
    LET w == s.flight[v].waker IN
    /\ s.flight[v].state = "running" /\ v \in s.reported
    /\ w # "CP" \/ (s.cp = "up" /\ s.pc = "idle")
    /\ s' = [s EXCEPT !.flight[v].state = "complete", !.active[w] = 0,
              !.rows = @ \cup (IF w = "CP" THEN {v} ELSE {}),
              !.durable = @ \cup (IF w = "CP" THEN {v} ELSE {}),
              !.rowHealthy = @ \cup (IF w = "CP" THEN {v} ELSE {})]

BootFailure(v) == /\ s.vm[v].phase \in {"boot", "dead"} /\ v \notin s.reported
                 /\ s' = [s EXCEPT !.vm[v].phase = "destroyed"]

CompleteFailure(v) ==
    LET w == s.flight[v].waker IN
    /\ s.flight[v].state = "running" /\ s.vm[v].phase = "destroyed"
    /\ w # "CP" \/ (s.cp = "up" /\ s.pc = "idle")
    /\ s' = [s EXCEPT !.flight[v].state = "failed", !.active[w] = 0]

Forward(r, v) ==
    /\ s.req[r].state = "parked"
    /\ s.flight[s.req[r].flight].state = "complete"
    /\ v = s.req[r].flight \/ (WrongWinner /\ v \in s.winners)
    /\ s' = [s EXCEPT !.req[r].state = "dial", !.req[r].target = v,
              !.targeted = @ \cup {v},
              !.unownedUse = @ \cup (IF v \in s.rows \/ v = s.req[r].flight THEN {} ELSE {v})]

DialResult(r) ==
    LET v == s.req[r].target
        ok == s.vm[v].phase = "ready"
    IN /\ s.req[r].state = "dial"
       /\ s' = [s EXCEPT !.req[r].state = IF ok THEN "delivered" ELSE IF SilentFailure THEN "silent" ELSE "failed",
                 !.badDelivery = @ \cup (IF ~ok /\ SilentFailure THEN {r} ELSE {}),
                 !.witnesses = Observe((IF ~ok /\ ~SilentFailure /\ s.req[r].flight # 0 THEN {"loud"} ELSE {})
                    \cup (IF ok /\ s.cp # "up" /\ s.req[r].waker \in Nodes /\ s.req[r].flight # 0 THEN {"offline"} ELSE {}))]

Timeout(r) == /\ s.req[r].state \in {"checked", "parked", "dial"}
              /\ s' = [s EXCEPT !.req[r].state = IF SilentFailure THEN "silent" ELSE "failed",
                                !.badDelivery = @ \cup (IF SilentFailure THEN {r} ELSE {}),
                                !.witnesses = Observe(IF ~SilentFailure /\ s.req[r].flight # 0 THEN {"loud"} ELSE {})]

CrashVM(v) == /\ s.vm[v].phase = "ready" /\ s.failures < MaxFailures
              /\ s' = [s EXCEPT !.vm[v].phase = "dead", !.failures = @ + 1]
ExpireProbe(v) == /\ v \in s.fresh /\ s' = [s EXCEPT !.fresh = @ \ {v}]
Probe(v) == /\ v \in s.reported
            /\ s' = [s EXCEPT !.healthy = IF s.vm[v].phase = "ready" THEN @ \cup {v} ELSE @ \ {v},
                              !.fresh = IF s.vm[v].phase = "ready" THEN @ \cup {v} ELSE @ \ {v}]
WithdrawStale(v, r) ==
    /\ s.req[r].state = "checked" /\ s.req[r].waker = s.vm[v].node
    /\ s.active[s.req[r].waker] = 0 /\ v \in s.reported
    /\ v \notin s.fresh \cap s.healthy
    /\ \A q \in Requests : ~(s.req[q].state = "dial" /\ s.req[q].target = v)
    /\ s' = [s EXCEPT !.vm[v].phase = "destroyed", !.reported = @ \ {v},
                      !.fresh = @ \ {v}, !.healthy = @ \ {v}]

LeaveCP(mode) ==
    /\ s.cp = "up" /\ s.leaves < MaxLeaves /\ mode \in {"absent", "crashed"}
    /\ s' = [s EXCEPT !.cp = mode, !.leaves = @ + 1,
              !.active["CP"] = IF mode = "crashed" THEN 0 ELSE @,
              !.flight = [v \in Requests |-> IF mode = "crashed" /\ s.flight[v].waker = "CP" /\ s.flight[v].state = "running"
                                             THEN [s.flight[v] EXCEPT !.state = "lost"] ELSE s.flight[v]],
              !.rows = IF mode = "crashed" THEN s.durable ELSE @,
              !.rowHealthy = IF mode = "crashed" THEN s.durable ELSE @,
              !.pc = IF mode = "crashed" THEN "idle" ELSE @]
ReturnCP == /\ s.cp # "up"
            /\ s' = [s EXCEPT !.cp = "up",
                      !.returnedBoots = IF Witness = "return" THEN @ \cup {v \in Requests : s.vm[v].origin = "ACTIVATOR" /\ s.vm[v].phase = "boot"} ELSE @]

BeginReconcile == /\ s.cp = "up" /\ s.pc = "idle"
                  /\ s' = [s EXCEPT !.pc = "existing", !.facts = s.reported,
                                    !.factsHealthy = s.healthy]
AdoptExisting ==
    /\ s.cp = "up" /\ s.pc = "existing"
    /\ s' = [s EXCEPT !.rowHealthy = (s.rows \cap s.factsHealthy),
                      !.pc = IF OrphansFirst THEN "orphan" ELSE "adopt"]
AdoptActivator ==
    LET adopt == IF Leak THEN {} ELSE {v \in s.facts : s.vm[v].origin = "ACTIVATOR"}
    IN /\ s.cp = "up" /\ s.pc = "adopt"
       /\ s' = [s EXCEPT !.rows = @ \cup adopt,
                 !.rowHealthy = @ \cup (adopt \cap s.factsHealthy),
                 !.witnesses = Observe((IF (adopt \ s.rows) \cap s.returnedBoots # {} THEN {"return"} ELSE {})
                    \cup (IF Cardinality(Live) > 1 /\ (adopt \ s.rows) # {} THEN {"duplicate"} ELSE {})),
                 !.pc = "backfill"]
Backfill(d) == /\ s.cp = "up" /\ s.pc = "backfill" /\ d \subseteq s.rows
               /\ s' = [s EXCEPT !.durable = @ \cup d, !.pc = "evict"]
ReconcileStep ==
    /\ s.cp = "up" /\ s.pc \in {"evict", "withdraw", "redrive"}
    /\ s' = [s EXCEPT !.pc = CASE s.pc = "evict" -> "withdraw"
                              [] s.pc = "withdraw" -> "redrive"
                              [] OTHER -> IF OrphansFirst THEN "publish" ELSE "orphan"]
Orphans ==
    LET victims == {v \in s.facts :
          (ReapActivator /\ s.vm[v].origin = "ACTIVATOR") \/
          ((SkipOriginGuard \/ s.vm[v].origin # "ACTIVATOR") /\ s.active["CP"] = 0 /\ v \notin s.rows)}
    IN /\ s.cp = "up" /\ s.pc = "orphan"
       /\ s' = [s EXCEPT !.vm = [v \in Requests |-> IF v \in victims THEN [s.vm[v] EXCEPT !.phase = "destroyed"] ELSE s.vm[v]],
                 !.reported = @ \ victims, !.fresh = @ \ victims, !.healthy = @ \ victims,
                 !.reaped = @ \cup {v \in victims : s.vm[v].origin = "ACTIVATOR" /\ s.vm[v].phase = "ready"},
                 !.witnesses = Observe(IF Cardinality(Live) > 1 /\ victims # {} THEN {"duplicate"} ELSE {}),
                 !.pc = IF OrphansFirst THEN "adopt" ELSE "publish"]
Publish ==
    LET endpoints == s.rows \cap s.rowHealthy
        extra == IF UnownedPublish THEN {v \in Requests : s.vm[v].phase = "ready" /\ v \notin s.winners \cup s.rows} ELSE {}
    IN /\ s.cp = "up" /\ s.pc = "publish"
       /\ s' = [s EXCEPT !.published = endpoints \cup extra,
                 !.targeted = @ \cup endpoints \cup extra,
                 !.unownedUse = @ \cup extra,
                 !.stranded = @ \cup (IF Settled THEN Live \ s.rows ELSE {}),
                 !.route = Preferred, !.pc = "idle"]

Next == \/ \E n \in Nodes : Advertise(n)
        \/ \E r \in Requests : CheckMiss(r) \/ UnavailableCP(r) \/ DialResult(r) \/ Timeout(r)
        \/ \E r \in Requests, n \in Nodes : Join(r, n) \/ CPMiss(r, n)
        \/ \E r \in Requests, v \in Requests : ChooseRoute(r, v) \/ CPStraggler(r, v) \/ CheckStraggler(r, v) \/ Forward(r, v) \/ WithdrawStale(v, r)
        \/ \E v \in Requests : Ready(v) \/ Register(v) \/ Complete(v) \/ BootFailure(v) \/ CompleteFailure(v) \/ CrashVM(v) \/ ExpireProbe(v) \/ Probe(v)
        \/ \E mode \in {"absent", "crashed"} : LeaveCP(mode)
        \/ ReturnCP \/ BeginReconcile \/ AdoptExisting \/ AdoptActivator
        \/ (\E d \in SUBSET Requests : Backfill(d)) \/ ReconcileStep \/ Orphans \/ Publish

TypeOK == /\ s.cp \in {"up", "absent", "crashed"}
          /\ s.leaves \in 0..MaxLeaves /\ s.failures \in 0..MaxFailures
          /\ s.active \in [Wakers -> Requests \cup {0}]
          /\ \A v \in Requests : s.vm[v] \in [node : Nodes \cup {"none"}, origin : {"none", "CP", "ACTIVATOR"}, phase : {"empty", "boot", "ready", "dead", "destroyed"}]
          /\ \A v \in Requests : s.flight[v] \in [waker : Wakers \cup {"none"}, state : {"free", "running", "complete", "failed", "lost"}]
          /\ \A r \in Requests : s.req[r] \in [state : {"new", "checked", "parked", "dial", "delivered", "failed", "silent"}, waker : Wakers \cup {"none"}, flight : Requests \cup {0}, target : Requests \cup {0}]
          /\ \A set \in {s.rows, s.durable, s.rowHealthy, s.reported, s.healthy, s.fresh, s.winners, s.everReady, s.targeted, s.unownedUse, s.badDelivery, s.reaped, s.stranded, s.facts, s.factsHealthy, s.returnedBoots, s.published} : set \subseteq Requests
          /\ s.ads \subseteq Nodes /\ s.route \in Wakers
          /\ s.pc \in {"idle", "existing", "adopt", "backfill", "evict", "withdraw", "redrive", "orphan", "publish"}
          /\ s.witnesses \subseteq {"offline", "return", "duplicate", "loud"}
OneWinnerPerFlight == \A r \in Requests : s.req[r].flight # 0 /\ s.req[r].target # 0 => s.req[r].target = s.req[r].flight
NoUntrackedServing == s.unownedUse = {}
NoLegitActivatorWakeReaped == s.reaped = {}
PublishAfterReady == s.targeted \subseteq s.everReady
NoDeadSplice == s.badDelivery = {} /\ \A r \in Requests : s.req[r].state # "silent"
NoLeak == s.stranded = {}
AtMostOneVMPerWorkload == Cardinality(Live) <= 1
NeverOfflineDelivery == "offline" \notin s.witnesses
NeverReturnAdoption == "return" \notin s.witnesses
NeverDuplicateResolved == "duplicate" \notin s.witnesses
NeverLoudFailure == "loud" \notin s.witnesses

Spec == Init /\ [][Next]_vars
FairSpec == Spec /\ WF_vars(ReturnCP) /\ WF_vars(BeginReconcile)
            /\ WF_vars(AdoptExisting) /\ WF_vars(AdoptActivator)
            /\ WF_vars(\E d \in SUBSET Requests : Backfill(d))
            /\ WF_vars(ReconcileStep) /\ WF_vars(Orphans) /\ WF_vars(Publish)
            /\ \A v \in Requests : WF_vars(Ready(v) \/ BootFailure(v))
                /\ WF_vars(Register(v)) /\ SF_vars(Complete(v) \/ CompleteFailure(v))
            /\ \A r \in Requests : WF_vars(Timeout(r))
EventualAccounting == \A v \in Requests : (s.vm[v].phase # "empty") ~> (v \in s.rows \/ s.vm[v].phase = "destroyed")
RequestsResolve == \A r \in Requests : (s.req[r].state # "new") ~> (s.req[r].state \in {"delivered", "failed"})
=============================================================================
