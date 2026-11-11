-------------------------- MODULE destroy_contract --------------------------
EXTENDS Naturals, FiniteSets

\* Observable destroy-intent contract. Keys are VM slots in the bounded model
\* and <<session_id, vm_id>> pairs in trace replay. Confirmation evidence is
\* an input: replay validates the recorded claim, not physical node cessation.
Initial(pending, completed) == pending = {} /\ completed = {}

Begin(pending, completed, afterPending, afterCompleted, key) ==
    /\ key \notin completed
    /\ afterPending = pending \cup {key}
    /\ afterCompleted = completed

Confirm(pending, completed, afterPending, afterCompleted, key, evidence) ==
    /\ key \in pending
    /\ key \notin completed
    /\ evidence
    /\ afterPending = pending \ {key}
    /\ afterCompleted = completed \cup {key}

\* Only the finite model recycles slots. Runtime identities never use this.
Recycle(pending, completed, afterPending, afterCompleted, key) ==
    /\ afterPending = pending \ {key}
    /\ afterCompleted = completed \ {key}

Step(keys, pending, completed, afterPending, afterCompleted, confirmed) ==
    \/ /\ afterPending = pending
       /\ afterCompleted = completed
    \/ \E key \in keys :
         \/ Begin(pending, completed, afterPending, afterCompleted, key)
         \/ Confirm(pending, completed, afterPending, afterCompleted, key,
                    key \in confirmed)
         \/ Recycle(pending, completed, afterPending, afterCompleted, key)
=============================================================================
