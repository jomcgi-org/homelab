defmodule Embervm.BrickLedger do
  @moduledoc """
  Pure query surface over the per-instance capacity facts (`Embervm.NodeCapacity`)
  that presents the fleet as a set of fixed-size BRICKS: a brick is one noded
  instance deployed as exactly one T-shirt size-class, and the ledger buckets the
  dispatchable instances by that class and picks a brick of the right class for a
  workload. NO process and NO table of its own: like `Embervm.NodeCapacity` it is
  a stateless read surface over the registry-owned capacity ETS, so it is always
  consistent with dispatch's own view and can never drift from it.

  ## the capacity model this encodes (ADR embervm/013 section 7 as amended)

  Bricks are the single capacity unit on both tiers. The control plane moves brick
  COUNTS, never sizes (in-place resize is dropped): a workload that needs `N` MiB
  is placed onto a brick whose size-class holds it, and when no brick of the
  matching class has room the request is fleet-full (Karpenter adds a node on EKS,
  the fixed homelab refuses and pages). This module is the read half of that
  policy; the count controller that scales the brick Deployments and the placement
  consumers that call `pick/4` are the write/decision halves.

  ## the wildcard class (legacy DaemonSet compatibility)

  A brick carries its class from `NodeStatus.size_class`, echoed into the capacity
  facts as `:size_class`. The legacy DaemonSet (and any daemon predating the
  field) reports an EMPTY class; the ledger treats empty as the WILDCARD on the
  supply side, so a wildcard brick satisfies a request for ANY class. That is what
  makes the placement rewrite (PR-2) a no-op while the fleet is still DS-only: the
  single DS instance is a wildcard brick that every workload matches, exactly the
  pre-brick behaviour. Once real size-classed bricks exist beside it (PR-3), a
  request for a concrete class prefers those but still falls back to the wildcard.

  ## PR-1: populated but UNREAD

  In the inert first PR nothing calls `pick/4` or `by_class/1`; the placement
  consumers (dispatcher/pool/session/serving/stateful/group) still run their
  pre-brick node selection. This module exists so the data shape and the selection
  primitive land, tested, one PR ahead of the rewrite that reads them. Deleting
  `prefer_newest_per_node` and routing every consumer through `pick/4` is PR-2.
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
          mem_budget_mib: non_neg_integer(),
          live_vms: non_neg_integer(),
          max_live_vms: non_neg_integer(),
          free_slots: non_neg_integer()
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

  @doc """
  All dispatchable bricks bucketed by size-class label. The wildcard class is
  bucketed under its literal empty-string key `""`; callers that want "bricks that
  can serve class X" should use `candidates/3`, which folds the wildcard in.
  """
  @spec by_class(atom()) :: %{String.t() => [brick()]}
  def by_class(table \\ NodeCapacity.table()) do
    table
    |> bricks()
    |> Enum.group_by(& &1.size_class)
  end

  @doc """
  The bricks that can currently serve a request for `size_class` needing
  `need_mib` MiB: class matches (an exact-class brick OR a wildcard/empty-class
  brick), memory headroom covers the need, and at least one live-VM slot is free.
  Sorted by `instance_id` so the order is deterministic regardless of ETS scan
  order (the stable base `pick/4` selects over).
  """
  @spec candidates(String.t(), non_neg_integer(), atom()) :: [brick()]
  def candidates(size_class, need_mib, table \\ NodeCapacity.table()) do
    table
    |> bricks()
    |> Enum.filter(&serves?(&1, size_class, need_mib))
    |> Enum.sort_by(& &1.instance_id)
  end

  @doc """
  Deterministically pick a brick for a `size_class` request needing `need_mib`
  MiB, keyed by `key` (the workload/session identifier). `{:ok, brick}` when a
  brick can serve it, `{:error, :fleet_full}` when none can.

  Selection hashes `key` across the sorted candidate list (`:erlang.phash2/2`), so
  the same key sticks to the same brick as long as that brick stays a candidate
  (warmth reuse) while distinct keys spread across the available bricks. It
  deliberately does NOT prefer the newest instance per node: preferring newest
  collapses a node that holds several bricks down to a single instance, which is
  the exact multi-brick regression the placement rewrite must avoid.
  """
  @spec pick(String.t(), non_neg_integer(), term(), atom()) ::
          {:ok, brick()} | {:error, :fleet_full}
  def pick(size_class, need_mib, key, table \\ NodeCapacity.table()) do
    case candidates(size_class, need_mib, table) do
      [] -> {:error, :fleet_full}
      cands -> {:ok, Enum.at(cands, :erlang.phash2(key, length(cands)))}
    end
  end

  # A brick serves a request when its class matches (exact, or it is a wildcard/
  # empty-class brick) AND it has the memory headroom AND a free live-VM slot.
  defp serves?(brick, size_class, need_mib) do
    class_ok = brick.size_class == size_class or brick.size_class == ""
    class_ok and brick.mem_headroom_mib >= need_mib and brick.free_slots > 0
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
      mem_budget_mib: Map.get(facts, :mem_budget_mib, 0),
      live_vms: live,
      max_live_vms: max_live,
      free_slots: max(max_live - live, 0)
    }
  end
end
