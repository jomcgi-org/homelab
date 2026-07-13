defmodule Embervm.TaskStateTest do
  @moduledoc """
  Exhaustively walks the (state, event) cartesian product against the FSM's
  transition table: every legal pair must return the documented next state,
  and every illegal pair must be rejected the same way (never silently
  accepted, never a different error shape).
  """
  use ExUnit.Case, async: true

  alias Embervm.TaskState

  @legal %{
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

  test "exhaustive transition table: every (state, event) pair matches the documented outcome" do
    assert map_size(@legal) == 9

    for state <- TaskState.states(), event <- TaskState.events() do
      case Map.fetch(@legal, {state, event}) do
        {:ok, expected_next} ->
          assert TaskState.transition(state, event) == {:ok, expected_next},
                 "expected #{inspect({state, event})} -> {:ok, #{inspect(expected_next)}}"

          assert TaskState.transition!(state, event) == expected_next

        :error ->
          assert TaskState.transition(state, event) == {:error, {:illegal_transition, state, event}},
                 "expected #{inspect({state, event})} to be illegal"

          assert_raise TaskState.IllegalTransition, fn ->
            TaskState.transition!(state, event)
          end
      end
    end
  end

  test "terminal?/1 is true only for succeeded, failed_permanent, dead_lettered" do
    expected_terminal = MapSet.new([:succeeded, :failed_permanent, :dead_lettered])

    for state <- TaskState.states() do
      assert TaskState.terminal?(state) == MapSet.member?(expected_terminal, state)
    end

    assert MapSet.new(TaskState.terminal_states()) == expected_terminal
  end
end
