defmodule Embervm.Brick.Portfolio do
  @moduledoc """
  Derives the minimum size-class portfolio from declared workload floors.

  Every positive workload floor emits that many memory-sized items. Items are
  assigned to the smallest declared class whose admission capacity can hold
  them, then packed within that exact class using deterministic
  first-fit-decreasing. Admission capacity is usable memory minus the daemon's
  memory rejection cushion. The cushion is reserved once per bin, while the bin
  is also capped by the declared live-VM slot limit. Items never move to a
  larger class to reduce the number of bins.

  `floors/2` returns one result per declared class, keyed by class name:

      %{
        "2gi" => %{
          computed_floor: 2,
          effective_min: 2,
          flag: nil
        }
      }

  `effective_min` preserves a chart-declared manual minimum by taking the
  maximum of it and `computed_floor`. `flag` is `:floor_overflow` when the
  computed floor exceeds the class's authorized outer bound (or its fixed
  maximum when runtime ceiling ownership is disabled), or when an item cannot
  fit even the largest declared class.

  Catalog entries and class declarations are already validated at their input
  boundaries. This function remains total for stale or legacy maps: entries
  without a positive integer floor and memory size contribute nothing, while a
  class without positive admission capacity cannot receive an item. A zero
  slot limit means no node-side slot ceiling, matching noded's configuration
  contract.
  """

  @type class_result :: %{
          computed_floor: non_neg_integer(),
          effective_min: non_neg_integer(),
          flag: nil | :floor_overflow
        }

  @spec floors([map()], [map()]) :: %{String.t() => class_result()}
  def floors(catalog, classes) when is_list(catalog) and is_list(classes) do
    normalized_classes = normalize_classes(classes)

    bins_by_class =
      catalog
      |> floor_items()
      |> Enum.reduce(%{}, fn item, bins -> pack_item(item, normalized_classes, bins) end)

    Map.new(normalized_classes, fn class ->
      bins = Map.get(bins_by_class, class.name, [])
      computed = length(bins)
      effective_min = max(class.min, computed)
      flag =
        if computed > class.max or Enum.any?(bins, &Map.get(&1, :oversized, false)),
          do: :floor_overflow

      {class.name,
       %{computed_floor: computed, effective_min: effective_min, flag: flag}}
    end)
  end

  defp normalize_classes(classes) do
    classes
    |> Enum.flat_map(fn class ->
      name = field(class, [:name, "name"])
      usable_mib = field(class, [:usable_mib, "usable_mib"])
      reject_floor_mib =
        field(class, [:mem_reject_floor_mib, "mem_reject_floor_mib"])

      slots = field(class, [:slots, "slots", :max_live_vms, "max_live_vms", "maxLiveVMs"])

      if is_binary(name) and is_integer(usable_mib) and usable_mib > 0 and
           is_integer(reject_floor_mib) and reject_floor_mib >= 0 and
           is_integer(slots) and slots >= 0 do
        min = non_negative(field(class, [:min, "min"]), 0)
        desired = non_negative(field(class, [:desired, "desired"]), 0)
        max_replicas = non_negative(field(class, [:max, "max"]), max(desired, min))
        ceiling_bound = non_negative(field(class, [:ceiling_bound, "ceiling_bound"]), 0)
        authorized_max = if ceiling_bound > 0, do: max(max_replicas, ceiling_bound), else: max_replicas

        [
          %{
            name: name,
            usable_mib: usable_mib,
            capacity_mib: max(usable_mib - reject_floor_mib, 0),
            slots: slots,
            min: min,
            max: authorized_max
          }
        ]
      else
        []
      end
    end)
    |> Enum.sort_by(&{&1.capacity_mib, &1.name})
  end

  defp floor_items(catalog) do
    catalog
    |> Enum.flat_map(fn entry ->
      name = field(entry, [:name, "name"])
      floor = field(entry, [:floor, "floor"])
      mem_mib = field(entry, [:mem_mib, "mem_mib", "memMib"])

      if is_binary(name) and is_integer(floor) and floor > 0 and
           is_integer(mem_mib) and mem_mib > 0 do
        List.duplicate(%{name: name, mem_mib: mem_mib}, floor)
      else
        []
      end
    end)
    |> Enum.sort_by(fn item -> {-item.mem_mib, item.name} end)
  end

  defp pack_item(item, classes, bins_by_class) do
    case Enum.find(classes, &(&1.capacity_mib >= item.mem_mib)) do
      nil ->
        mark_unplaceable(item, classes, bins_by_class)

      class ->
        bins = Map.get(bins_by_class, class.name, [])
        Map.put(bins_by_class, class.name, first_fit(bins, item.mem_mib, class))
    end
  end

  # Attribute an item larger than every declared class to the largest class for
  # diagnostics, but mark the synthetic bin oversized so it always raises
  # floor_overflow and can never trigger a scale write.
  defp mark_unplaceable(_item, [], bins_by_class), do: bins_by_class

  defp mark_unplaceable(item, classes, bins_by_class) do
    class = List.last(classes)
    bin = %{used_mib: item.mem_mib, used_slots: 1, oversized: true}
    Map.update(bins_by_class, class.name, [bin], &(&1 ++ [bin]))
  end

  defp first_fit(bins, mem_mib, class) do
    case Enum.find_index(bins, &fits?(&1, mem_mib, class)) do
      nil ->
        bins ++ [%{used_mib: mem_mib, used_slots: 1}]

      index ->
        List.update_at(
          bins,
          index,
          &%{used_mib: &1.used_mib + mem_mib, used_slots: &1.used_slots + 1}
        )
    end
  end

  defp fits?(bin, mem_mib, class) do
    bin.used_mib + mem_mib <= class.capacity_mib and
      (class.slots == 0 or bin.used_slots < class.slots)
  end

  defp non_negative(value, _default) when is_integer(value) and value >= 0, do: value
  defp non_negative(_value, default), do: default

  defp field(map, keys), do: Enum.find_value(keys, &Map.get(map, &1))
end
