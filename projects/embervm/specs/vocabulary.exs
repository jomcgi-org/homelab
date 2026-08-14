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
        # NOTE: Pre-split history ambiguity. Before this change, :session_relit was
        # also emitted by the rejoin path (filesystem lineage restore, no generation
        # pairing). Production has about 59 such rows, distinguishable by the
        # absence of `generation` in the payload. Any consumer reading historical
        # :session_relit rows must treat absence of `generation` as a rejoin
        # (filesystem restore) rather than a relight (memory snapshot restore). This
        # payload-based discriminator is self-describing and correct forever,
        # unlike a time-based watermark that would break on restore.
        ~w(session_banked session_relit session_evicted session_destroying
           session_destroyed)a ++
        # R0 quota gate protocol 3: quota.tla models the submit-time quota
        # denial audit append, while dispatch-side skips are counters only.
        ~w(quota_enforced)a,
    excluded:
      # R0 task/VM lifecycle kinds the spec does NOT model as distinct actions.
      # vm_destroyed is the durable audit kind for a VM teardown; the spec models
      # the VM state reaching "destroyed" (Succeed / AbandonClaim / CrashNode) but
      # not the log-emission verb as a named action. Task-lane VMs are reclaimed by
      # the node, not CP; there is no append site. started collapses
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
      # session_rejoined is not modeled by bank_relight.tla (filesystem lineage,
      # no generation pairing).
      ~w(session_parking session_parked session_rejoined)a ++
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
  },
  # -- append sites for every MODELED op kind (layer 1, the #4756 guard) -------
  # Which control-plane module actually appends each modeled kind, declared
  # rather than inferred. spec_vocabulary_test.exs asserts the keys exactly
  # partition the modeled op kinds, the file exists, and the atom appears in it
  # outside `#` comments and @doc/@moduledoc heredocs.
  #
  # WHY A DECLARED REGISTRY RATHER THAN A GREP. #4756: `:primed` and
  # `:vm_destroyed` sat in the closed @kinds enum with NO append site, and layer
  # 1 passed because it only checked enum against spec. Two greps were tried and
  # both failed, which is why this is a manifest:
  #
  #   bare `:atom` anywhere in lib      -> too LOOSE. `:primed` matched a metrics
  #     counter key in base_builder.ex (`Map.get(entry, :primed, 0)`) and a prose
  #     comment, so it was green before any emission existed.
  #   `kind: :atom` literal              -> too STRICT. Only `:primed` is built
  #     literally; every other kind is threaded as a VARIABLE across a module
  #     boundary (`%Op{kind: op_kind}` in task_store, `SessionStore.transition(
  #     ..., :session_banked, ...)` in session_manager), so it failed 8 kinds
  #     that are genuinely emitted.
  #
  # Naming the module is the part a grep cannot infer and a reviewer can check.
  # A kind with no honest site cannot be given one here, which forces the
  # decision the enum's closed-ness was supposed to force: emit it, or move it
  # to `excluded` with a reason (as vm_destroyed now is, since task-lane VMs are
  # reclaimed by the node and no CP-side teardown exists).
  op_kind_sites: %{
    submitted: "task_store.ex",
    assigned: "task_store.ex",
    succeeded: "task_store.ex",
    # :primed builder (Embervm.PrimedOp) is invoked from multiple sites
    # (dispatcher, pool_manager, session_manager), so a shared builder centralizes
    # the payload structure. The guard verifies the builder exists; runtime coverage
    # is the checker's assertion (zero :primed alongside :assigned is vacuous, not passing).
    primed: "primed_op.ex",
    quota_enforced: "metering.ex",
    session_banked: "session_manager.ex",
    session_relit: "session_manager.ex",
    session_evicted: "session_manager.ex",
    session_destroying: "session_manager.ex",
    session_destroyed: "session_manager.ex"
  },
  # -- SpecTrace emission sites (#4770) ---------------------------------------
  # Which module EMITS each spec action, mirroring op_kind_sites above. Name the
  # emission site, NOT `spec_trace.ex`: that module is the transport, and every
  # action passes through it, so declaring it would make this registry
  # unfalsifiable. The first run of this test caught exactly that mistake.
  #
  # Same limitation as op_kind_sites, stated so nobody mistakes it for more: this
  # proves a site EXISTS, not that the path RUNS. #4765 was an append on a code
  # path production never took, and no static registry can see that. The runtime
  # half is the harness coverage assertion (a run with zero `prime` records
  # alongside `assigned` ops is VACUOUS, not PASS).
  spec_trace_sites: %{
    # One hook inside the shared builder covers all three prime sites
    # (dispatcher cold-miss, PoolManager refill, SessionManager session prime).
    prime: "primed_op.ex",
    # On the dispatcher's sweep, emitted EVEN WHEN NOTHING ELSE FIRES. A wedge
    # is the control plane failing to adopt, and absence is invisible in an
    # event stream, so this periodic state observation is what makes
    # non-progress detectable at all.
    checkpoint: "dispatcher.ex",
    recv_status: "node_registry.ex",
    adopt_inventory: "dispatcher.ex",
    dispatch_warm: "dispatcher.ex",
    dispatch_miss: "dispatcher.ex",
    age_to_unknown: "node_registry.ex",
    age_to_down: "node_registry.ex",
    reconnect: "node_registry.ex",
    restart_cp: "dispatcher.ex",
    succeed: "dispatcher.ex",
    begin_destroy: "session_manager.ex",
    confirm_destroy: "session_manager.ex",
    abandon_claim: "dispatcher.ex"
  },

  # Deliberate exclusions, as DATA rather than prose, so an exclusion is an act
  # someone performed rather than an omission nobody noticed. `Reap` was already
  # excluded in a comment; recording it here makes the set enumerable, which is
  # what #4800's proposed check needs:
  #
  #   keys(spec_trace_sites) + keys(spec_trace_excluded) == actions(spec)
  #
  # Both directions matter. An action in neither is unobserved, and an entry here
  # that gains a code site is a stale exclusion.
  # PROSE ACTIONS deliberately unobserved. Keys here are snake_case renderings of
  # entries in adoption.tla's prose map, and they participate in the coverage
  # identity asserted by spec_trace_sites_test:
  #
  #   prose_actions == observed_actions + keys(spec_trace_excluded)
  #
  # Both directions matter. An action in neither is unobserved with nobody
  # noticing, and an entry here that gains a code site is a stale exclusion
  # claiming the action is simultaneously observed and deliberately unobserved.
  spec_trace_excluded: %{
    reap:
      "no code site: the CP has no reap path, only the sweeper's eviction. " <>
        "Modeled in adoption.tla for completeness of the state space.",

    # The three below are unobservable in principle, not merely unimplemented,
    # and the distinction matters: no amount of instrumentation fixes them.
    crash_cp:
      "a control plane cannot emit a record of its own death. Observed " <>
        "INDIRECTLY via restart_cp, whose fresh run_id marks the boundary, " <>
        "which is why the checker partitions by run_id at all.",
    crash_node:
      "node-side death. The CP learns of it only as an absence, through " <>
        "age_to_unknown and age_to_down, which are separately observed. There " <>
        "is no moment at which the CP could append a crash_node record.",
    send_status:
      "the NODE's send half of the status exchange. The CP observes only its " <>
        "own receive half, recv_status. Modeled as a distinct action because " <>
        "the gap between send and receive is where the straggler lives.",
    recycle_id:
      "explicitly not a code action. The spec's own prose says so: it models " <>
        "the inexhaustible vm_id supply so a destroyed slot can back a fresh " <>
        "Prime."
  },

  # Emission sites that are NOT one-to-one with a prose action. Kept separate
  # from the exclusions because these are observations we ADD, not spec actions
  # we skip, and folding them together would make the coverage identity lie in
  # the opposite direction.
  spec_trace_site_notes: %{
    checkpoint:
      "NOT a spec action: the string Checkpoint does not appear in " <>
        "adoption.tla at all. It is a periodic state observation invented for " <>
        "the trace, emitted every sweep even when nothing else fires, because " <>
        "absence of progress is invisible in an event stream and a wedge is " <>
        "precisely the CP failing to act.",
    begin_destroy:
      "a REFINEMENT of Succeed(t), not an action of its own. adoption.tla has " <>
        "no BeginDestroy action: Succeed both completes the task and appends " <>
        "the durable destroying intent in one step. The code splits that into " <>
        "two observable moments, so one spec action maps to two records. That " <>
        "is a legitimate many-to-one refinement, and writing it down is what " <>
        "stops the coverage check reading begin_destroy as an unmodeled " <>
        "invention."
  },

  # Runtime scope decisions, distinct from both of the above: not a prose action
  # and not an emission site, but a class of RECORD the invariants deliberately
  # skip. Recorded so the filtering is a stated decision rather than a quirk of
  # checker code.
  spec_trace_scope_notes: %{
    # #4814. Not a missing emission: an event outside this spec's universe.
    snapshot_only_destroy:
      "session_destroyed on a banked or parked session. adoption.tla's " <>
        "`destroying` and `cpDestroyed` are sets of VMs, and a banked session " <>
        "holds none (evict_snapshot reclaims a snapshot bundle), so such a " <>
        "destroy cannot violate NoDestroyBeforeConfirm or " <>
        "DestroyIntentPrecedesRecord. Detected at runtime via the derived " <>
        "`had_vm` var, which both destroy invariants filter on, with the " <>
        "excluded count surfaced in the verdict detail so the exclusion is " <>
        "visible rather than silent. Modeling snapshot eviction properly " <>
        "belongs to bank_relight.tla, not here: an action touching none of " <>
        "adoption's variables would be noise in it. See #4809, #4810.",

    # #4812. Emitted and registered above, but read by no invariant yet. Recorded
    # so groundwork is distinguishable from an oversight, which is the
    # declared-but-unwired class this repo keeps rediscovering.
    unread_by_any_invariant:
      "succeed and abandon_claim are emitted and stored but no invariant reads " <>
        "them today. adoption.tla models Succeed(t) and AbandonClaim(t), so the " <>
        "records are deliberate groundwork for the task-lifecycle invariants, " <>
        "not a spec action nobody wired up."
  }
}
