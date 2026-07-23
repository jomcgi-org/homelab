defmodule Embervm.GroupStateTest do
  @moduledoc """
  Exhaustive coverage of the composite-group FSM (`Embervm.GroupState`): every
  legal transition maps to the documented next state, and EVERY illegal (state,
  event) pair is refused by `transition/2` and RAISES via `transition!/2`. The
  live/terminal predicates and the deliberate ABSENCE of a `degraded` state are
  asserted too (degraded is a flag on `running`, not an FSM node).
  """
  use ExUnit.Case, async: true

  alias Embervm.GroupState
  alias Embervm.GroupState.IllegalTransition

  @legal %{
    {:creating, :publish} => :running,
    {:running, :unpublish} => :banking,
    {:running, :bank} => :banking,
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :running,
    {:banked, :relight} => :relighting,
    {:relighting, :relight_ready} => :creating,
    {:relighting, :relight_abort} => :banked,
    {:banked, :fresh_boot} => :fresh_booting,
    {:fresh_booting, :fresh_ready} => :creating,
    {:fresh_booting, :fresh_abort} => :banked,
    {:creating, :destroy} => :destroyed,
    {:running, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    {:fresh_booting, :destroy} => :destroyed,
    # Node-confirmed destroy (ADR embervm/014 decision 5): begin_destroy from every
    # non-terminal state, then destroying -> destroy.
    {:creating, :begin_destroy} => :destroying,
    {:running, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:fresh_booting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    {:creating, :fail} => :failed,
    {:running, :fail} => :failed,
    {:banking, :fail} => :failed,
    {:relighting, :fail} => :failed,
    {:fresh_booting, :fail} => :failed
  }

  test "every legal transition maps to the documented next state" do
    for {{state, event}, next} <- @legal do
      assert GroupState.transition(state, event) == {:ok, next}
      assert GroupState.transition!(state, event) == next
    end
  end

  test "there is no `degraded` FSM state (degraded is a flag on running)" do
    refute :degraded in GroupState.states()
    # No event ever transitions INTO or OUT of a degraded state.
    refute Enum.any?(@legal, fn {{s, _e}, n} -> s == :degraded or n == :degraded end)
  end

  test "every (state, event) pair NOT in the legal table is illegal and raises" do
    states = GroupState.states()
    events = GroupState.events()

    for state <- states, event <- events, not Map.has_key?(@legal, {state, event}) do
      assert GroupState.transition(state, event) == {:error, {:illegal_transition, state, event}},
             "expected #{inspect(state)} -/-> on #{inspect(event)} to be illegal"

      assert_raise IllegalTransition, fn -> GroupState.transition!(state, event) end
    end
  end

  test "terminal states never transition on any event" do
    for state <- GroupState.terminal_states(), event <- GroupState.events() do
      assert GroupState.transition(state, event) == {:error, {:illegal_transition, state, event}}
    end
  end

  test "live? is the non-terminal, non-banked states (destroying included)" do
    # destroying IS live (ADR embervm/014: the per-member node-confirmed teardown
    # RPCs are in flight, so the singleton guard must still count the group).
    assert GroupState.live_states() ==
             [:creating, :running, :banking, :relighting, :fresh_booting, :destroying]

    for s <- [:creating, :running, :banking, :relighting, :fresh_booting, :destroying],
        do: assert(GroupState.live?(s))

    for s <- [:banked, :destroyed, :failed], do: refute(GroupState.live?(s))
  end

  test "terminal? is exactly destroyed + failed" do
    assert GroupState.terminal_states() == [:destroyed, :failed]
    for s <- [:destroyed, :failed], do: assert(GroupState.terminal?(s))

    for s <- [:creating, :running, :banking, :banked, :relighting, :fresh_booting, :destroying],
        do: refute(GroupState.terminal?(s))
  end
end
