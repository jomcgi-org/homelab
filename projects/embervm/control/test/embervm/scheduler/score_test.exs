defmodule Embervm.Scheduler.ScoreTest do
  use ExUnit.Case, async: true

  alias Embervm.Scheduler.Score

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

  test "wildcards and blind classed bricks score last" do
    assert Score.score(brick(size_class: "", live_vms: 0)) == -1.0
    # A classed brick with a zero budget is no longer a wildcard: it is BLIND
    # (Brick.blind?/1) and the memory gate refuses it. It still has no memory
    # signal to pack on, so it scores on the spread scale, which only orders a
    # request with no memory gate at all.
    blind = brick(mem_budget_mib: 0, live_vms: 0)
    refute Embervm.Brick.wildcard?(blind)
    assert Embervm.Brick.blind?(blind)
    assert Score.score(blind) == -1.0
  end

  test "a reservation brick packs on declared use, net of the in-flight debit" do
    # Declared: 2048 reserved of 8192, no overhead, so admittable is 6144 and the
    # memory half of the score is 2048 / 8192 = 0.25, whatever observed headroom
    # says (here it claims the brick is nearly empty).
    reserved = Map.merge(brick(mem_headroom_mib: 8_000, live_vms: 0), %{admits_on_reservation: true, mem_reserved_mib: 2_048})
    assert_in_delta Score.score(reserved), 0.125, 0.0001

    # An in-flight claim of 2048 counts as allocated: 4096 / 8192.
    assert_in_delta Score.score(Map.put(reserved, :pending_mib, 2_048)), 0.25, 0.0001

    # A daemon that reports its own admission ledger is taken at its word.
    reported = Map.merge(reserved, %{admission_reported: true, mem_admittable_mib: 0})
    assert_in_delta Score.score(reported), 0.5, 0.0001
  end

  test "an observed brick keeps the headroom ratio, net of the in-flight debit" do
    observed = brick(mem_headroom_mib: 4_096, live_vms: 0)
    assert_in_delta Score.score(observed), 0.25, 0.0001
    assert_in_delta Score.score(Map.put(observed, :pending_mib, 2_048)), 0.375, 0.0001
  end

  test "classed fullness scores from empty to full" do
    assert Score.score(brick(mem_headroom_mib: 8_192, live_vms: 0)) == 0.0
    assert Score.score(brick(mem_headroom_mib: 0, live_vms: 4)) == 1.0
    assert_in_delta Score.score(brick(mem_headroom_mib: 4_096, live_vms: 2)), 0.5, 0.0001
  end

  test "headroom race is clamped" do
    assert Score.score(brick(mem_headroom_mib: 16_384, live_vms: 0)) == 0.0
  end

  test "emptier wildcards score ahead of fuller wildcards" do
    emptier = brick(instance_id: "empty", size_class: "", live_vms: 1)
    fuller = brick(instance_id: "full", size_class: "", live_vms: 3)

    assert Score.score(emptier) > Score.score(fuller)
    assert [emptier, fuller] == Score.order([fuller, emptier], "key")
  end

  test "wildcards score behind every classed brick, including a full one" do
    full = brick(instance_id: "full", mem_headroom_mib: 0, live_vms: 4)
    wildcard = brick(instance_id: "wild", size_class: "", live_vms: 0)

    assert Score.score(full) == 1.0
    assert Score.score(wildcard) == -1.0
    assert [full, wildcard] == Score.order([wildcard, full], "key")
  end

  test "wildcard with no live-VM capacity scores -1.0" do
    assert Score.score(brick(size_class: "", max_live_vms: 0, live_vms: 0)) == -1.0
  end

  test "order prefers fuller classed bricks and classed bricks over wildcards" do
    full = brick(instance_id: "full", mem_headroom_mib: 1_000, live_vms: 4)
    empty = brick(instance_id: "empty", mem_headroom_mib: 8_192, live_vms: 0)
    wildcard = brick(instance_id: "wild", size_class: "", mem_headroom_mib: 0)

    assert [full, empty, wildcard] == Score.order([wildcard, empty, full], "key")
  end

  test "a small VM must not strand the large brick" do
    small = brick(instance_id: "small", mem_budget_mib: 1_536, mem_headroom_mib: 1_536, live_vms: 0)
    large = brick(instance_id: "large", mem_budget_mib: 15_872, mem_headroom_mib: 15_872, live_vms: 0)

    assert [small, large] == Score.order([large, small], "key")
  end

  test "score still dominates best-fit" do
    large_full = brick(instance_id: "large", mem_budget_mib: 16_384, mem_headroom_mib: 8_192)
    small_empty = brick(instance_id: "small", mem_budget_mib: 2_048, mem_headroom_mib: 2_048, live_vms: 0)

    assert Score.score(large_full) > Score.score(small_empty)
    assert [large_full, small_empty] == Score.order([small_empty, large_full], "key")
  end

  test "distinct keys spread across bricks sharing score and budget" do
    first = brick(instance_id: "a", mem_budget_mib: 8_192, mem_headroom_mib: 8_192, live_vms: 0)
    second = brick(instance_id: "b", mem_budget_mib: 8_192, mem_headroom_mib: 8_192, live_vms: 0)

    heads =
      1..32
      |> Enum.map(&Score.order([first, second], "workload-#{&1}") |> List.first())
      |> Enum.uniq()

    assert length(heads) == 2
  end

  test "order head is the selected candidate" do
    candidates = [brick(instance_id: "a"), brick(instance_id: "b", live_vms: 4)]
    assert List.first(Score.order(candidates, "wl")).instance_id == "b"
  end

  test "order is a permutation" do
    candidates = for id <- ~w(a b c d), do: brick(instance_id: id, live_vms: String.to_integer(id, 36) - 10)
    ordered = Score.order(candidates, "wl")
    assert Enum.sort(Enum.map(ordered, & &1.instance_id)) == Enum.sort(Enum.map(candidates, & &1.instance_id))
    assert length(ordered) == length(Enum.uniq_by(ordered, & &1.instance_id))
  end

  test "wildcards tie on zero budget and remain behind classed bricks" do
    classed = brick(instance_id: "classed", mem_budget_mib: 1_536, mem_headroom_mib: 1_536, live_vms: 0)
    wildcards = [
      brick(instance_id: "wild-a", size_class: "", mem_budget_mib: 0, live_vms: 0),
      brick(instance_id: "wild-b", size_class: "", mem_budget_mib: 0, live_vms: 0)
    ]

    ordered = Score.order(wildcards ++ [classed], "key")

    assert List.first(ordered) == classed
    assert Enum.map(Enum.drop(ordered, 1), & &1.mem_budget_mib) == [0, 0]
  end

  test "order breaks ties by instance_id rather than input order" do
    first = brick(instance_id: "a")
    second = brick(instance_id: "b")

    assert Score.order([first, second], "wl") == Score.order([second, first], "wl")
  end
end
