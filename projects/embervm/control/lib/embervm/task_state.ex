defmodule Embervm.TaskState do
  @moduledoc """
  The task lifecycle FSM, expressed as data: a module-attribute map from
  `{state, event}` to the next state. `Embervm.TaskStore` is the only caller
  that mutates task state, and every mutation goes through `transition/2` or
  `transition!/2` first, so illegal transitions fail loudly at the call site
  instead of corrupting ETS or the op-log with a state the FSM never sanctions.

  Keeping this a lookup table (not `case`/`cond` chains) means the legal-move
  set is inspectable and testable as data: `states/0`, `events/0`, and the
  transition table itself are the single source of truth the exhaustive test
  in `task_state_test.exs` walks the full cartesian product against.
  """

  defmodule IllegalTransition do
    defexception [:state, :event]

    @impl true
    def message(%{state: state, event: event}) do
      "illegal transition: #{inspect(state)} -/-> on #{inspect(event)}"
    end
  end

  @states [
    :queued,
    :assigned,
    :running,
    :succeeded,
    :failed_retryable,
    :failed_permanent,
    :dead_lettered
  ]

  @terminal_states [:succeeded, :failed_permanent, :dead_lettered]

  @events [
    :assign,
    :start,
    :succeed,
    :fail_retryable,
    :fail_permanent,
    :retry,
    :dead_letter,
    :redrive,
    :expire
  ]

  # The transition table. Every legal (state, event) pair the control plane
  # allows; anything not a key here is illegal by construction.
  #
  # `:redrive` is the DLQ escape hatch (Task 8): a dead-lettered task is
  # terminal to the automatic pipeline, but an operator (or the redrive API)
  # can move it back to `:queued` for another run. It is the ONLY event that
  # leaves a terminal state, which is why it is enumerated explicitly rather
  # than folded into `:retry` (retry increments the attempt counter and only
  # applies to `:failed_retryable`; redrive RESETS the counter, see TaskStore).
  #
  # `:expire` (ADR embervm/002) moves a `:queued` task whose `expires_at` has
  # passed straight to `:failed_permanent`: the dispatcher fires it when it pops a
  # task past its TTL, so an over-budget parked task never dispatches after its
  # deadline (closing the D12 queued-task-TTL gap). Only `:queued` needs it (only
  # queued tasks are popped for dispatch); it does NOT dead-letter (expiry is not a
  # processing failure).
  @transitions %{
    {:queued, :assign} => :assigned,
    {:queued, :expire} => :failed_permanent,
    {:assigned, :start} => :running,
    {:assigned, :fail_retryable} => :failed_retryable,
    {:assigned, :fail_permanent} => :failed_permanent,
    {:running, :succeed} => :succeeded,
    {:running, :fail_retryable} => :failed_retryable,
    {:running, :fail_permanent} => :failed_permanent,
    {:failed_retryable, :retry} => :queued,
    {:failed_permanent, :dead_letter} => :dead_lettered,
    {:dead_lettered, :redrive} => :queued
  }

  @type state ::
          :queued
          | :assigned
          | :running
          | :succeeded
          | :failed_retryable
          | :failed_permanent
          | :dead_lettered

  @type event ::
          :assign
          | :start
          | :succeed
          | :fail_retryable
          | :fail_permanent
          | :retry
          | :dead_letter
          | :redrive
          | :expire

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

  # The lifecycle-forward rank: a monotone partial order over the states along the
  # normal progression queued -> assigned -> running -> {terminal or the retry
  # branch}. It exists so the op-log projection can apply a DEFERRED, possibly
  # out-of-order async lifecycle append (:assigned/:started under
  # EMBERVM_ASYNC_LIFECYCLE_WRITES, ADR embervm/014 decision 2) MONOTONICALLY: an
  # append that targets a state advances the durable projection only from a
  # strictly-lower-ranked state, so a stale late-arriving :assigned cannot regress a
  # row that already ran or terminalized, while a legitimate forward jump (queued ->
  # running when the :assigned append was lost) still applies. failed_retryable and
  # every terminal state rank ABOVE running so neither :assigned (rank 1) nor
  # :started (rank 2) can ever pull them backward; a genuine re-run re-enters at
  # queued (rank 0) via :retry/:redrive and is advanced by fresh appends. This is
  # the single source of truth for that order; the SQLite/Postgres projections
  # derive their guard predicate from it (see states_below/1).
  @forward_rank %{
    queued: 0,
    assigned: 1,
    running: 2,
    failed_retryable: 3,
    succeeded: 3,
    failed_permanent: 3,
    dead_lettered: 3
  }

  @spec forward_rank(state()) :: non_neg_integer()
  def forward_rank(state), do: Map.fetch!(@forward_rank, state)

  @doc """
  The set of state NAMES (as strings, matching the durable `tasks.state` column)
  ranked strictly below `target`. A deferred async append that moves the projection
  to `target` may only do so from one of these, so the projection stays monotone
  under out-of-order/replayed appends. `target` is an atom state.
  """
  @spec states_below(state()) :: [String.t()]
  def states_below(target) do
    target_rank = forward_rank(target)

    for {state, rank} <- @forward_rank, rank < target_rank, do: Atom.to_string(state)
  end
end
