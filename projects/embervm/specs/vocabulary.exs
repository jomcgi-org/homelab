# Layer-1 vocabulary manifest for the adoption spec (ADR embervm/006, layer 1).
#
# This declares, per implementation surface, which enum members the adoption.tla
# model MODELS versus which it deliberately EXCLUDES. spec_vocabulary_test.exs
# reads this with Code.eval_file and asserts three things per surface:
#
#   1. modeled and excluded PARTITION the live implementation enum (every current
#      member appears in exactly one bucket, no extras, no gaps). A new rpc verb,
#      health state, or op-log kind that lands in neither bucket fails CI and
#      forces a human decision: model it, or exclude it here with a reason.
#   2. modeled and excluded are DISJOINT.
#   3. every modeled name's string appears verbatim in adoption.tla (the freshness
#      cross-check: a name we claim to model must actually be mentioned in the
#      spec's prose map or actions).
#
# The exclusions are grouped by feature rung with a one-line reason each. Per the
# ADR the pilot models exactly one protocol (VM lifecycle + adoption); serving,
# stateful, groups, sessions, continuity, and FaaS are out of scope by decision,
# so their vocabulary is excluded, not modeled.
%{
  # -- node.proto RPC verbs (proto/embervm/node/v1/node.proto) ----------------
  # The adoption spec models the R0 task-lifecycle + node-health surface: Prime a
  # VM, Assign it to a task, Destroy it, and the WatchNode / GetNodeStatus status
  # flow that drives the health machine and adoption reconcile.
  proto_rpcs: %{
    modeled: ~w(Prime Assign Destroy WatchNode GetNodeStatus)a,
    excluded:
      ~w(
        BuildBase
      )a ++
        # R2 sessions: bank/relight lifecycle, out of scope (protocol 2 in the ADR).
        ~w(SessionAssign Bank Relight EvictSnapshot)a ++
        # R3 serving: long-lived HTTP-over-tap VMs, out of scope.
        ~w(StartServing StopServing)a ++
        # R4 stateful: singleton volume-owning VMs + generation pairing, out of scope.
        ~w(StartStateful StopStateful ResolveStateful DeleteVolume)a ++
        # R5 groups: composite multi-member workloads, out of scope.
        ~w(CreateGroupNetwork DeleteGroupNetwork StartGroupMember StopGroupMember)a ++
        # R6 continuity: off-node artifact durability, out of scope.
        ~w(ExportArtifact RestoreArtifact EvictArtifact)a ++
        # Artifact-decoupling Phase 2: control-plane -> daemon workload-registry
        # push verbs (SyncRegistry converges the pushed set; Register/Deregister
        # are the incremental forms). They deliver node-side image identity, not
        # VM lifecycle or adoption, so they are outside the adoption TLA model.
        ~w(SyncRegistry RegisterWorkload DeregisterWorkload)a
    # BuildBase (R0): the once-per-image base bake. The spec abstracts the pool as
    # an inexhaustible id supply (RecycleId) and never models base builds, so it is
    # excluded even though it is an R0 verb.
  },

  # -- NodeRegistry health states (Embervm.NodeRegistry.health_states/0) -------
  # The whole health machine (starting -> healthy -> unknown -> down) is the
  # adoption spec's node-side state; every state is modeled.
  health_states: %{
    modeled: ~w(starting healthy unknown down)a,
    excluded: []
  },

  # -- op-log kinds (Embervm.OpLog.kinds/0) -----------------------------------
  # The spec's durable taskState mirrors the op-log's task-lifecycle kinds:
  # submitted (a queued task at init), assigned (DispatchWarm / DispatchMiss),
  # succeeded (Succeed), and primed (the VM Prime deposit path). These four atoms
  # appear verbatim in adoption.tla, which the freshness test enforces.
  op_kinds: %{
    modeled: ~w(submitted assigned succeeded primed)a,
    excluded:
      # R0 task/VM lifecycle kinds the spec does NOT model as distinct actions.
      # vm_destroyed is the durable audit kind for a VM teardown; the spec models
      # the VM state reaching "destroyed" (Succeed / AbandonClaim / CrashNode) but
      # not the log-emission verb as a named action, so the atom vm_destroyed is
      # excluded (its string is absent from the spec by design). started collapses
      # into assigned (the spec's taskState has no separate running state);
      # base_built pairs with the excluded BuildBase verb; drain / quota_enforced /
      # denied are metering + drain concerns out of the adoption model's scope.
      ~w(started vm_destroyed base_built denied drain quota_enforced retried
         redrive dead_lettered failed)a ++
        # R2 session lifecycle kinds, out of scope (protocol 2 in the ADR).
        # session_destroying is the ADR embervm/014 node-confirmed-destroy intent kind
        # (durable destroy intent before the confirmed teardown RPC); it rides the same
        # out-of-scope R2 session lifecycle, so it is excluded alongside its terminal
        # session_destroyed. The adoption spec's destroying-state + destroy invariant
        # are added in the PR 5 TLA follow-through, not modeled off this kind's string.
        ~w(session_created session_invoked session_banked session_relit
           session_expired session_evicted session_destroying session_destroyed
           session_failed)a ++
        # R3 serving lifecycle kinds, out of scope. serving_destroying is the
        # ADR embervm/014 destroy-intent kind (see session_destroying).
        ~w(serving_started serving_published serving_unpublished serving_banked
           serving_relit serving_evicted serving_destroying serving_destroyed
           serving_failed serving_stats)a ++
        # R4 stateful lifecycle + volume kinds, out of scope. generation_blessed
        # is the volume-generation blessing ledger audit kind (control plane as
        # sole generation issuer), part of the same out-of-scope volume machinery.
        # stateful_destroying is the ADR embervm/014 destroy-intent kind.
        ~w(volume_created volume_deleted generation_blessed stateful_started
           stateful_published stateful_unpublished stateful_banked stateful_relit
           stateful_cold_booted stateful_evicted stateful_destroying stateful_destroyed
           stateful_failed stateful_stats)a ++
        # R5 composite-group lifecycle kinds, out of scope. group_destroying is the
        # ADR embervm/014 destroy-intent kind.
        ~w(group_created group_net_created group_net_deleted group_member_started
           group_running group_published group_unpublished group_banked group_relit
           group_fresh_booted group_set_evicted group_degraded group_destroying
           group_destroyed group_failed group_stats)a ++
        # R6 continuity (node drain + off-node artifact) kinds, out of scope.
        ~w(node_drain_started node_drain_finished artifact_exported
           artifact_restored artifact_evicted_remote)a
  }
}
