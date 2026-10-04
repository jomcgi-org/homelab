--------------------------- MODULE ExpertTiering ---------------------------
(***************************************************************************)
(* Expert tiering protocol for oom-inference.                              *)
(*                                                                         *)
(* Every expert lives on disk. Bounded host buffers and bounded VRAM       *)
(* slots cache copies of experts. Copies move disk -> host -> VRAM through *)
(* asynchronous transfers that are in flight for several steps. The engine *)
(* routes a layer to a set of experts, makes each one executable (a ready  *)
(* VRAM slot for the GPU path, or a ready host buffer for the CPU expert   *)
(* path), then launches. Launched work runs asynchronously, behind the     *)
(* host, in stream order.                                                  *)
(*                                                                         *)
(* A captured CUDA graph bakes slot addresses in. While it is replayable   *)
(* those slots must keep holding the experts they held at capture.         *)
(* Routed (non-graph) launches resolve expert -> slot through the slot     *)
(* table at launch time, so they bake in nothing.                          *)
(*                                                                         *)
(* Prefetch and placement policy are modelled as unconstrained             *)
(* nondeterminism: any expert may be staged at any time and any unpinned   *)
(* copy may be evicted at any time. The safety properties must hold no     *)
(* matter what the policy does.                                            *)
(*                                                                         *)
(* Bug selects a deliberately broken variant, to show each invariant has   *)
(* teeth. Bug = "none" is the protocol the engine implements.              *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Experts,      \* expert ids (one layer's worth is enough: layers are independent)
    Slots,        \* VRAM slots (fixed arena)
    HostBufs,     \* pinned host staging/cache buffers (fixed arena)
    TopK,         \* experts routed per launch
    MaxInflight,  \* launches the GPU stream may run behind the host
    GraphMax,     \* experts a captured graph may bake in
    None,         \* model value: empty slot / unmapped
    Garbage,      \* model value: bytes that are not any expert
    Bug           \* "none" or one of BugVariants

BugVariants == {"skip_kernel_pin", "skip_copy_pin", "skip_graph_pin",
                "table_before_complete"}

ASSUME Bug \in {"none"} \cup BugVariants
ASSUME TopK >= 1 /\ MaxInflight >= 1

VARIABLES
    vram,      \* slot -> expert bytes it holds (or None / Garbage)
    vramSt,    \* slot -> "free" | "loading" | "ready"
    host,      \* host buffer -> expert bytes it holds (or None / Garbage)
    hostSt,    \* host buffer -> "free" | "loading" | "ready"
    table,     \* expert -> slot or None: the engine's slot table
    d2h,       \* in-flight disk -> host copies, set of <<e, h>>
    h2v,       \* in-flight host -> VRAM copies, set of <<e, h, s>>
    pending,   \* experts routed for the next launch ({} when none)
    inflight,  \* launched, not yet completed work, in stream order;
               \* each entry maps expert -> <<"gpu", slot>> or <<"cpu", hostbuf>>
    graph      \* None, or the captured graph's baked map expert -> slot

vars == <<vram, vramSt, host, hostSt, table, d2h, h2v, pending, inflight, graph>>

Bytes == Experts \cup {None, Garbage}
Locs  == ({"gpu"} \X Slots) \cup ({"cpu"} \X HostBufs)

TypeOK ==
    /\ vram \in [Slots -> Bytes]
    /\ vramSt \in [Slots -> {"free", "loading", "ready"}]
    /\ host \in [HostBufs -> Bytes]
    /\ hostSt \in [HostBufs -> {"free", "loading", "ready"}]
    /\ table \in [Experts -> Slots \cup {None}]
    /\ d2h \subseteq Experts \X HostBufs
    /\ h2v \subseteq Experts \X HostBufs \X Slots
    /\ pending \subseteq Experts
    /\ Len(inflight) <= MaxInflight
    /\ graph = None \/ \E G \in SUBSET Experts : graph \in [G -> Slots]

----------------------------------------------------------------------------
(* Derived sets *)

InHost(e) == \E h \in HostBufs : host[h] = e
InVram(e) == \E s \in Slots : vram[s] = e

\* Locations read by launched work that has not completed.
InflightLocs ==
    UNION { { inflight[i][e] : e \in DOMAIN inflight[i] } : i \in 1..Len(inflight) }

InflightSlots == { l[2] : l \in { l \in InflightLocs : l[1] = "gpu" } }
InflightHosts == { l[2] : l \in { l \in InflightLocs : l[1] = "cpu" } }
CopySrcHosts  == { c[2] : c \in h2v }
GraphSlots    == IF graph = None THEN {} ELSE { graph[e] : e \in DOMAIN graph }

\* Routed experts that are already resident stay put until launch, and the
\* host copy of a routed expert not yet in VRAM stays put until it is.
PendingSlots == { s \in Slots : vram[s] \in pending /\ vramSt[s] = "ready" }
PendingHosts == { h \in HostBufs : host[h] \in pending /\ ~InVram(host[h]) }

VramPinned(s) ==
    \/ s \in InflightSlots /\ Bug /= "skip_kernel_pin"
    \/ s \in GraphSlots    /\ Bug /= "skip_graph_pin"
    \/ s \in PendingSlots

HostPinned(h) ==
    \/ h \in InflightHosts /\ Bug /= "skip_kernel_pin"
    \/ h \in CopySrcHosts  /\ Bug /= "skip_copy_pin"
    \/ h \in PendingHosts

----------------------------------------------------------------------------
(* Actions *)

Init ==
    /\ vram = [s \in Slots |-> None]
    /\ vramSt = [s \in Slots |-> "free"]
    /\ host = [h \in HostBufs |-> None]
    /\ hostSt = [h \in HostBufs |-> "free"]
    /\ table = [e \in Experts |-> None]
    /\ d2h = {}
    /\ h2v = {}
    /\ pending = {}
    /\ inflight = << >>
    /\ graph = None

\* The router picks the experts for the next layer launch.
Route ==
    /\ pending = {}
    /\ \E R \in SUBSET Experts :
        /\ R /= {}
        /\ Cardinality(R) <= TopK
        /\ pending' = R
    /\ UNCHANGED <<vram, vramSt, host, hostSt, table, d2h, h2v, inflight, graph>>

\* Start reading expert e from disk into a free host buffer (demand or prefetch).
StageD2H(e, h) ==
    /\ ~InHost(e)
    /\ hostSt[h] = "free"
    /\ host' = [host EXCEPT ![h] = e]
    /\ hostSt' = [hostSt EXCEPT ![h] = "loading"]
    /\ d2h' = d2h \cup {<<e, h>>}
    /\ UNCHANGED <<vram, vramSt, table, h2v, pending, inflight, graph>>

\* A disk read completes. Disk always holds the right bytes.
CompleteD2H(c) ==
    /\ c \in d2h
    /\ hostSt' = [hostSt EXCEPT ![c[2]] = "ready"]
    /\ d2h' = d2h \ {c}
    /\ UNCHANGED <<vram, vramSt, host, table, h2v, pending, inflight, graph>>

\* Start copying a ready host copy of e into a free VRAM slot.
StageH2V(e, h, s) ==
    /\ hostSt[h] = "ready"
    /\ host[h] = e
    /\ ~InVram(e)
    /\ vramSt[s] = "free"
    /\ vram' = [vram EXCEPT ![s] = e]
    /\ vramSt' = [vramSt EXCEPT ![s] = "loading"]
    /\ h2v' = h2v \cup {<<e, h, s>>}
    /\ table' = IF Bug = "table_before_complete"
                   THEN [table EXCEPT ![e] = s]
                   ELSE table
    /\ UNCHANGED <<host, hostSt, d2h, pending, inflight, graph>>

\* A host -> VRAM copy completes. The slot receives whatever the source
\* buffer holds at that moment: if the source was recycled mid-copy the
\* slot holds garbage, though the engine still believes it holds e.
CompleteH2V(c) ==
    /\ c \in h2v
    /\ LET e == c[1]  h == c[2]  s == c[3] IN
        /\ vram' = [vram EXCEPT ![s] = IF host[h] = e /\ hostSt[h] = "ready"
                                          THEN e ELSE Garbage]
        /\ vramSt' = [vramSt EXCEPT ![s] = "ready"]
        /\ table' = [table EXCEPT ![e] = s]
    /\ h2v' = h2v \ {c}
    /\ UNCHANGED <<host, hostSt, d2h, pending, inflight, graph>>

\* Evict any unpinned ready host buffer (policy is unconstrained).
EvictHost(h) ==
    /\ hostSt[h] = "ready"
    /\ ~HostPinned(h)
    /\ host' = [host EXCEPT ![h] = None]
    /\ hostSt' = [hostSt EXCEPT ![h] = "free"]
    /\ UNCHANGED <<vram, vramSt, table, d2h, h2v, pending, inflight, graph>>

\* Evict any unpinned ready VRAM slot and unmap it.
EvictVram(s) ==
    /\ vramSt[s] = "ready"
    /\ ~VramPinned(s)
    /\ vram' = [vram EXCEPT ![s] = None]
    /\ vramSt' = [vramSt EXCEPT ![s] = "free"]
    /\ table' = [e \in Experts |-> IF table[e] = s THEN None ELSE table[e]]
    /\ UNCHANGED <<host, hostSt, d2h, h2v, pending, inflight, graph>>

\* Where the engine may run expert e right now, from its own bookkeeping.
Executable(e) ==
    (IF table[e] = None THEN {} ELSE {<<"gpu", table[e]>>})
    \cup { <<"cpu", h>> : h \in { h \in HostBufs : host[h] = e /\ hostSt[h] = "ready" } }

\* Launch the routed layer once every routed expert is executable.
Launch ==
    /\ pending /= {}
    /\ Len(inflight) < MaxInflight
    /\ \A e \in pending : Executable(e) /= {}
    /\ \E assign \in [pending -> Locs] :
        /\ \A e \in pending : assign[e] \in Executable(e)
        /\ inflight' = Append(inflight, assign)
    /\ pending' = {}
    /\ UNCHANGED <<vram, vramSt, host, hostSt, table, d2h, h2v, graph>>

\* The oldest launched work completes (stream order).
Complete ==
    /\ Len(inflight) > 0
    /\ inflight' = Tail(inflight)
    /\ UNCHANGED <<vram, vramSt, host, hostSt, table, d2h, h2v, pending, graph>>

\* Capture a graph that bakes in the current slots of some mapped experts.
Capture ==
    /\ graph = None
    /\ \E G \in SUBSET { e \in Experts : table[e] /= None } :
        /\ G /= {}
        /\ Cardinality(G) <= GraphMax
        /\ graph' = [e \in G |-> table[e]]
    /\ UNCHANGED <<vram, vramSt, host, hostSt, table, d2h, h2v, pending, inflight>>

\* Replay the captured graph: it reads exactly the baked slots.
Replay ==
    /\ graph /= None
    /\ Len(inflight) < MaxInflight
    /\ inflight' = Append(inflight, [e \in DOMAIN graph |-> <<"gpu", graph[e]>>])
    /\ UNCHANGED <<vram, vramSt, host, hostSt, table, d2h, h2v, pending, graph>>

\* Drop the graph (e.g. placement policy wants its slots back). Replays
\* already in flight stay pinned through InflightSlots.
Invalidate ==
    /\ graph /= None
    /\ graph' = None
    /\ UNCHANGED <<vram, vramSt, host, hostSt, table, d2h, h2v, pending, inflight>>

Next ==
    \/ Route
    \/ \E e \in Experts, h \in HostBufs : StageD2H(e, h)
    \/ \E c \in d2h : CompleteD2H(c)
    \/ \E e \in Experts, h \in HostBufs, s \in Slots : StageH2V(e, h, s)
    \/ \E c \in h2v : CompleteH2V(c)
    \/ \E h \in HostBufs : EvictHost(h)
    \/ \E s \in Slots : EvictVram(s)
    \/ Launch
    \/ Complete
    \/ Capture
    \/ Replay
    \/ Invalidate

----------------------------------------------------------------------------
(* Fairness: the engine's demand path for the routed layer. Prefetch, policy *)
(* eviction, capture and replay get no fairness: they may happen or not.    *)

\* The demand path stages routed experts that are not yet resident.
DemandStageD2H == \E e \in pending, h \in HostBufs : ~InVram(e) /\ StageD2H(e, h)
DemandStageH2V == \E e \in pending, h \in HostBufs, s \in Slots : StageH2V(e, h, s)

\* It frees a slot or buffer only when a routed expert is waiting for one.
DemandEvictVram == /\ \E e \in pending : ~InVram(e)
                   /\ \A s \in Slots : vramSt[s] /= "free"
                   /\ \E s \in Slots : EvictVram(s)
DemandEvictHost == /\ \E e \in pending : ~InVram(e) /\ ~InHost(e)
                   /\ \A h \in HostBufs : hostSt[h] /= "free"
                   /\ \E h \in HostBufs : EvictHost(h)

\* Strong fairness on the demand path: prefetch and policy may keep
\* stealing free slots, so the demand actions are only enabled now and
\* then; strong fairness says they still eventually win.
Fairness ==
    /\ WF_vars(\E c \in d2h : CompleteD2H(c))
    /\ WF_vars(\E c \in h2v : CompleteH2V(c))
    /\ WF_vars(Complete)
    /\ SF_vars(Launch)
    /\ SF_vars(DemandStageD2H)
    /\ SF_vars(DemandStageH2V)
    /\ SF_vars(DemandEvictVram)
    /\ SF_vars(DemandEvictHost)

Spec == Init /\ [][Next]_vars /\ Fairness

----------------------------------------------------------------------------
(* Safety *)

\* Launched work only ever reads locations holding the expert it was
\* launched for, fully staged, for as long as it is in flight. Covers both
\* "never overwrite memory an in-flight kernel reads" and "never launch on
\* a slot whose staging has not completed".
KernelReadsValid ==
    \A i \in 1..Len(inflight) : \A e \in DOMAIN inflight[i] :
        LET l == inflight[i][e] IN
            IF l[1] = "gpu"
                THEN vram[l[2]] = e /\ vramSt[l[2]] = "ready"
                ELSE host[l[2]] = e /\ hostSt[l[2]] = "ready"

\* An in-flight host -> VRAM copy's source keeps the expert it is copying.
CopySourceValid ==
    \A c \in h2v : host[c[2]] = c[1] /\ hostSt[c[2]] = "ready"

\* The slot table never points at a slot that does not hold that expert's
\* fully staged bytes.
TableConsistent ==
    \A e \in Experts : table[e] /= None =>
        /\ vram[table[e]] = e
        /\ vramSt[table[e]] = "ready"

\* A replayable graph's baked slots still hold what they held at capture.
GraphValid ==
    graph /= None =>
        \A e \in DOMAIN graph : vram[graph[e]] = e /\ vramSt[graph[e]] = "ready"

\* No expert occupies two slots or two host buffers.
NoDuplicates ==
    /\ \A s1, s2 \in Slots : s1 /= s2 /\ vram[s1] \in Experts => vram[s1] /= vram[s2]
    /\ \A h1, h2 \in HostBufs : h1 /= h2 /\ host[h1] \in Experts => host[h1] /= host[h2]

\* Every launch's routed experts were resolved by the engine's own
\* bookkeeping; with TableConsistent this means resident at launch.
Safety == /\ TypeOK
          /\ KernelReadsValid
          /\ CopySourceValid
          /\ TableConsistent
          /\ GraphValid
          /\ NoDuplicates

(* Liveness *)

\* Every routed layer is eventually launched.
RoutedLayerLaunches == [](pending /= {} => <>(pending = {}))

=============================================================================
