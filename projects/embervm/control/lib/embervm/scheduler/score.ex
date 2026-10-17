defmodule Embervm.Scheduler.Score do
  @moduledoc """
  Brick placement scoring and deterministic ordering: the single ordering
  primitive every placement path shares.
  """

  @doc """
  Score a brick for placement. Two policies, one comparable scale.

  Classed bricks are reclaimable (an empty one is the unit of reclaim, ADR
  embervm/016 section 4) and have a real budget, so they PACK: k8s MostAllocated,
  `(slot_ratio + mem_ratio) / 2`, range [0.0, 1.0], higher = fuller = preferred.

  Wildcards (empty `size_class`) are neither reclaimable nor measurable, so they
  SPREAD: k8s LeastAllocated, `-1.0 - slot_ratio`, range [-2.0, -1.0], higher =
  emptier = preferred. That keeps every wildcard below every classed brick while
  spreading among themselves. Packing them would concentrate a workload onto one
  arbitrary brick with the memory gate disabled (`Scheduler.mem_eligible?/2`
  short-circuits true for a wildcard) for none of the reclaim benefit that
  justifies packing. A BLIND classed brick (`Embervm.Brick.blind?/1`, zero
  budget) has no memory signal either, so it scores on the same spread scale;
  the memory gate already refuses it, so this only orders a no-memory-gate
  (`need_mib: nil`) request.

  `slot_ratio` is an exact count and is the only signal trusted for wildcards.
  `mem_ratio` follows the brick's admission model, net of the in-flight debit
  `Scheduler.place/1` charges (`:pending_mib`, absent reads as 0), so an assumed
  placement counts as allocated exactly as k8s counts an assumed pod:

    * reserved model (`admits_on_reservation`): DECLARED use,
      `(budget - (Brick.admittable_mib/1 - pending)) / budget`, the same
      quantity the daemon admits on, so packing follows commitments rather
      than lagging residency.
    * observed model: `(budget - (headroom - pending)) / budget`. OBSERVED cgroup
      headroom lags: memory.max and memory.current are read separately and a
      just-restored Firecracker guest faults its pages in lazily. The clamp
      exists for that race.
  """
  @spec score(map()) :: float()
  def score(brick) do
    max_live = Map.get(brick, :max_live_vms, 0)
    live = Map.get(brick, :live_vms, 0)
    slot_ratio = if max_live == 0, do: 0.0, else: live / max_live

    if Embervm.Brick.wildcard?(brick) or Embervm.Brick.blind?(brick) do
      -1.0 - slot_ratio
    else
      budget = Map.get(brick, :mem_budget_mib, 0)
      pending = pending_mib(brick)

      free =
        if Embervm.Brick.reservation?(brick),
          do: Embervm.Brick.admittable_mib(brick) - pending,
          else: Map.get(brick, :mem_headroom_mib, 0) - pending

      mem_ratio = ((budget - free) / budget) |> clamp(0.0, 1.0)
      (slot_ratio + mem_ratio) / 2.0
    end
  end

  @doc """
  Order candidates by score, then best-fit toward the smallest usable brick,
  with the sticky hash breaking remaining ties. This pursues ADR embervm/016
  section 4's anti-fragmentation goal by best-fit, not its unimplemented
  class-exact mechanism, which ADR embervm/021 makes obsolete because a
  workload has no class.
  """
  @spec order([map()], term()) :: [map()]
  def order([], _key), do: []

  def order(candidates, key) do
    candidates
    |> Enum.group_by(&rounded_score/1)
    |> Enum.sort_by(&elem(&1, 0), :desc)
    |> order_groups(key)
  end

  defp order_groups([{_score, group} | rest], key) do
    order_by_fit(group, key) ++
      Enum.flat_map(rest, fn {_score, items} -> order_by_fit(items, key) end)
  end

  defp order_by_fit(items, key) do
    items
    |> Enum.group_by(&Map.get(&1, :mem_budget_mib, 0))
    |> Enum.sort_by(&elem(&1, 0), :asc)
    |> Enum.flat_map(fn {_budget, same} -> rotate_sticky(same, key) end)
  end

  defp rotate_sticky(items, key) do
    sorted = Enum.sort_by(items, &Map.get(&1, :instance_id, ""))
    rotate(sorted, :erlang.phash2(key, length(sorted)))
  end

  defp rounded_score(brick), do: Float.round(score(brick), 3)

  defp rotate(items, 0), do: items
  defp rotate(items, offset) do
    {head, tail} = Enum.split(items, offset)
    tail ++ head
  end

  defp clamp(value, low, high), do: value |> max(low) |> min(high)

  defp pending_mib(brick) do
    case Map.get(brick, :pending_mib) do
      n when is_integer(n) and n > 0 -> n
      _ -> 0
    end
  end
end
