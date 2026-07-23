defmodule Embervm.ServingStateTest do
  @moduledoc """
  Exhaustive FSM coverage for Embervm.ServingState, mirroring the session-state
  test: every legal (state, event) pair is asserted to land on the expected next
  state, and every pair NOT in the table is asserted illegal (both the `{:error,
  ...}` tuple form and the raising `transition!/2` form). This is the guard that
  keeps the transition table and the code that drives it in lockstep, and that the
  state STRINGS stay exactly the merged projection's set.
  """
  use ExUnit.Case, async: true

  alias Embervm.ServingState
  alias Embervm.ServingState.IllegalTransition

  # The complete legal table, duplicated here as the test's independent source of
  # truth (so a typo in the module's @transitions is caught, not mirrored).
  @legal %{
    {:starting, :publish} => :published,
    {:draining, :publish} => :published,
    {:published, :publish} => :published,
    {:published, :unpublish} => :draining,
    {:draining, :bank} => :banking,
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :draining,
    {:banked, :relight} => :relighting,
    {:relighting, :relight_ready} => :starting,
    {:relighting, :relight_abort} => :banked,
    {:banked, :evict} => :evicted,
    {:starting, :destroy} => :destroyed,
    {:published, :destroy} => :destroyed,
    {:draining, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    # Node-confirmed destroy (ADR embervm/014 decision 5): begin_destroy from every
    # non-terminal state, then destroying -> destroy.
    {:starting, :begin_destroy} => :destroying,
    {:published, :begin_destroy} => :destroying,
    {:draining, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    {:starting, :fail} => :failed,
    {:published, :fail} => :failed,
    {:draining, :fail} => :failed,
    {:banking, :fail} => :failed,
    {:relighting, :fail} => :failed
  }

  test "states and events are the expected closed sets" do
    assert Enum.sort(ServingState.states()) ==
             Enum.sort([
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
             ])

    assert Enum.sort(ServingState.events()) ==
             Enum.sort([
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
             ])

    assert Enum.sort(ServingState.terminal_states()) == Enum.sort([:evicted, :destroyed, :failed])
  end

  test "every legal (state, event) transitions to the expected next state" do
    for {{state, event}, expected} <- @legal do
      assert ServingState.transition(state, event) == {:ok, expected}
      assert ServingState.transition!(state, event) == expected
    end
  end

  test "every (state, event) pair NOT in the legal table is illegal" do
    for state <- ServingState.states(), event <- ServingState.events() do
      unless Map.has_key?(@legal, {state, event}) do
        assert ServingState.transition(state, event) == {:error, {:illegal_transition, state, event}}

        assert_raise IllegalTransition, fn -> ServingState.transition!(state, event) end
      end
    end
  end

  test "terminal states are terminal and non-terminal are not" do
    for state <- [:evicted, :destroyed, :failed], do: assert(ServingState.terminal?(state))

    for state <- [:starting, :published, :draining, :banking, :banked, :relighting, :destroying],
        do: refute(ServingState.terminal?(state))
  end

  test "terminal_op_kind maps each terminal state to its op kind" do
    assert ServingState.terminal_op_kind(:evicted) == :serving_evicted
    assert ServingState.terminal_op_kind(:destroyed) == :serving_destroyed
    assert ServingState.terminal_op_kind(:failed) == :serving_failed
  end

  test "no terminal state has any outgoing edge (a serving instance dies terminally)" do
    for state <- ServingState.terminal_states(), event <- ServingState.events() do
      assert ServingState.transition(state, event) == {:error, {:illegal_transition, state, event}}
    end
  end

  test "the publish self-edge on published is legal (health republish is idempotent)" do
    assert ServingState.transition(:published, :publish) == {:ok, :published}
  end
end
