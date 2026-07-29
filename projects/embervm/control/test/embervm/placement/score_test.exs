defmodule Embervm.Placement.ScoreTest do
  use ExUnit.Case, async: true

  alias Embervm.BrickLedger
  alias Embervm.Placement.Score

  defp brick(opts \\ []) do
    %{
      instance_id: Keyword.get(opts, :instance_id, "node/pod"),
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 4_096),
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 8_192),
      live_vms: Keyword.get(opts, :live_vms, 2),
      max_live_vms: Keyword.get(opts, :max_live_vms, 4)
    }
  end

  test "wildcards score last" do
    assert Score.most_allocated(brick(size_class: "")) == -1.0
    assert Score.most_allocated(brick(mem_budget_mib: 0)) == -1.0
  end

  test "classed fullness scores from empty to full" do
    assert Score.most_allocated(brick(mem_headroom_mib: 8_192, live_vms: 0)) == 0.0
    assert Score.most_allocated(brick(mem_headroom_mib: 0, live_vms: 4)) == 1.0
    assert_in_delta Score.most_allocated(brick(mem_headroom_mib: 4_096, live_vms: 2)), 0.5, 0.0001
  end

  test "headroom race is clamped" do
    assert Score.most_allocated(brick(mem_headroom_mib: 16_384, live_vms: 0)) == 0.0
  end

  test "order prefers fuller classed bricks and classed bricks over wildcards" do
    full = brick(instance_id: "full", mem_headroom_mib: 1_000, live_vms: 4)
    empty = brick(instance_id: "empty", mem_headroom_mib: 8_192, live_vms: 0)
    wildcard = brick(instance_id: "wild", size_class: "", mem_headroom_mib: 0)

    assert [full, empty, wildcard] == Score.order([wildcard, empty, full], "key")
  end

  test "order head equals BrickLedger.choose/2" do
    candidates = [brick(instance_id: "a"), brick(instance_id: "b", live_vms: 4)]
    assert List.first(Score.order(candidates, "wl")) == BrickLedger.choose(candidates, "wl")
  end

  test "order is a permutation" do
    candidates = for id <- ~w(a b c d), do: brick(instance_id: id, live_vms: String.to_integer(id, 36) - 10)
    ordered = Score.order(candidates, "wl")
    assert Enum.sort(Enum.map(ordered, & &1.instance_id)) == Enum.sort(Enum.map(candidates, & &1.instance_id))
    assert length(ordered) == length(Enum.uniq_by(ordered, & &1.instance_id))
  end

  test "order breaks ties by instance_id rather than input order" do
    first = brick(instance_id: "a")
    second = brick(instance_id: "b")

    assert Score.order([first, second], "wl") == Score.order([second, first], "wl")
  end
end
