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

  ## how the dispatcher reads this (PR-2)

  The task dispatcher (`Embervm.Dispatcher.pick_node/2`) is the one placement path
  that was actually brick-broken: it collapsed each node to its newest instance
  (`prefer_newest_per_node`), which under co-located bricks would pin all of a
  node's work to one brick. PR-2 deleted that collapse and now tie-breaks its own
  warm- and miss-tier candidate sublists through `choose/2` (the deterministic
  sticky selection `pick/4` is built on). Warmth stays the dispatcher's own
  two-tier decision; this module stays warmth-agnostic. The other consumers were
  left as-is: session/serving placement already rendezvous-hash over per-instance
  facts (so they spread across co-located bricks for free), and stateful/group are
  volume-/node-anchored and never select among instances. `pick/4` and `by_class/1`
  remain UNREAD until a workload actually declares a size-class requirement (PR-3+).
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
          free_slots: non_neg_integer(),
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
    case choose(candidates(size_class, need_mib, table), key) do
      nil -> {:error, :fleet_full}
      brick -> {:ok, brick}
    end
  end

  @doc """
  Deterministically choose one entry from an ALREADY-FILTERED candidate list,
  keyed by `key`. Sorts by `:instance_id` so the order is stable regardless of the
  caller's list order, then hashes `key` across it (`:erlang.phash2/2`, always
  0..n-1 so the index is in-bounds). Same key sticks to the same entry as long as
  it stays a candidate; distinct keys spread across the list. Returns `nil` for an
  empty list.

  This is the selection primitive `pick/4` uses after applying the class/headroom/
  slot filter. The task dispatcher reuses it directly to tie-break its OWN warm-
  and miss-tier candidate sublists (which carry warmth/prime-budget facts the
  ledger deliberately does not model): it stops picking an arbitrary first match
  and spreads deterministically across co-located bricks instead, without the
  ledger having to learn about inventory. Accepts any map carrying `:instance_id`
  (a brick from this module or a raw capacity fact from the dispatcher).
  """
  @spec choose([map()], term()) :: map() | nil
  def choose([], _key), do: nil

  def choose(candidates, key) do
    sorted = Enum.sort_by(candidates, fn c -> Map.get(c, :instance_id, "") end)
    Enum.at(sorted, :erlang.phash2(key, length(sorted)))
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
      free_slots: max(max_live - live, 0),
      # The per-workload capacity submap (`%{workload => %{base_state:, snapshot_ref:,
      # ...}}`), carried through unchanged so `Embervm.Placement.base_ready?/2` can read
      # `workloads[workload].base_state` off a normalized brick. Absent on a fact that
      # predates the field -> `%{}`, which reads as base-not-ready (fail-closed: a brick
      # that never advertised a base is not a cold-placement target).
      workloads: Map.get(facts, :workloads) || %{}
    }
  end
end
