defmodule Embervm.Brick do
  @moduledoc """
  A brick is one noded instance. This module normalizes raw capacity facts from
  the `Embervm.NodeCapacity` ETS table and provides brick properties used by
  scheduling.

  `free_slots/1` uses an explicit `:free_slots` value when present. Otherwise it
  computes `max(max_live_vms - live_vms, 0)`, so it accepts normalized bricks,
  raw capacity facts, and PoolManager instance maps. A present value of zero is
  respected, while a missing or nil value falls back to the count calculation.
  """

  alias Embervm.NodeCapacity
  @typedoc """
  One brick: a dispatchable noded instance, normalized from its capacity facts to
  just the fields brick placement reasons about.
  """
  @type brick :: %{
          node_id: String.t(),
          pod_uid: String.t(),
          instance_id: String.t(),
          size_class: String.t(),
          mem_headroom_mib: non_neg_integer(),
          mem_reject_floor_mib: non_neg_integer(),
          mem_budget_mib: non_neg_integer(),
          live_vms: non_neg_integer(),
          max_live_vms: non_neg_integer(),
          free_slots: non_neg_integer(),
          configured_id: String.t(),
          serving_subnet_cidr: String.t(),
          workloads: %{optional(term()) => map()}
        }

  @doc """
  Every dispatchable brick, in no particular order. Empty when nothing is
  dispatchable (the fail-closed default `NodeCapacity.all/1` already guarantees,
  including before the registry has booted).
  """
  @spec bricks(atom()) :: [brick()]
  def bricks(table \\ NodeCapacity.table()) do
    table
    |> NodeCapacity.all()
    |> Enum.map(&to_brick/1)
  end

  @doc "All dispatchable bricks bucketed by size-class label."
  @spec by_class(atom()) :: %{String.t() => [brick()]}
  def by_class(table \\ NodeCapacity.table()) do
    table
    |> bricks()
    |> Enum.group_by(& &1.size_class)
  end

  @doc "Whether a brick has an empty class or no configured memory budget."
  @spec wildcard?(map()) :: boolean()
  def wildcard?(brick) do
    Map.get(brick, :size_class, "") == "" or Map.get(brick, :mem_budget_mib, 0) == 0
  end

  @doc """
  Returns the dial key for a brick. Legacy or statically-seeded capacity facts
  carry no `instance_id`, so the node name is the correct dial key for them.
  """
  @spec dial_id(map()) :: String.t()
  def dial_id(brick) do
    case Map.get(brick, :instance_id) do
      id when is_binary(id) and id != "" -> id
      _ -> Map.get(brick, :configured_id, "")
    end
  end

  @doc "Return available VM slots, honoring an explicit value when present."
  @spec free_slots(map()) :: non_neg_integer()
  def free_slots(brick) do
    Map.get(brick, :free_slots) ||
      max(Map.get(brick, :max_live_vms, 0) - Map.get(brick, :live_vms, 0), 0)
  end

  # Normalize one capacity-facts map to a brick. Missing numeric facts read as 0
  # (a daemon that never set a budget field), which fail-closed makes that brick
  # simply uncompetitive rather than crashing the reader.
  defp to_brick(facts) do
    live = Map.get(facts, :live_vms, 0)
    max_live = Map.get(facts, :max_live_vms, 0)

    %{
      node_id: Map.get(facts, :node_id, ""),
      pod_uid: Map.get(facts, :pod_uid, ""),
      instance_id: Map.get(facts, :instance_id, ""),
      size_class: Map.get(facts, :size_class, ""),
      mem_headroom_mib: Map.get(facts, :mem_headroom_mib, 0),
      mem_reject_floor_mib: Map.get(facts, :mem_reject_floor_mib, 0),
      mem_budget_mib: Map.get(facts, :mem_budget_mib, 0),
      configured_id: Map.get(facts, :configured_id) || "",
      serving_subnet_cidr: Map.get(facts, :serving_subnet_cidr) || "",
      live_vms: live,
      max_live_vms: max_live,
      free_slots: max(max_live - live, 0),
      # The per-workload capacity submap (`%{workload => %{base_state:, snapshot_ref:,
      # ...}}`), carried through unchanged so `Embervm.Scheduler.base_ready?/2` can read
      # `workloads[workload].base_state` off a normalized brick. Absent on a fact that
      # predates the field -> `%{}`, which reads as base-not-ready (fail-closed: a brick
      # that never advertised a base is not a cold-placement target).
      workloads: Map.get(facts, :workloads) || %{}
    }
  end
end
