defmodule Embervm.StatefulState do
  @moduledoc """
  The stateful-instance lifecycle FSM (R4), expressed as data, mirroring
  `Embervm.ServingState`: a module-attribute map from `{state, event}` to the
  next state. `Embervm.StatefulStore` is the only caller that mutates stateful
  state, and every mutation goes through `transition/2` or `transition!/2`
  first, so an illegal transition fails loudly at the call site instead of
  corrupting ETS or the op-log with a state the FSM never sanctions.

  ## the class: a SINGLETON, L4 stateful sandbox

  A stateful workload is a singleton by construction (decision 3, the catalog
  rejects spec.concurrency): exactly one live VM at a time, backed by a durable
  per-workload volume plus a banked warmth snapshot. The lifecycle is the serving
  lifecycle with two structural differences: (1) `banked` is reached from
  `serving` via a transient `banking` (there is no long-lived `draining` state,
  the endpoint is pulled the instant a bank begins), and (2) a banked instance
  can wake either WARM (relight, resuming the snapshot) or COLD (`cold_boot`, when
  the snapshot/volume pair is broken and warmth must be discarded).

  ## the lifecycle

      starting     -> serving                    (L4 endpoint installed in the fan-out)
      serving      -> banking -> banked           (idle bank / unpublish, Task 8+)
      banked       -> relighting -> starting      (WARM wake, snapshot resumed)
      banked       -> cold_booting -> starting    (COLD wake, pair broken, warmth discarded)
      banked       -> evicted                     (pair broken / banked-TTL GC)

  ## naming: the projection strings are authoritative

  Exactly as `ServingState` aligns to the shipped `serving_instances`
  projection, this FSM's persisted states ARE the strings the MERGED PR-1
  `stateful_instances` projection (`Embervm.OpLog.SQLite`) writes and no others:

    * `starting`  (stateful_started, stateful_relit, stateful_cold_booted)
    * `serving`   (stateful_published)     <- the live-and-in-fan-out state
    * `banked`    (stateful_banked)
    * `evicted` / `destroyed` / `failed`  (the terminal kinds)

  `state_from_string/1` in `StatefulStore` must be total over precisely that set
  for a projection-rebuild-then-publish to be byte-identical to the pre-restart
  snapshot (the property the EndpointPublisher's correctness rests on). The states
  NOT in the projection are `banking`, `checkpointed`, `relighting`, and
  `cold_booting`: all are transient, ETS-only markers never persisted (see below).

  ## transient (ETS-only) states

  `banking`, `checkpointed`, `relighting`, and `cold_booting` are the stateful
  counterparts of the serving FSM's `banking`/`relighting`: entered by
  `StatefulStore.mark/2`
  with NO op-log append (the durable log records only COMPLETED lifecycle
  transitions, standing decision), and healed from node inventory by adoption if
  a crash strands them. The paired durable completion op (`stateful_banked`,
  `stateful_relit`, `stateful_cold_booted`) is what actually persists the
  outcome; until it lands these are ETS-only markers a later op or an adoption
  reconcile resolves. Their `*_abort` edges are the ETS-only recovery when the
  daemon RPC fails but the VM/snapshot is intact (the bank did not write a
  snapshot, or a transient relight failure left the snapshot restorable),
  returning the instance to the pre-operation state so a later attempt retries.

  ## the two wake paths off `banked`

  `relight` (WARM) resumes the banked snapshot in place; `cold_boot` (COLD)
  discards the snapshot and boots fresh on the current volume. The activator (a
  later task) chooses between them on the pairing check
  (`snapshot_generation == volume.generation`): a valid pair relights, a broken
  pair cold-boots (and evicts the stale bundle via a paired `stateful_evicted`).
  Both wake edges land back on `starting` (not yet republished); a paired publish
  then moves the instance to `serving`. `cold_ready` carries a machine-readable
  reason (pair_broken cause) in its op payload so the discarded-warmth event is
  fully reconstructable from the op alone.

  Terminal states each carry a recorded reason (the op payload's `reason`):

    * `destroyed`: reachable from every non-terminal state (a delete or lifetime
      expiry can destroy an instance at any point in its life).
    * `failed`: reachable from every LIVE state (a StartStateful / StopStateful
      transport error, a readiness timeout, an unrestorable snapshot, or repeated
      bank failure fails the instance from wherever it was).
    * `evicted`: reachable ONLY from `banked` (only a banked instance holds an
      evictable snapshot bundle). Its reason is `pair_broken` (the volume
      generation moved out from under the bundle) or `ttl` (the banked-TTL GC).
  """

  defmodule IllegalTransition do
    defexception [:state, :event]

    @impl true
    def message(%{state: state, event: event}) do
      "illegal stateful transition: #{inspect(state)} -/-> on #{inspect(event)}"
    end
  end

  @states [
    :starting,
    :serving,
    :banking,
    :checkpointed,
    :banked,
    :relighting,
    :cold_booting,
    :destroying,
    :evicted,
    :destroyed,
    :failed
  ]

  @terminal_states [:evicted, :destroyed, :failed]

  # The states that hold a LIVE stateful VM (not a banked snapshot, not a terminal
  # row). A live instance is the singleton the class allows exactly one of.
  # `banked` is deliberately NOT live: it holds a snapshot + volume, no VM.
  # `checkpointed` (ADR embervm/008) IS live: the interruptible-bank checkpoint
  # leaves the VM PAUSED (not destroyed), still holding the volume attach, awaiting
  # a resolve, so the singleton guard must count it.
  # `destroying` (ADR embervm/014 decision 5) also holds a live VM: the
  # node-confirmed teardown RPC is in flight, so the singleton guard must count it
  # until the node confirms teardown and the terminal destroyed op fires.
  @live_states [
    :starting,
    :serving,
    :banking,
    :checkpointed,
    :relighting,
    :cold_booting,
    :destroying
  ]

  @events [
    :publish,
    :unpublish,
    :bank,
    :bank_ready,
    :bank_abort,
    :checkpoint_ready,
    :commit,
    :abort,
    :relight,
    :relight_ready,
    :relight_abort,
    :cold_boot,
    :cold_ready,
    :cold_abort,
    :evict,
    :begin_destroy,
    :destroy,
    :fail
  ]

  # The transition table. Every legal (state, event) pair; anything not a key
  # here is illegal by construction.
  @transitions %{
    # Publish: a started (relit, or cold-booted) instance whose L4 endpoint is now
    # installed in the fan-out, moving it to `serving`.
    {:starting, :publish} => :serving,
    # Unpublish: the endpoint is pulled from the fan-out and the bank sequence
    # begins. Transient (ETS-only): a serving instance being banked (or health-
    # ejected) leaves the fan-out and enters `banking` in one ETS-only step. The VM
    # is not gone yet; `bank_ready` completes the durable bank. `bank` is the same
    # edge under a different caller name (an explicit bank vs an unpublish-then-bank).
    {:serving, :unpublish} => :banking,
    {:serving, :bank} => :banking,
    # Idle-bank completion: banking -> banked (StopStateful BANK wrote the snapshot).
    # The bank_abort edge (banking -> serving) is the ETS-only recovery when the
    # bank RPC fails: no snapshot was written, so the instance returns to serving (a
    # later attempt re-banks, or it is republished). banking is transient, crash-
    # healed-from-node.
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :serving,
    # Interruptible bank (ADR embervm/008, opt-in): the CHECKPOINT completes
    # (banking -> checkpointed), leaving the VM PAUSED awaiting a control-plane
    # resolve. checkpointed is transient/ETS-only (like banking), healed from node
    # inventory by adoption (noded reports checkpoint_pending). The resolve forks:
    # `commit` publishes the temp as the bundle and destroys (checkpointed ->
    # banked), `abort` bumps the generation, resumes the SAME paused VM, and
    # republishes (checkpointed -> serving, hot, no relight). A CHECKPOINT *RPC*
    # failure reuses the existing bank_abort edge (banking -> serving): noded left
    # the VM live, so nothing entered checkpointed.
    {:banking, :checkpoint_ready} => :checkpointed,
    {:checkpointed, :commit} => :banked,
    {:checkpointed, :abort} => :serving,
    # WARM wake: banked -> relighting (StartStateful relight in flight) -> starting
    # (then a paired publish moves it to serving). The relight_abort edge
    # (relighting -> banked) is the ETS-only recovery when a transient (non-
    # precondition) relight failure leaves the snapshot intact: the instance returns
    # to banked and a later miss re-relights (or cold-boots).
    {:banked, :relight} => :relighting,
    {:relighting, :relight_ready} => :starting,
    {:relighting, :relight_abort} => :banked,
    # COLD wake: banked -> cold_booting (the pair was broken, warmth discarded, a
    # fresh boot on the current volume) -> starting. cold_ready carries the reason
    # (pair_broken cause) in the op payload. Like relight, its abort edge
    # (cold_booting -> banked) is the ETS-only recovery when the cold boot RPC fails
    # transiently before the fresh VM exists (the banked bundle is still on disk, so
    # a later attempt retries; the eviction of that bundle only lands with a
    # completed cold_ready + paired stateful_evicted).
    {:banked, :cold_boot} => :cold_booting,
    {:cold_booting, :cold_ready} => :starting,
    {:cold_booting, :cold_abort} => :banked,
    # Banked-TTL GC / broken-pair eviction: only a banked instance holds an
    # evictable snapshot bundle. reason is pair_broken or ttl.
    {:banked, :evict} => :evicted,
    # Destroy: from every non-terminal state. The direct edge is today's behaviour;
    # the begin_destroy -> destroying -> destroy path is the node-confirmed shape
    # (ADR embervm/014 decision 5), gated by EMBERVM_NODE_CONFIRMED_DESTROY.
    {:starting, :destroy} => :destroyed,
    {:serving, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:checkpointed, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    {:cold_booting, :destroy} => :destroyed,
    {:starting, :begin_destroy} => :destroying,
    {:serving, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:checkpointed, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:cold_booting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    # Fail (daemon transport/timeout, readiness timeout, unrestorable snapshot,
    # repeated bank failure): from every LIVE state that touches the daemon. banked
    # cannot `fail` (it holds no VM); a broken banked bundle `evict`s, it does not
    # fail.
    {:starting, :fail} => :failed,
    {:serving, :fail} => :failed,
    {:banking, :fail} => :failed,
    # A checkpointed instance fails when its resolve RPC errors irrecoverably (a
    # commit whose publish failed, or an abort whose resume failed so noded tore
    # the paused VM down): the VM is gone, so the instance fails from checkpointed.
    {:checkpointed, :fail} => :failed,
    {:relighting, :fail} => :failed,
    {:cold_booting, :fail} => :failed
  }

  @type state ::
          :starting
          | :serving
          | :banking
          | :checkpointed
          | :banked
          | :relighting
          | :cold_booting
          | :destroying
          | :evicted
          | :destroyed
          | :failed

  @type event ::
          :publish
          | :unpublish
          | :bank
          | :bank_ready
          | :bank_abort
          | :checkpoint_ready
          | :commit
          | :abort
          | :relight
          | :relight_ready
          | :relight_abort
          | :cold_boot
          | :cold_ready
          | :cold_abort
          | :evict
          | :begin_destroy
          | :destroy
          | :fail

  @spec states() :: [state()]
  def states, do: @states

  @spec events() :: [event()]
  def events, do: @events

  @spec live_states() :: [state()]
  def live_states, do: @live_states

  @spec terminal_states() :: [state()]
  def terminal_states, do: @terminal_states

  @spec terminal?(state()) :: boolean()
  def terminal?(state), do: state in @terminal_states

  @doc """
  Whether `state` holds a LIVE stateful VM (the singleton the class permits one
  of). True for the five non-terminal, non-banked states; false for `banked`
  (holds a snapshot, not a VM) and every terminal state. The store's singleton
  invariant (`start/2` refuses a second live instance) reads this predicate.
  """
  @spec live?(state()) :: boolean()
  def live?(state), do: state in @live_states

  @spec transition(state(), event()) ::
          {:ok, state()} | {:error, {:illegal_transition, state(), event()}}
  def transition(state, event) do
    case Map.fetch(@transitions, {state, event}) do
      {:ok, next} -> {:ok, next}
      :error -> {:error, {:illegal_transition, state, event}}
    end
  end

  @spec transition!(state(), event()) :: state()
  def transition!(state, event) do
    case transition(state, event) do
      {:ok, next} ->
        next

      {:error, {:illegal_transition, state, event}} ->
        raise IllegalTransition, state: state, event: event
    end
  end

  @doc """
  The op kind that records a given terminal state, for the write-through append.
  A total map over the three terminal states; a non-terminal state raises
  (callers only ask for terminal kinds), mirroring `ServingState.terminal_op_kind/1`.
  """
  @spec terminal_op_kind(state()) :: atom()
  def terminal_op_kind(:evicted), do: :stateful_evicted
  def terminal_op_kind(:destroyed), do: :stateful_destroyed
  def terminal_op_kind(:failed), do: :stateful_failed
end
