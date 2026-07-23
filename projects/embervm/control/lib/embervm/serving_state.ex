defmodule Embervm.ServingState do
  @moduledoc """
  The serving-instance lifecycle FSM, expressed as data, mirroring
  `Embervm.SessionState`: a module-attribute map from `{state, event}` to the
  next state. `Embervm.ServingStore` is the only caller that mutates serving
  state, and every mutation goes through `transition/2` or `transition!/2`
  first, so an illegal transition fails loudly at the call site instead of
  corrupting ETS or the op-log with a state the FSM never sanctions.

  ## the lifecycle

      starting  -> published                (endpoint installed in the fan-out)
      published -> draining                  (endpoint pulled from the fan-out)
      draining  -> banking -> banked         (idle bank, Task 9)
      banked    -> relighting -> starting    (relight-on-miss, Task 8 activator)
      published -> published                 (health republish is a no-op edge)

  ## naming: `published` (NOT the plan's `serving`), and no `expired` terminal

  This is a deliberate alignment to the SHIPPED contract, not a divergence from
  the plan. The R3 plan's FSM sketch names the live-and-in-fan-out state `serving`
  and lists `expired` among the terminals. The MERGED PR-1 `serving_instances`
  projection (`Embervm.OpLog.SQLite`) is authoritative over that illustrative
  sketch, and it writes exactly these seven state strings and no others:

    * `starting`  (serving_started AND serving_relit)
    * `published` (serving_published)     <- the plan's "serving"
    * `draining`  (serving_unpublished)
    * `banked`    (serving_banked)
    * `evicted` / `destroyed` / `failed`  (terminate_serving)

  So this FSM's states ARE those seven strings verbatim. Naming the live state
  `serving` (or adding an `expired` terminal with no `serving_expired` op behind
  it) would force a translation layer in `ServingStore`'s rebuild and break its
  totality: `state_from_string/1` must be total over precisely the projection's
  output for a projection-rebuild-then-publish to be byte-identical to the
  pre-restart snapshot (the property the EndpointPublisher's correctness rests
  on). Lifetime expiry is therefore NOT a distinct state here; it lands as
  `destroyed` (via `serving_destroyed`, driven by Task 9's sweeper, out of this
  PR). `banking` and `relighting` are the only states NOT in the projection, and
  only because they are transient ETS-only markers never persisted (see below).
  A future reviewer seeing `published`-not-`serving` should read it as
  contract-conformance, not a plan gap.

  ## transient (ETS-only) states

  `banking` and `relighting` are the serving counterparts of the session FSM's
  same-named states: entered by `ServingStore.mark/2` with NO op-log append
  (the durable log records only COMPLETED lifecycle transitions, standing
  decision), and healed from node inventory by adoption if a crash strands them.
  The paired durable completion op (`serving_banked` / `serving_relit`) is what
  actually persists the outcome; until it lands these are ETS-only markers a
  later op or an adoption reconcile resolves. Their `*_abort` edges are the
  ETS-only recovery when the daemon RPC fails but the VM/snapshot is intact (the
  bank did not write a snapshot, or a transient relight failure left the snapshot
  restorable), returning the instance to the pre-operation state so a later
  attempt retries.

  Terminal states each carry a recorded reason (the op payload's `reason`) and
  are reachable from MOST live states, because a serving instance can be
  destroyed, evicted, or failed at almost any point:

    * `destroyed`: reachable from every non-terminal state (a delete, a drain to
      DESTROY, or lifetime expiry can destroy an instance while it is starting,
      published, draining, banking, banked, or relighting).
    * `failed`: reachable from the live states that touch the daemon (starting,
      published, draining, banking, relighting) and reflects a StartServing/
      StopServing transport error, a readiness timeout, an unrestorable
      snapshot, or repeated bank failure.
    * `evicted`: reachable ONLY from `banked` (only a banked instance holds an
      evictable serving snapshot), the banked-TTL GC / disk-pressure path.
  """

  defmodule IllegalTransition do
    defexception [:state, :event]

    @impl true
    def message(%{state: state, event: event}) do
      "illegal serving transition: #{inspect(state)} -/-> on #{inspect(event)}"
    end
  end

  @states [
    :starting,
    :published,
    :draining,
    :banking,
    :banked,
    :relighting,
    :destroying,
    :evicted,
    :destroyed,
    :failed
  ]

  @terminal_states [:evicted, :destroyed, :failed]

  @events [
    :publish,
    :unpublish,
    :bank,
    :bank_ready,
    :bank_abort,
    :relight,
    :relight_ready,
    :relight_abort,
    :evict,
    :begin_destroy,
    :destroy,
    :fail
  ]

  # The transition table. Every legal (state, event) pair; anything not a key
  # here is illegal by construction.
  @transitions %{
    # Publish: a started (or relit) instance whose endpoint is now installed in
    # the fan-out. A health-driven republish of an already-published instance is
    # a legal self-edge (publisher re-emits the same fact; no state churn).
    {:starting, :publish} => :published,
    {:draining, :publish} => :published,
    {:published, :publish} => :published,
    # Unpublish: the endpoint is pulled from the fan-out (health ejection, or the
    # first step of a drain-then-bank). The VM is not necessarily gone yet.
    {:published, :unpublish} => :draining,
    # Idle-bank: draining -> banking (StopServing BANK in flight) -> banked. The
    # bank_abort edge (banking -> draining) is the ETS-only recovery when the
    # bank RPC fails: no snapshot was written, so the instance returns to
    # draining (a later attempt re-banks, or it is republished). banking is a
    # transient, crash-healed-from-node state.
    {:draining, :bank} => :banking,
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :draining,
    # Relight-on-miss: banked -> relighting (StartServing relight in flight) ->
    # starting (then a paired publish moves it to published). The relight_abort
    # edge (relighting -> banked) is the ETS-only recovery when a transient
    # (non-precondition) relight failure leaves the snapshot intact: the instance
    # returns to banked and a later miss re-relights.
    {:banked, :relight} => :relighting,
    {:relighting, :relight_ready} => :starting,
    {:relighting, :relight_abort} => :banked,
    # Banked-TTL GC / disk-pressure eviction: only a banked instance holds an
    # evictable serving snapshot.
    {:banked, :evict} => :evicted,
    # Destroy: from every non-terminal state. The direct edge is today's behaviour;
    # the begin_destroy -> destroying -> destroy path is the node-confirmed shape
    # (ADR embervm/014 decision 5), gated by EMBERVM_NODE_CONFIRMED_DESTROY. Serving
    # destruction is sweeper-driven today, so the node-confirmed manager wiring is a
    # follow-up; the state + edges exist so a destroying row rebuilds and the FSM is
    # coherent across all stores.
    {:starting, :destroy} => :destroyed,
    {:published, :destroy} => :destroyed,
    {:draining, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    {:starting, :begin_destroy} => :destroying,
    {:published, :begin_destroy} => :destroying,
    {:draining, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    # Fail (daemon transport/timeout, readiness timeout, unrestorable snapshot,
    # repeated bank failure): from every live state that touches the daemon.
    {:starting, :fail} => :failed,
    {:published, :fail} => :failed,
    {:draining, :fail} => :failed,
    {:banking, :fail} => :failed,
    {:relighting, :fail} => :failed
  }

  @type state ::
          :starting
          | :published
          | :draining
          | :banking
          | :banked
          | :relighting
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
          | :relight
          | :relight_ready
          | :relight_abort
          | :evict
          | :begin_destroy
          | :destroy
          | :fail

  @spec states() :: [state()]
  def states, do: @states

  @spec events() :: [event()]
  def events, do: @events

  @spec terminal_states() :: [state()]
  def terminal_states, do: @terminal_states

  @spec terminal?(state()) :: boolean()
  def terminal?(state), do: state in @terminal_states

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
  (callers only ask for terminal kinds), mirroring `SessionState.terminal_op_kind/1`.
  """
  @spec terminal_op_kind(state()) :: atom()
  def terminal_op_kind(:evicted), do: :serving_evicted
  def terminal_op_kind(:destroyed), do: :serving_destroyed
  def terminal_op_kind(:failed), do: :serving_failed
end
