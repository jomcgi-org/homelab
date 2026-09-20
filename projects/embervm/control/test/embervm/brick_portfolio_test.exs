defmodule Embervm.Brick.PortfolioTest do
  use ExUnit.Case, async: true

  alias Embervm.Brick.Portfolio

  defp class(name, usable_mib, slots, overrides \\ %{}) do
    Map.merge(
      %{name: name, usable_mib: usable_mib, slots: slots, min: 0, max: 10, desired: 0},
      overrides
    )
  end

  test "zero floors contribute no bins" do
    catalog = [%{name: "cold", floor: 0, mem_mib: 512}]

    assert Portfolio.floors(catalog, [class("2gi", 1_792, 8)]) == %{
             "2gi" => %{computed_floor: 0, effective_min: 0, flag: nil}
           }
  end

  test "an exact memory fit occupies one bin" do
    catalog = [%{name: "semgrep", floor: 1, mem_mib: 1_792}]

    assert Portfolio.floors(catalog, [class("2gi", 1_792, 8)])["2gi"] == %{
             computed_floor: 1,
             effective_min: 1,
             flag: nil
           }
  end

  test "first-fit-decreasing packs multiple workloads by memory" do
    catalog = [
      %{name: "large", floor: 2, mem_mib: 600},
      %{name: "small", floor: 2, mem_mib: 400}
    ]

    assert Portfolio.floors(catalog, [class("pool", 1_000, 8)])["pool"].computed_floor == 2
  end

  test "memory fragmentation can require more bins than aggregate capacity" do
    catalog = [%{name: "fragment", floor: 4, mem_mib: 600}]

    assert Portfolio.floors(catalog, [class("pool", 1_000, 8)])["pool"].computed_floor == 4
  end

  test "slot limits open a new bin before memory is exhausted" do
    catalog = [%{name: "tiny", floor: 3, mem_mib: 100}]

    assert Portfolio.floors(catalog, [class("pool", 1_000, 2)])["pool"].computed_floor == 2
  end

  test "items stay in the smallest fitting class without cross-class borrowing" do
    catalog = [%{name: "small", floor: 3, mem_mib: 600}]
    classes = [class("1gi", 700, 8), class("2gi", 1_400, 8)]

    result = Portfolio.floors(catalog, classes)

    assert result["1gi"].computed_floor == 3
    assert result["2gi"].computed_floor == 0
  end

  test "manual minima remain effective warmth overrides" do
    result =
      Portfolio.floors(
        [%{name: "warm", floor: 1, mem_mib: 512}],
        [class("16gi", 16_000, 8, %{min: 2})]
      )

    assert result["16gi"] == %{computed_floor: 1, effective_min: 2, flag: nil}
  end

  test "computed floors above max are flagged instead of clamped" do
    result =
      Portfolio.floors(
        [%{name: "busy", floor: 3, mem_mib: 600}],
        [class("1gi", 700, 8, %{max: 2})]
      )

    assert result["1gi"] == %{
             computed_floor: 3,
             effective_min: 3,
             flag: :floor_overflow
           }
  end

  test "an item larger than every declared class is an immediate overflow" do
    result =
      Portfolio.floors(
        [%{name: "oversized", floor: 1, mem_mib: 2_000}],
        [class("largest", 1_000, 8, %{max: 4})]
      )

    assert result["largest"].computed_floor == 1
    assert result["largest"].flag == :floor_overflow
  end

  test "catalog and class input order cannot change the result" do
    catalog = [
      %{name: "zeta", floor: 2, mem_mib: 400},
      %{name: "alpha", floor: 2, mem_mib: 600}
    ]

    classes = [class("large", 2_000, 8), class("small", 1_000, 8)]

    assert Portfolio.floors(catalog, classes) ==
             Portfolio.floors(Enum.reverse(catalog), Enum.reverse(classes))
  end

  test "a zero slot limit retains noded's unlimited-slot meaning" do
    catalog = [%{name: "tiny", floor: 3, mem_mib: 100}]

    assert Portfolio.floors(catalog, [class("pool", 1_000, 0)])["pool"].computed_floor == 1
  end
end
