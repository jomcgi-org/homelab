defmodule Embervm.SessionState do
  @moduledoc """
  The session lifecycle FSM, expressed as data, mirroring `Embervm.TaskState`: a
  module-attribute map from `{state, event}` to the next state. `Embervm.SessionStore`
  is the only caller that mutates session state, and every mutation goes through
  `transition/2` or `transition!/2` first, so an illegal transition fails loudly
  at the call site instead of corrupting ETS or the op-log with a state the FSM
  never sanctions.

  ## the lifecycle

      creating -> running                  (create claimed a VM and it is live)
      running  -> banking -> banked        (idle bank, PR-4)
      banked   -> relighting -> running    (relight-on-invoke, PR-4)

  Terminal states each carry a recorded reason (the op payload's `reason`), and
  are reachable from MOST live states because a session can be destroyed,
  expired, evicted, or failed at almost any point:

    * `destroyed` (DELETE /v1/sessions/{id}): reachable from every non-terminal
      state (the caller may destroy a session while it is creating, running,
      banking, banked, or relighting).
    * `failed` (a SessionAssign transport/DEADLINE_EXCEEDED error, an
      unrestorable snapshot, or repeated bank failure): reachable from the live
      states that touch the daemon (running, banking, relighting) and from
      creating (a create that could not bring the VM up).
    * `expired` (max-lifetime TTL): reachable from running and banked (a live or
      banked session past `expires_at`).
    * `evicted` (banked-TTL GC or disk-pressure eviction): reachable only from
      `banked` (only a banked session holds an evictable snapshot).

  `bank`/`relight` (the PR-4 idle-bank and relight-on-invoke events) are enumerated
  here so the transition table is COMPLETE for the whole rung even though PR-3 only
  drives `create_ready`/`destroy`/`fail`; leaving the edges undefined would make a
  PR-4 bank raise. The `*_ready`/`*_failed` split mirrors the task FSM's
  assign/start vs fail edges: an operation that reaches the daemon and comes back
  has a distinct success and failure edge, and the failure edge lands on `failed`.
  """

  defmodule IllegalTransition do
    defexception [:state, :event]

    @impl true
    def message(%{state: state, event: event}) do
      "illegal session transition: #{inspect(state)} -/-> on #{inspect(event)}"
    end
  end

  @states [
    :creating,
    :running,
    :banking,
    :parking,
    :banked,
    :parked,
    :relighting,
    :destroying,
    :expired,
    :evicted,
    :destroyed,
    :failed
  ]

  @terminal_states [:expired, :evicted, :destroyed, :failed]

  @events [
    :create_ready,
    :bank,
    :bank_ready,
    :bank_abort,
    :park,
    :park_complete,
    :rejoin_ready,
    :parked_abort,
    :relight,
    :relight_ready,
    :relight_abort,
    :expire,
    :evict,
    :begin_destroy,
    :destroy,
    :fail
  ]

  # The transition table. Every legal (state, event) pair; anything not a key
  # here is illegal by construction.
  @transitions %{
    # Create: a claimed/primed VM is live.
    {:creating, :create_ready} => :running,
    # Idle-bank: running -> banking (bank RPC in flight) -> banked. The bank_abort
    # edge (banking -> running) is the ETS-only recovery when the Bank RPC fails: the
    # VM is still alive, so the session returns to running (no durable op; the failed
    # bank left no snapshot). banking is a transient, crash-healed-from-node state.
    {:running, :bank} => :banking,
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :running,
    # Relight-on-invoke: banked -> relighting (relight RPC in flight) -> running. The
    # relight_abort edge (relighting -> banked) is the ETS-only recovery when a
    # transient (non-precondition) relight failure leaves the snapshot intact: the
    # session returns to banked and a later invoke re-relights.
    {:banked, :relight} => :relighting,
    {:running, :park} => :parking,
    {:parking, :park_complete} => :parked,
    {:parking, :expire} => :expired,
    {:relighting, :rejoin_ready} => :running,
    {:parked, :relight} => :relighting,
    {:parked, :expire} => :expired,
    {:parked, :destroy} => :destroyed,
    {:relighting, :relight_ready} => :running,
    {:relighting, :relight_abort} => :banked,
    {:relighting, :parked_abort} => :parked,
    # Max-lifetime expiry: a live or banked session past its deadline.
    {:running, :expire} => :expired,
    {:banked, :expire} => :expired,
    # Banked-TTL GC / disk-pressure eviction: only a banked session.
    {:banked, :evict} => :evicted,
    # Destroy (DELETE). Two shapes, selected by the EMBERVM_NODE_CONFIRMED_DESTROY
    # gate in the manager:
    #   * gate off (today's behaviour): the direct `:destroy` edge records
    #     destroyed first, then tears down asynchronously.
    #   * gate on (ADR embervm/014 decision 5): `:begin_destroy` first records the
    #     durable `destroying` intent, the node-confirmed teardown RPC runs, and
    #     only a confirmed teardown takes the `:destroying -> :destroy -> destroyed`
    #     edge. A crash mid-destroy rebuilds as `destroying` and re-drives it.
    {:creating, :destroy} => :destroyed,
    {:running, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    {:creating, :begin_destroy} => :destroying,
    {:running, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    # Fail (daemon transport/timeout, unrestorable snapshot, repeated bank failure):
    # from every live state that touches the daemon.
    {:creating, :fail} => :failed,
    {:running, :fail} => :failed,
    {:banking, :fail} => :failed,
    {:relighting, :fail} => :failed
  }

  @type state ::
          :creating
          | :running
          | :banking
          | :parking
          | :banked
          | :parked
          | :relighting
          | :destroying
          | :expired
          | :evicted
          | :destroyed
          | :failed

  @type event ::
          :create_ready
          | :bank
          | :bank_ready
          | :relight
          | :park
          | :park_complete
          | :rejoin_ready
          | :parked_abort
          | :relight_ready
          | :expire
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
  A total map over the four terminal states; a non-terminal state raises (callers
  only ask for terminal kinds).
  """
  @spec terminal_op_kind(state()) :: atom()
  def terminal_op_kind(:expired), do: :session_expired
  def terminal_op_kind(:evicted), do: :session_evicted
  def terminal_op_kind(:destroyed), do: :session_destroyed
  def terminal_op_kind(:failed), do: :session_failed
end
