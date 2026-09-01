defmodule Embervm.StatefulStateTest do
  @moduledoc """
  Exhaustive FSM coverage for Embervm.StatefulState, mirroring the serving-state
  test: every legal (state, event) pair is asserted to land on the expected next
  state, and every pair NOT in the table is asserted illegal (both the `{:error,
  ...}` tuple form and the raising `transition!/2` form). This is the guard that
  keeps the transition table and the code that drives it in lockstep, and that the
  state STRINGS stay exactly the merged projection's set.
  """
  use ExUnit.Case, async: true

  alias Embervm.StatefulState
  alias Embervm.StatefulState.IllegalTransition

  # The complete legal table, duplicated here as the test's independent source of
  # truth (so a typo in the module's @transitions is caught, not mirrored).
  @legal %{
    {:starting, :publish} => :serving,
    {:serving, :unpublish} => :banking,
    {:serving, :bank} => :banking,
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :serving,
    {:banking, :checkpoint_ready} => :checkpointed,
    {:checkpointed, :commit} => :banked,
    {:checkpointed, :abort} => :serving,
    {:checkpointed, :destroy} => :destroyed,
    {:checkpointed, :fail} => :failed,
    {:banked, :relight} => :relighting,
    {:relighting, :relight_ready} => :starting,
    {:relighting, :relight_abort} => :banked,
    {:banked, :cold_boot} => :cold_booting,
    {:cold_booting, :cold_ready} => :starting,
    {:cold_booting, :cold_abort} => :banked,
    {:banked, :evict} => :evicted,
    {:starting, :destroy} => :destroyed,
    {:serving, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    {:cold_booting, :destroy} => :destroyed,
    # Node-confirmed destroy (ADR embervm/014 decision 5): begin_destroy from every
    # non-terminal state (checkpointed included), then destroying -> destroy on
    # confirmation or -> fail after the bounded missing-owner escape.
    {:starting, :begin_destroy} => :destroying,
    {:serving, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:checkpointed, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:cold_booting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    {:destroying, :fail} => :failed,
    {:starting, :fail} => :failed,
    {:serving, :fail} => :failed,
    {:banking, :fail} => :failed,
    {:relighting, :fail} => :failed,
    {:cold_booting, :fail} => :failed
  }

  test "states and events are the expected closed sets" do
    assert Enum.sort(StatefulState.states()) ==
             Enum.sort([
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
             ])

    assert Enum.sort(StatefulState.events()) ==
             Enum.sort([
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
             ])

    assert Enum.sort(StatefulState.terminal_states()) == Enum.sort([:evicted, :destroyed, :failed])
  end

  test "every legal (state, event) transitions to the expected next state" do
    for {{state, event}, expected} <- @legal do
      assert StatefulState.transition(state, event) == {:ok, expected}
      assert StatefulState.transition!(state, event) == expected
    end
  end

  test "every (state, event) pair NOT in the legal table is illegal" do
    for state <- StatefulState.states(), event <- StatefulState.events() do
      unless Map.has_key?(@legal, {state, event}) do
        assert StatefulState.transition(state, event) == {:error, {:illegal_transition, state, event}}

        assert_raise IllegalTransition, fn -> StatefulState.transition!(state, event) end
      end
    end
  end

  test "terminal states are terminal and non-terminal are not" do
    for state <- [:evicted, :destroyed, :failed], do: assert(StatefulState.terminal?(state))

    for state <- [
          :starting,
          :serving,
          :banking,
          :checkpointed,
          :banked,
          :relighting,
          :cold_booting,
          :destroying
        ],
        do: refute(StatefulState.terminal?(state))
  end

  test "live? is true for the live states and false for banked + terminals" do
    # checkpointed IS live (ADR embervm/008: the paused VM still holds the volume);
    # destroying IS live (ADR embervm/014: node-confirmed teardown RPC in flight).
    for state <- [:starting, :serving, :banking, :checkpointed, :relighting, :cold_booting, :destroying],
        do: assert(StatefulState.live?(state))

    # banked holds a snapshot, not a VM: NOT live (the singleton gate must let a
    # banked-only workload cold-boot a fresh instance).
    refute StatefulState.live?(:banked)

    for state <- [:evicted, :destroyed, :failed], do: refute(StatefulState.live?(state))
  end

  test "terminal_op_kind maps each terminal state to its op kind" do
    assert StatefulState.terminal_op_kind(:evicted) == :stateful_evicted
    assert StatefulState.terminal_op_kind(:destroyed) == :stateful_destroyed
    assert StatefulState.terminal_op_kind(:failed) == :stateful_failed
  end

  test "no terminal state has any outgoing edge (a stateful instance dies terminally)" do
    for state <- StatefulState.terminal_states(), event <- StatefulState.events() do
      assert StatefulState.transition(state, event) == {:error, {:illegal_transition, state, event}}
    end
  end

  test "banked cannot fail (it holds no VM); a broken bundle evicts instead" do
    assert StatefulState.transition(:banked, :fail) == {:error, {:illegal_transition, :banked, :fail}}
    assert StatefulState.transition(:banked, :evict) == {:ok, :evicted}
  end

  test "both wake paths off banked land back on starting" do
    assert StatefulState.transition!(:banked, :relight) == :relighting
    assert StatefulState.transition!(:relighting, :relight_ready) == :starting
    assert StatefulState.transition!(:banked, :cold_boot) == :cold_booting
    assert StatefulState.transition!(:cold_booting, :cold_ready) == :starting
  end

  test "interruptible bank: checkpoint forks to commit->banked or abort->serving (ADR 008)" do
    # The CHECKPOINT completes off banking (not the atomic bank_ready).
    assert StatefulState.transition!(:banking, :checkpoint_ready) == :checkpointed
    # The resolve forks: commit banks, abort resumes to serving.
    assert StatefulState.transition!(:checkpointed, :commit) == :banked
    assert StatefulState.transition!(:checkpointed, :abort) == :serving
    # A checkpointed instance can still be destroyed (forced roll) or failed
    # (resolve RPC error tore the paused VM down).
    assert StatefulState.transition!(:checkpointed, :destroy) == :destroyed
    assert StatefulState.transition!(:checkpointed, :fail) == :failed
  end

  test "checkpointed cannot skip the resolve (no direct edge to banked/serving)" do
    # Only commit/abort (plus destroy/fail) leave checkpointed; a raw bank_ready or
    # publish is illegal, so a checkpoint must be explicitly resolved.
    assert StatefulState.transition(:checkpointed, :bank_ready) ==
             {:error, {:illegal_transition, :checkpointed, :bank_ready}}

    assert StatefulState.transition(:checkpointed, :publish) ==
             {:error, {:illegal_transition, :checkpointed, :publish}}

    # And the checkpoint edge is only reachable from banking, not directly from serving.
    assert StatefulState.transition(:serving, :checkpoint_ready) ==
             {:error, {:illegal_transition, :serving, :checkpoint_ready}}
  end
end
