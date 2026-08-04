defmodule Embervm.SessionStateTest do
  @moduledoc """
  Exhaustively walks the (state, event) cartesian product against the session
  FSM's transition table: every legal pair returns the documented next state, and
  every illegal pair is rejected the same way (never silently accepted, never a
  different error shape). Mirrors Embervm.TaskStateTest; the totality of the
  illegal-transition rejection is what a bank/relight/destroy path relies on to
  fail loudly instead of corrupting state.
  """
  use ExUnit.Case, async: true

  alias Embervm.SessionState

  @legal %{
    {:creating, :create_ready} => :running,
    {:running, :bank} => :banking,
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :running,
    {:banked, :relight} => :relighting,
    {:parked, :relight} => :relighting,
    {:parked, :expire} => :expired,
    {:parked, :destroy} => :destroyed,
    {:relighting, :relight_ready} => :running,
    {:relighting, :relight_abort} => :banked,
    {:relighting, :parked_abort} => :parked,
    {:running, :park} => :parking,
    {:parking, :park_complete} => :parked,
    {:parking, :expire} => :expired,
    {:relighting, :rejoin_ready} => :running,
    {:running, :expire} => :expired,
    {:banked, :expire} => :expired,
    {:banked, :evict} => :evicted,
    {:parked, :evict} => :evicted,
    {:creating, :destroy} => :destroyed,
    {:running, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    # Node-confirmed destroy (ADR embervm/014 decision 5): begin_destroy from every
    # non-terminal state records the destroying intent, then destroying -> destroy.
    {:creating, :begin_destroy} => :destroying,
    {:running, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    {:creating, :fail} => :failed,
    {:running, :fail} => :failed,
    {:banking, :fail} => :failed,
    {:relighting, :fail} => :failed
  }

  test "exhaustive transition table: every (state, event) pair matches the documented outcome" do
    assert map_size(@legal) == 34
    assert length(SessionState.events()) == 16
    assert length(SessionState.states()) == 12

    for state <- SessionState.states(), event <- SessionState.events() do
      case Map.fetch(@legal, {state, event}) do
        {:ok, expected_next} ->
          assert SessionState.transition(state, event) == {:ok, expected_next},
                 "expected #{inspect({state, event})} -> {:ok, #{inspect(expected_next)}}"

          assert SessionState.transition!(state, event) == expected_next

        :error ->
          assert SessionState.transition(state, event) ==
                   {:error, {:illegal_transition, state, event}},
                 "expected #{inspect({state, event})} to be illegal"

          assert_raise SessionState.IllegalTransition, fn ->
            SessionState.transition!(state, event)
          end
      end
    end
  end

  test "parked transitions to relighting, expired, destroyed, and evicted" do
    assert SessionState.transition(:parked, :relight) == {:ok, :relighting}
    assert SessionState.transition(:parked, :expire) == {:ok, :expired}
    assert SessionState.transition(:parked, :destroy) == {:ok, :destroyed}
    assert SessionState.transition(:parked, :evict) == {:ok, :evicted}
  end

  test "only running can park" do
    assert SessionState.transition(:running, :park) == {:ok, :parking}
    assert SessionState.transition(:parking, :park_complete) == {:ok, :parked}
    assert SessionState.transition(:parking, :expire) == {:ok, :expired}
    assert SessionState.transition(:banked, :park) == {:error, {:illegal_transition, :banked, :park}}
    assert SessionState.transition(:relighting, :park) == {:error, {:illegal_transition, :relighting, :park}}
    assert SessionState.transition(:banked, :park) == {:error, {:illegal_transition, :banked, :park}}
  end

  test "parked_abort recovers a failed parked relight" do
    assert SessionState.transition(:relighting, :parked_abort) == {:ok, :parked}
  end

  test "parked is not terminal and does not accept terminal operations" do
    refute SessionState.terminal?(:parked)
    refute :parked in SessionState.terminal_states()
    assert_raise FunctionClauseError, fn -> SessionState.terminal_op_kind(:parked) end
  end

  test "no event leaves a terminal state (terminals are absorbing)" do
    for state <- SessionState.terminal_states(), event <- SessionState.events() do
      assert SessionState.transition(state, event) == {:error, {:illegal_transition, state, event}}
    end
  end

  test "terminal?/1 is true only for the four terminal states" do
    expected = MapSet.new([:expired, :evicted, :destroyed, :failed])

    for state <- SessionState.states() do
      assert SessionState.terminal?(state) == MapSet.member?(expected, state)
    end

    assert MapSet.new(SessionState.terminal_states()) == expected
  end

  test "terminal_op_kind/1 maps each terminal state to its op kind" do
    assert SessionState.terminal_op_kind(:expired) == :session_expired
    assert SessionState.terminal_op_kind(:evicted) == :session_evicted
    assert SessionState.terminal_op_kind(:destroyed) == :session_destroyed
    assert SessionState.terminal_op_kind(:failed) == :session_failed
  end
end
