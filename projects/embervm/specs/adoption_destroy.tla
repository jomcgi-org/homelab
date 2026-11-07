-------------------------- MODULE adoption_destroy --------------------------
EXTENDS adoption
CONSTANT InjectUnownedCompletion
DC == INSTANCE destroy_contract

\* Check the existing model's actual Next relation against the same contract
\* used by trace replay. No lifecycle actions are copied into this wrapper.
\* The deliberate mutant proves that completing without intent is rejected.
UnownedCompletion ==
    \E v \in VMs \ (destroying \cup cpDestroyed) :
        /\ cpDestroyed' = cpDestroyed \cup {v}
        /\ UNCHANGED <<vmState, vmNode, vmPrincipal, statusCh, cpAlive, health,
             streamerGen, lastGen, inventory, inflightMeta, lastReport,
             taskState, taskVM, taskPrincipal, destroying, reapedLive,
             cpCrashes, nodeCrashes, genCtr>>

CheckedNext == Next \/ (InjectUnownedCompletion /\ UnownedCompletion)
CheckedSpec == Init /\ [][CheckedNext]_vars
DestroyConformance ==
    /\ DC!Initial(destroying, cpDestroyed)
    /\ [][DC!Step(VMs, destroying, cpDestroyed, destroying', cpDestroyed',
                 {v \in VMs : vmState'[v] = "destroyed"})]_<<destroying, cpDestroyed>>
\* A negative reachability fixture proves the checked model can complete.
NeverCompletes == cpDestroyed = {}
=============================================================================
