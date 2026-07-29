defmodule Embervm.Placement.Score do
  @moduledoc """
  MostAllocated scoring and deterministic brick ordering.
  """

  @doc """
  Score a brick by its fullness. Higher score = fuller = preferred. This is
  k8s's MostAllocated plugin (equal weight per resource), which is the prior
  art ADR 016 section 4 cites.

  A wildcard is the burst envelope (ADR 013): scored last so classed bricks
  pack first and the wildcard only absorbs what nothing classed can hold.

  `mem_ratio` is derived from OBSERVED cgroup headroom, which lags: memory.max
  and memory.current are read separately and a just-restored Firecracker guest
  faults its pages in lazily. The clamp exists for that race. Issue #4140
  Phase B replaces this term with a reserved/allocatable reservation ledger.
  Do not try to fix the lag here.
  """
  @spec most_allocated(map()) :: float()
  def most_allocated(brick) do
    if Embervm.Placement.wildcard?(brick) do
      -1.0
    else
      max_live = Map.get(brick, :max_live_vms, 0)
      live = Map.get(brick, :live_vms, 0)
      budget = Map.get(brick, :mem_budget_mib, 0)
      headroom = Map.get(brick, :mem_headroom_mib, 0)
      slot_ratio = if max_live == 0, do: 0.0, else: live / max_live
      mem_ratio = ((budget - headroom) / budget) |> clamp(0.0, 1.0)
      (slot_ratio + mem_ratio) / 2.0
    end
  end

  @doc """
  Order candidates by MostAllocated score with sticky tie-breaking.
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

  defp rounded_score(brick), do: Float.round(most_allocated(brick), 3)

  defp rotate(items, 0), do: items
  defp rotate(items, offset) do
    {head, tail} = Enum.split(items, offset)
    tail ++ head
  end

  defp clamp(value, low, high), do: value |> max(low) |> min(high)
end
