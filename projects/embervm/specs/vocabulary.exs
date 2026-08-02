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
  # adoption.tla models the R0 task-lifecycle + node-health surface: Prime a VM,
  # Assign it to a task, Destroy it, and the WatchNode / GetNodeStatus status flow
  # that drives the health machine and adoption reconcile. bank_relight.tla (ADR 006
  # protocol 2) models the Bank and Relight generation-pairing verbs.
  proto_rpcs: %{
    modeled: ~w(Prime Assign Destroy WatchNode GetNodeStatus Bank Relight)a,
    excluded:
      ~w(
        BuildBase
      )a ++
        # R2 sessions: the remaining bank/relight lifecycle verbs bank_relight.tla
        # does not model as distinct actions. SessionAssign (the warm session claim)
        # and EvictSnapshot (the snapshot-only GC verb) are out of the protocol-2
        # generation-pairing subset; Bank and Relight moved to `modeled` above.
        ~w(SessionAssign EvictSnapshot)a ++
        # R3 serving: long-lived HTTP-over-tap VMs, out of scope.
        ~w(StartServing StopServing)a ++
        # R4 stateful: singleton volume-owning VMs + generation pairing, out of scope.
        ~w(StartStateful StopStateful ResolveStateful DeleteVolume)a ++
        # R5 groups: composite multi-member workloads, out of scope.
        ~w(CreateGroupNetwork DeleteGroupNetwork StartGroupMember StopGroupMember)a ++
        # R6 continuity: off-node artifact durability, out of scope. ListArtifacts
        # is the remote (store) inventory read that remote base retention computes
        # its keep-set from; like its siblings it is durability plumbing, not VM
        # lifecycle or adoption.
        ~w(ExportArtifact RestoreArtifact EvictArtifact ListArtifacts ArchiveVolume RetireVolume)a ++
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
  # adoption.tla's durable taskState mirrors the op-log's task-lifecycle kinds:
  # submitted (a queued task at init), assigned (DispatchWarm / DispatchMiss),
  # succeeded (Succeed), and primed (the VM Prime deposit path). bank_relight.tla
  # (ADR 006 protocol 2) additionally models the session bank/relight/evict and
  # node-confirmed-destroy kinds: session_banked (Bank), session_relit
  # (RelightWarm), session_evicted (EvictBrokenPair), session_destroying
  # (BeginDestroy) and session_destroyed (ConfirmDestroy). Every modeled atom
  # appears verbatim in some spec .tla, which the freshness test enforces across
  # the whole specs/ directory.
  op_kinds: %{
    modeled:
      ~w(submitted assigned succeeded primed)a ++
        # R2 session protocol-2 kinds, modeled by bank_relight.tla (ADR 006
        # protocol 2): bank/relight generation pairing + the ADR 014 node-confirmed
        # destroy carve-out (session_destroying intent -> node confirm ->
        # session_destroyed record).
        ~w(session_banked session_relit session_evicted session_destroying
           session_destroyed)a ++
        # R0 quota gate protocol 3: quota.tla models the submit-time quota
        # denial audit append, while dispatch-side skips are counters only.
        ~w(quota_enforced)a,
    excluded:
      # R0 task/VM lifecycle kinds the spec does NOT model as distinct actions.
      # vm_destroyed is the durable audit kind for a VM teardown; the spec models
      # the VM state reaching "destroyed" (Succeed / AbandonClaim / CrashNode) but
      # not the log-emission verb as a named action, so the atom vm_destroyed is
      # excluded (its string is absent from the spec by design). started collapses
      # into assigned (the spec's taskState has no separate running state);
      # base_built pairs with the excluded BuildBase verb. denied remains excluded
      # because it covers auth-forbidden and per-principal queue-depth audit kinds;
      # quota.tla treats queue depth as a model bound rather than modeling its
      # denial op. drain is a node-drain concern outside the adoption model's scope.
      # session_parking/session_parked are the ADR 027 memory:false quadrant's
      # park intent and completion. They are a session-class durability concern,
      # outside the adoption pilot and outside bank_relight.tla's bank/relight
      # generation pairing (a parked session holds a filesystem lineage volume,
      # not a memory snapshot, so it pairs with no generation).
      ~w(session_parking session_parked)a ++
        ~w(started vm_destroyed base_built denied drain retried
           redrive dead_lettered failed)a ++
        # Remaining R2 session lifecycle kinds still out of scope. The bank/relight,
        # evict, and node-confirmed-destroy kinds moved to `modeled` above (bank_relight.tla,
        # ADR 006 protocol 2). session_created / session_invoked / session_expired /
        # session_failed are the create/invoke/expire/fail lifecycle edges bank_relight
        # does not model (it starts from a running instance and models only the
        # bank/relight/evict/destroy generation-pairing subset).
        ~w(session_created session_invoked session_expired session_failed)a ++
        # R3 serving lifecycle kinds, out of scope. serving_destroying is the
        # ADR embervm/014 destroy-intent kind (see session_destroying).
        ~w(serving_started serving_published serving_unpublished serving_banked
           serving_relit serving_evicted serving_destroying serving_destroyed
           serving_failed serving_stats)a ++
        # R4 stateful lifecycle + volume kinds, out of scope. generation_blessed
        # is the volume-generation blessing ledger audit kind (control plane as
        # sole generation issuer), part of the same out-of-scope volume machinery.
        # blessing_lease_granted is a durability operation, not modeled in
        # adoption.tla per ADR embervm/006 scope.
        # stateful_destroying is the ADR embervm/014 destroy-intent kind.
        ~w(volume_created volume_deleted generation_blessed blessing_lease_granted
           stateful_started
           stateful_published stateful_unpublished stateful_banked stateful_relit
           stateful_cold_booted stateful_evicted stateful_destroying stateful_destroyed
           stateful_failed stateful_stats)a ++
        # R7 checkpoint-abort auto-heal (ADR embervm/017): the durable
        # checkpoint-dispatch record that lets the control plane auto-heal its own
        # auto-aborted checkpoint. Part of the same out-of-scope stateful machinery.
        ~w(checkpoint_dispatched checkpoint_resolved)a ++
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
