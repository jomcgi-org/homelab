defmodule Embervm.Brick do
  @moduledoc """
  A brick is one noded instance. This module normalizes raw capacity facts from
  the `Embervm.NodeCapacity` ETS table and provides brick properties used by
  scheduling.

  `free_slots/1` uses an explicit `:free_slots` value when present. Otherwise it
  computes `max(max_live_vms - live_vms, 0)`, so it accepts normalized bricks,
  raw capacity facts, and PoolManager instance maps. A present value of zero is
  respected, while a missing or nil value falls back to the count calculation.

  ## one memory gate

  `admittable_mib/1` is the single answer to "how large a guest would this brick
  admit for ONE more VM". A daemon that reports its own admission ledger
  (`admission_reported`) is taken at its word: `mem_admittable_mib` is computed
  by the same function its boot verbs call, so the control plane cannot drift
  from it. An older daemon falls back to the per-model predicate evaluated here
  on the facts it does report, which mirrors noded's `pressure.go` exactly. The
  memory predicates in `Embervm.Scheduler` and the PACK score in
  `Embervm.Scheduler.Score` both read it, so there is no second copy to drift.

  A WILDCARD is a brick with an empty `size_class` (the legacy DaemonSet, the
  full-node envelope). A CLASSED brick reporting a zero budget is BLIND: its
  cgroup is unreadable (for example a jailer-armed daemon reading its own
  daemon-child cgroup), so nothing can say what it would admit and it is never
  memory-eligible (fail closed).
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
          mem_reserved_mib: non_neg_integer(),
          admits_on_reservation: boolean(),
          vm_overhead_mib: non_neg_integer(),
          admission_reported: boolean(),
          mem_admittable_mib: integer(),
          mem_inflight_mib: non_neg_integer(),
          inflight_vms: non_neg_integer(),
          admission_refusals: %{optional(String.t()) => non_neg_integer()},
          jailer_armed: boolean(),
          launch_modes: %{optional(String.t()) => non_neg_integer()},
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

  @doc """
  Whether a brick is the WILDCARD: an empty size class (the legacy DaemonSet, a
  full-node envelope that can boot any guest). A classed brick with a zero
  budget is NOT a wildcard; see `blind?/1`.
  """
  @spec wildcard?(map()) :: boolean()
  def wildcard?(brick), do: (Map.get(brick, :size_class) || "") == ""

  @doc """
  Whether a CLASSED brick reports no memory budget, so its admission cannot be
  evaluated. Such a brick is never memory-eligible: the fail-open reading it
  used to get ("no budget means unlimited") is exactly how a jailer-armed brick
  whose cgroup readout broke would accept every guest.
  """
  @spec blind?(map()) :: boolean()
  def blind?(brick), do: not wildcard?(brick) and num(brick, :mem_budget_mib) <= 0

  @doc "Whether the brick admits on declared (reserved) memory rather than observed headroom."
  @spec reservation?(map()) :: boolean()
  def reservation?(brick), do: Map.get(brick, :admits_on_reservation) == true

  @doc """
  The largest guest memMib this brick would admit for ONE more VM, before any
  control-plane claim the daemon has not yet seen (`Embervm.Scheduler.Inflight`).

    * `admission_reported`: the daemon's own `mem_admittable_mib`, verbatim. It
      already counts the daemon's in-flight boots and per-VM overhead. Signed: a
      negative value admits nothing, not even a zero-need probe.
    * reserved model (`admits_on_reservation`): `mem_budget_mib -
      mem_reserved_mib - vm_overhead_mib * (live_vms + 1)`. No floor and no
      observed term, matching noded's `memExhausted`.
    * observed model: `mem_headroom_mib - mem_reject_floor_mib`, today's gate.

  Accepts normalized bricks, raw capacity facts and PoolManager instance maps.
  Meaningless for a wildcard or a blind brick; callers gate those first.
  """
  @spec admittable_mib(map()) :: integer()
  def admittable_mib(brick) do
    cond do
      Map.get(brick, :admission_reported) == true ->
        num(brick, :mem_admittable_mib)

      reservation?(brick) ->
        num(brick, :mem_budget_mib) - num(brick, :mem_reserved_mib) -
          num(brick, :vm_overhead_mib) * (num(brick, :live_vms) + 1)

      true ->
        num(brick, :mem_headroom_mib) - num(brick, :mem_reject_floor_mib)
    end
  end

  @doc """
  The per-VM overhead this brick's admission charges: `vm_overhead_mib` under
  the reserved model, 0 under observed. noded reports the configured value in
  both models but charges it only under reserved.
  """
  @spec charged_overhead_mib(map()) :: non_neg_integer()
  def charged_overhead_mib(brick) do
    if reservation?(brick), do: max(num(brick, :vm_overhead_mib), 0), else: 0
  end

  @doc """
  What one more VM of `need_mib` costs this brick's admission: the guest's
  declared memory plus the per-VM overhead the daemon charges
  (`charged_overhead_mib/1`). This is what an in-flight claim debits.
  """
  @spec charge_mib(map(), non_neg_integer() | nil) :: non_neg_integer()
  def charge_mib(brick, need_mib), do: max((need_mib || 0) + charged_overhead_mib(brick), 0)

  @doc """
  Returns the dial key for a brick. Legacy or statically-seeded capacity facts
  carry no `instance_id`, so the `configured_id` is the correct dial-home
  registration id for them.
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

  @doc """
  Normalize one capacity-facts map to a brick. Missing numeric facts, and facts
  PRESENT but holding nil, read as 0 (a daemon that never set a budget field),
  which fail-closed makes that brick simply uncompetitive rather than crashing
  the reader: Elixir orders nil above every number, so a raw nil headroom would
  satisfy every memory need. PoolManager builds its instance maps from this too,
  so every placement path reads the same shape.
  """
  @spec to_brick(map()) :: brick()
  def to_brick(facts) do
    live = num(facts, :live_vms)
    max_live = num(facts, :max_live_vms)

    %{
      node_id: Map.get(facts, :node_id) || "",
      pod_uid: Map.get(facts, :pod_uid) || "",
      instance_id: Map.get(facts, :instance_id) || "",
      size_class: Map.get(facts, :size_class) || "",
      mem_headroom_mib: num(facts, :mem_headroom_mib),
      mem_reject_floor_mib: num(facts, :mem_reject_floor_mib),
      mem_budget_mib: num(facts, :mem_budget_mib),
      mem_reserved_mib: num(facts, :mem_reserved_mib),
      admits_on_reservation: Map.get(facts, :admits_on_reservation) == true,
      vm_overhead_mib: num(facts, :vm_overhead_mib),
      # Admission ledger (NodeStatus 44-48). A daemon that predates them reports
      # admission_reported false, and admittable_mib/1 falls back to the
      # per-model predicate on the facts above.
      admission_reported: Map.get(facts, :admission_reported) == true,
      mem_admittable_mib: num(facts, :mem_admittable_mib),
      mem_inflight_mib: num(facts, :mem_inflight_mib),
      inflight_vms: num(facts, :inflight_vms),
      admission_refusals: Map.get(facts, :admission_refusals) || %{},
      # Jailer facts (NodeStatus 49-50), diagnostic: placement does not read them.
      jailer_armed: Map.get(facts, :jailer_armed) == true,
      launch_modes: Map.get(facts, :launch_modes) || %{},
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

  defp num(map, key) do
    case Map.get(map, key) do
      n when is_integer(n) -> n
      _ -> 0
    end
  end
end
