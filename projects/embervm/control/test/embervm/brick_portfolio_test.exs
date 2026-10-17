defmodule Embervm.Brick.PortfolioTest do
  use ExUnit.Case, async: true

  alias Embervm.Brick.Portfolio

  defp class(name, capacity_mib, slots, overrides \\ %{}) do
    reject_floor_mib = Map.get(overrides, :mem_reject_floor_mib, 512)

    Map.merge(
      %{
        name: name,
        usable_mib: capacity_mib + reject_floor_mib,
        mem_reject_floor_mib: reject_floor_mib,
        slots: slots,
        min: 0,
        max: 10,
        desired: 0
      },
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

  test "the admission cushion is reserved once per bin, not once per VM" do
    catalog = [%{name: "paired", floor: 2, mem_mib: 640}]

    result =
      Portfolio.floors(catalog, [
        class("2gi", 1_280, 8, %{mem_reject_floor_mib: 512})
      ])

    assert result["2gi"].computed_floor == 1
  end

  test "production-sized 1536 MiB work moves from 2gi to 4gi" do
    catalog = [
      %{name: "semgrep", floor: 1, mem_mib: 1_536},
      %{name: "bazel-query", floor: 1, mem_mib: 1_024}
    ]

    classes = [
      %{
        name: "2gi",
        usable_mib: 1_792,
        mem_reject_floor_mib: 512,
        slots: 8,
        min: 0,
        max: 4,
        desired: 0
      },
      %{
        name: "4gi",
        usable_mib: 3_840,
        mem_reject_floor_mib: 512,
        slots: 8,
        min: 0,
        max: 3,
        desired: 0
      }
    ]

    result = Portfolio.floors(catalog, classes)

    assert result["2gi"].computed_floor == 1
    assert result["4gi"].computed_floor == 1
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

  test "a class with no authorized bound does not receive an item another class fits" do
    catalog = [%{name: "pi", floor: 1, mem_mib: 256}]
    classes = [class("1gi", 256, 6, %{max: 0}), class("2gi", 1_280, 14, %{max: 4})]

    assert Portfolio.floors(catalog, classes) == %{
             "1gi" => %{computed_floor: 0, effective_min: 0, flag: nil},
             "2gi" => %{computed_floor: 1, effective_min: 1, flag: nil}
           }
  end

  test "a positive ceiling bound authorizes a zero-max class" do
    catalog = [%{name: "mid", floor: 1, mem_mib: 1_500}]

    classes = [
      class("2gi", 1_280, 14, %{max: 4}),
      class("4gi", 3_328, 16, %{max: 0, ceiling_bound: 1})
    ]

    assert Portfolio.floors(catalog, classes)["4gi"] == %{
             computed_floor: 1,
             effective_min: 1,
             flag: nil
           }
  end

  test "an item only an unauthorized class can hold still overflows there" do
    catalog = [%{name: "big", floor: 1, mem_mib: 8_000}]
    classes = [class("2gi", 1_280, 14, %{max: 4}), class("16gi", 15_616, 16, %{max: 0})]

    assert Portfolio.floors(catalog, classes) == %{
             "2gi" => %{computed_floor: 0, effective_min: 0, flag: nil},
             "16gi" => %{computed_floor: 1, effective_min: 1, flag: :floor_overflow}
           }
  end

  test "the per-VM overhead is charged once per item alongside the per-bin cushion" do
    # 2gi: usable 1792, cushion 512. Ten 128 MiB items fit the cushion bound
    # (1280 + 512), but with a 64 MiB overhead each, nine use 9 * 192 = 1728
    # and a tenth would need 1920 > 1792.
    catalog = [%{name: "tiny", floor: 10, mem_mib: 128}]

    assert Portfolio.floors(catalog, [class("2gi", 1_280, 14)])["2gi"].computed_floor == 1

    assert Portfolio.floors(catalog, [class("2gi", 1_280, 14, %{vm_overhead_mib: 64})])["2gi"].computed_floor ==
             2
  end

  test "an overhead above the cushion decides whether a class fits at all" do
    # 3072 + 512 fits 4gi (usable 3840), but 3072 + 1024 does not.
    catalog = [%{name: "runtime", floor: 1, mem_mib: 3_072}]

    classes = fn overhead ->
      [
        class("4gi", 3_328, 16, %{vm_overhead_mib: overhead}),
        class("8gi", 7_424, 16, %{vm_overhead_mib: overhead})
      ]
    end

    assert Portfolio.floors(catalog, classes.(192))["4gi"].computed_floor == 1
    assert Portfolio.floors(catalog, classes.(1_024))["4gi"].computed_floor == 0
    assert Portfolio.floors(catalog, classes.(1_024))["8gi"].computed_floor == 1
  end

  test "class authorization keeps the result independent of input order" do
    catalog = [
      %{name: "zeta", floor: 2, mem_mib: 200},
      %{name: "alpha", floor: 1, mem_mib: 600}
    ]

    classes = [
      class("1gi", 256, 6, %{max: 0}),
      class("4gi", 3_328, 16, %{max: 2}),
      class("2gi", 1_280, 14, %{max: 4})
    ]

    result = Portfolio.floors(catalog, classes)

    assert result == Portfolio.floors(Enum.reverse(catalog), Enum.reverse(classes))
    assert result["1gi"].computed_floor == 0
    assert result["2gi"].computed_floor == 1
  end

  test "a zero slot limit retains noded's unlimited-slot meaning" do
    catalog = [%{name: "tiny", floor: 3, mem_mib: 100}]

    assert Portfolio.floors(catalog, [class("pool", 1_000, 0)])["pool"].computed_floor == 1
  end
end
