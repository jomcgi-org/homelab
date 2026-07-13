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

  @events [:assign, :start, :succeed, :fail_retryable, :fail_permanent, :retry, :dead_letter]

  # The transition table. Every legal (state, event) pair the control plane
  # allows; anything not a key here is illegal by construction.
  @transitions %{
    {:queued, :assign} => :assigned,
    {:assigned, :start} => :running,
    {:assigned, :fail_retryable} => :failed_retryable,
    {:assigned, :fail_permanent} => :failed_permanent,
    {:running, :succeed} => :succeeded,
    {:running, :fail_retryable} => :failed_retryable,
    {:running, :fail_permanent} => :failed_permanent,
    {:failed_retryable, :retry} => :queued,
    {:failed_permanent, :dead_letter} => :dead_lettered
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
          :assign | :start | :succeed | :fail_retryable | :fail_permanent | :retry | :dead_letter

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
end
