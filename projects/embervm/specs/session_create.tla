----------------------------- MODULE session_create -----------------------------
(*****************************************************************************)
(* SessionManager create starvation model (issue #5051).                    *)
(*                                                                           *)
(* SessionManager is a GenServer with one mailbox. Periodic reconcile and    *)
(* create requests are dequeued serially. Before the fix, do_reconcile ran   *)
(* node RPCs on the manager. An unresponsive node could therefore hold the   *)
(* current reconcile forever while create requests waited behind it.         *)
(* ReconcileBlocking = FALSE models the fixed implementation: node work is   *)
(* moved off the manager, so the manager spends only one tick on reconcile.  *)
(*                                                                           *)
(* The initial mailbox contains one reconcile followed by every bounded      *)
(* create request. This is the minimal ordering that exposes the wedge. The  *)
(* manager can dequeue only when processing = NULL, exactly like a single    *)
(* GenServer mailbox. In blocking mode WaitSlowNode cycles forever and the   *)
(* create messages remain queued. In fixed mode ProcessTick completes the    *)
(* reconcile and then every create.                                           *)
(*****************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    MaxPending,       \* mailbox capacity, configured in the range 2..3
    MaxCreates,       \* number of submitted create ids, in the range 1..2
    MaxSlow,          \* bounded wait-counter domain, in the range 1..2
    ReconcileBlocking,\* TRUE models the blocking do_reconcile implementation
    NULL              \* no message is currently being processed

CreateIds == 1..MaxCreates
ReconcileMessage == [kind |-> "reconcile", id |-> 0]
CreateMessage(id) == [kind |-> "create", id |-> id]
Messages == {ReconcileMessage} \cup {CreateMessage(id) : id \in CreateIds}
InitialMailbox == <<ReconcileMessage>> \o [id \in CreateIds |-> CreateMessage(id)]

(*--algorithm session_create
variables
    \* The bounded GenServer mailbox. Reconcile is already ahead of creates,
    \* reproducing the ordering seen in the #5051 starvation incident.
    mailbox = InitialMailbox,

    \* The message currently owned by the manager and its remaining work.
    processing = NULL,
    procRemaining = 0,

    \* Create ids whose handler reached its terminal created step.
    created = {},

    \* The rebuild found an unresponsive node. It stays slow for this model's
    \* behavior, allowing the blocking RPC to demonstrate an actual wedge.
    nodeSlow = TRUE;

define
    TypeOK ==
        /\ MaxPending \in 2..3
        /\ MaxCreates \in 1..2
        /\ MaxSlow \in 1..2
        /\ ReconcileBlocking \in BOOLEAN
        /\ mailbox \in Seq(Messages)
        /\ Len(mailbox) <= MaxPending
        /\ processing \in Messages \cup {NULL}
        /\ procRemaining \in 0..MaxSlow
        /\ (processing = NULL) = (procRemaining = 0)
        /\ created \subseteq CreateIds
        /\ nodeSlow \in BOOLEAN

    EventuallyCreated == \A id \in CreateIds : <> (id \in created)
end define;

begin
ManagerLoop:
    while TRUE do
        either
            \* A GenServer dequeues only when it is not already in a callback.
            await processing = NULL /\ mailbox # << >>;
            processing := Head(mailbox) ||
            mailbox := Tail(mailbox) ||
            procRemaining :=
                IF Head(mailbox).kind = "reconcile" /\ ReconcileBlocking
                THEN MaxSlow
                ELSE 1;

        or
            \* Fixed reconcile work and create handlers both finish in one tick.
            \* A blocking slow reconcile cannot take this branch.
            await processing # NULL
                  /\ ~(processing.kind = "reconcile"
                       /\ ReconcileBlocking /\ nodeSlow);
            if processing.kind = "create" then
                created := created \cup {processing.id};
            end if;
            processing := NULL ||
            procRemaining := 0;

        or
            \* The old on-manager RPC never returns. Cycling the bounded counter
            \* represents continued waiting without making the state unbounded.
            await processing # NULL
                  /\ processing.kind = "reconcile"
                  /\ ReconcileBlocking /\ nodeSlow;
            if procRemaining = 1 then
                procRemaining := MaxSlow;
            else
                procRemaining := procRemaining - 1;
            end if;
        end either;
    end while;
end algorithm; *)

\* BEGIN TRANSLATION
VARIABLES mailbox, processing, procRemaining, created, nodeSlow

(* define statement *)
TypeOK ==
    /\ MaxPending \in 2..3
    /\ MaxCreates \in 1..2
    /\ MaxSlow \in 1..2
    /\ ReconcileBlocking \in BOOLEAN
    /\ mailbox \in Seq(Messages)
    /\ Len(mailbox) <= MaxPending
    /\ processing \in Messages \cup {NULL}
    /\ procRemaining \in 0..MaxSlow
    /\ (processing = NULL) = (procRemaining = 0)
    /\ created \subseteq CreateIds
    /\ nodeSlow \in BOOLEAN

EventuallyCreated == \A id \in CreateIds : <> (id \in created)


vars == << mailbox, processing, procRemaining, created, nodeSlow >>

Init == (* Global variables *)
        /\ mailbox = InitialMailbox
        /\ processing = NULL
        /\ procRemaining = 0
        /\ created = {}
        /\ nodeSlow = TRUE

Next == /\ \/ /\ processing = NULL /\ mailbox # << >>
              /\ /\ mailbox' = Tail(mailbox)
                 /\ procRemaining' = (IF Head(mailbox).kind = "reconcile" /\ ReconcileBlocking
                                      THEN MaxSlow
                                      ELSE 1)
                 /\ processing' = Head(mailbox)
              /\ UNCHANGED created
           \/ /\ processing # NULL
                 /\ ~(processing.kind = "reconcile"
                      /\ ReconcileBlocking /\ nodeSlow)
              /\ IF processing.kind = "create"
                    THEN /\ created' = (created \cup {processing.id})
                    ELSE /\ TRUE
                         /\ UNCHANGED created
              /\ /\ procRemaining' = 0
                 /\ processing' = NULL
              /\ UNCHANGED mailbox
           \/ /\ processing # NULL
                 /\ processing.kind = "reconcile"
                 /\ ReconcileBlocking /\ nodeSlow
              /\ IF procRemaining = 1
                    THEN /\ procRemaining' = MaxSlow
                    ELSE /\ procRemaining' = procRemaining - 1
              /\ UNCHANGED <<mailbox, processing, created>>
        /\ UNCHANGED nodeSlow

Spec == Init /\ [][Next]_vars

\* END TRANSLATION

\* Fair scheduling requires the manager to take a progress step whenever one
\* remains continuously enabled. It cannot make a blocked node RPC return.
FairSpec == Spec /\ WF_vars(Next)

=============================================================================
