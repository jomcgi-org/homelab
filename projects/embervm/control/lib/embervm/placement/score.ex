defmodule Embervm.Placement.Score do
  @moduledoc """
  Brick placement scoring and deterministic ordering: the single ordering
  primitive every placement path shares.
  """

  @doc """
  Score a brick for placement. Two policies, one comparable scale.

  Classed bricks are reclaimable (an empty one is the unit of reclaim, ADR
  embervm/016 section 4) and have a real budget, so they PACK: k8s MostAllocated,
  `(slot_ratio + mem_ratio) / 2`, range [0.0, 1.0], higher = fuller = preferred.

  Wildcards (empty `size_class`, or an unreadable cgroup reporting zero budget)
  are neither reclaimable nor measurable, so they SPREAD: k8s LeastAllocated,
  `-1.0 - slot_ratio`, range [-2.0, -1.0], higher = emptier = preferred. That
  keeps every wildcard below every classed brick while spreading among
  themselves. Packing them would concentrate a workload onto one arbitrary brick
  with the memory gate disabled (`Placement.mem_eligible?/2` short-circuits true
  for a wildcard) for none of the reclaim benefit that justifies packing.

  `slot_ratio` is an exact count and is the only signal trusted for wildcards.
  `mem_ratio` is derived from OBSERVED cgroup headroom, which lags: memory.max
  and memory.current are read separately and a just-restored Firecracker guest
  faults its pages in lazily. The clamp exists for that race. Issue #4140 Phase B
  replaces that term with a reserved/allocatable reservation ledger, so do not
  try to fix the lag here.
  """
  @spec score(map()) :: float()
  def score(brick) do
    max_live = Map.get(brick, :max_live_vms, 0)
    live = Map.get(brick, :live_vms, 0)
    slot_ratio = if max_live == 0, do: 0.0, else: live / max_live

    if Embervm.Placement.wildcard?(brick) do
      -1.0 - slot_ratio
    else
      budget = Map.get(brick, :mem_budget_mib, 0)
      headroom = Map.get(brick, :mem_headroom_mib, 0)
      mem_ratio = ((budget - headroom) / budget) |> clamp(0.0, 1.0)
      (slot_ratio + mem_ratio) / 2.0
    end
  end

  @doc """
  Order candidates by score with sticky tie-breaking.
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
    leading = Enum.sort_by(group, &Map.get(&1, :instance_id, ""))
    rotated = rotate(leading, :erlang.phash2(key, length(leading)))
    rotated ++ Enum.flat_map(rest, fn {_score, items} -> Enum.sort_by(items, &Map.get(&1, :instance_id, "")) end)
  end

  defp rounded_score(brick), do: Float.round(score(brick), 3)

  defp rotate(items, 0), do: items
  defp rotate(items, offset) do
    {head, tail} = Enum.split(items, offset)
    tail ++ head
  end

  defp clamp(value, low, high), do: value |> max(low) |> min(high)
end
