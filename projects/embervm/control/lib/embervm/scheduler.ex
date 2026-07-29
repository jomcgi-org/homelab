defmodule Embervm.Scheduler do
  @moduledoc """
  Unified placement entry point for brick-aware scheduling.

  `place/1` resolves explicit bricks or reads the capacity table, applies all
  requested filters in node, subnet, slots, memory, and base order, then returns
  the surviving brick maps in `Score` order. `:none` skips the base gate.
  `Brick.free_slots/1` is polymorphic across normalized bricks, raw facts, and
  PoolManager instance maps. A nil memory need skips the memory gate entirely,
  which is intentionally different from a need of zero.
  """

  alias Embervm.Brick
  alias Embervm.NodeCapacity
  alias Embervm.Scheduler.{Request, Score}

  @type brick :: map()

  @spec place(Request.t()) :: [map()]
  def place(%Request{} = req) do
    bricks = req.bricks || Brick.bricks(req.table || NodeCapacity.table())

    bricks
    |> Enum.filter(&node_match?(&1, req.node_id))
    |> Enum.filter(&subnet_match?(&1, req.require_subnet))
    |> Enum.filter(&(Brick.free_slots(&1) > 0))
    |> Enum.filter(&memory_match?(&1, req.need_mib))
    |> Enum.filter(&base_match?(&1, req.base, req.workload))
    |> Score.order(req.key || req.workload)
  end

  @doc """
  Places a cold request and distinguishes capacity from a missing workload base.
  A base-gated miss is retried without the base gate using the same request. An
  An empty resolved universe means the control plane is blind and returns
  `:no_bricks` without recording demand. An empty ungated result from a
  non-empty universe records capacity demand; a non-empty ungated result tells
  the base builder to provision on the brick that would have been selected.
  """
  @spec place_with_demand(Request.t()) ::
          {:ok, [map()]} | {:error, :no_bricks | :capacity | {:base_missing, term()}}
  def place_with_demand(%Request{base: base} = req) when base == :ready or is_tuple(base) do
    bricks = req.bricks || Brick.bricks(req.table || NodeCapacity.table())

    case bricks do
      [] ->
        {:error, :no_bricks}

      _ ->
        case place(%{req | bricks: bricks}) do
          [_ | _] = candidates ->
            {:ok, candidates}

          [] ->
            case place(%{req | bricks: bricks, base: :none}) do
              [] ->
                Embervm.BrickController.note_denial(req.need_mib || 0)
                {:error, :capacity}

              [brick | _] ->
                Embervm.BaseBuilder.note_base_missing(req.workload, Map.get(brick, :configured_id))
                {:error, {:base_missing, Map.get(brick, :configured_id)}}
            end
        end
    end
  end

  @spec eligible?(brick(), non_neg_integer()) :: boolean()
  def eligible?(brick, need_mib), do: Brick.free_slots(brick) > 0 and mem_eligible?(brick, need_mib)

  @doc """
  The memory gate. A WILDCARD is always eligible: a brick under no cgroup limit
  reports `mem_headroom_mib = 0` yet can boot any guest, so gating it on the
  headroom it reports would deny every placement onto it.

  A classed brick needs `headroom >= need + mem_reject_floor_mib`. The floor term
  is not padding: noded admits on need PLUS its own floor, so gating on need
  alone places workloads the node then refuses, forever (issue #4137).
  """
  @spec mem_eligible?(brick(), non_neg_integer()) :: boolean()
  def mem_eligible?(brick, need_mib) do
    Brick.wildcard?(brick) or
      Map.get(brick, :mem_headroom_mib, 0) >= need_mib + Map.get(brick, :mem_reject_floor_mib, 0)
  end

  @doc """
  Whether the brick has ADVERTISED `workload`'s base as ready. Distinct from the
  memory/slot gate: a freshly rolled brick is dispatchable and mem-eligible the
  instant it registers, but cannot resolve `boot_image_ref` until its noded
  re-provisions and advertises the base, so placing there hard-fails the boot.

  Absent workload entry, absent `workloads` map, or any non-READY state is false
  (fail-closed).
  """
  @spec base_ready?(brick(), term()) :: boolean()
  def base_ready?(brick, workload) do
    case Map.get(Map.get(brick, :workloads) || %{}, workload) do
      %{base_state: state} -> base_state_ready?(state)
      _ -> false
    end
  end

  defp node_match?(_brick, nil), do: true
  defp node_match?(brick, node_id), do: Map.get(brick, :node_id) == node_id

  defp subnet_match?(_brick, false), do: true
  defp subnet_match?(brick, true), do: is_binary(Map.get(brick, :serving_subnet_cidr)) and Map.get(brick, :serving_subnet_cidr) != ""

  defp memory_match?(_brick, nil), do: true
  defp memory_match?(brick, need_mib), do: mem_eligible?(brick, need_mib)

  defp base_match?(_brick, :none, _workload), do: true
  defp base_match?(brick, :ready, workload), do: base_ready?(brick, workload)
  defp base_match?(brick, {:ready, field}, workload) do
    base_ready?(brick, workload) and is_binary(get_in(brick, [:workloads, workload, field])) and get_in(brick, [:workloads, workload, field]) != ""
  end

  # The daemon reports base_state as a proto enum. Accept the protobuf-elixir atom
  # form AND the raw integer 3 defensively: both shapes reach us off the wire, and
  # matching only the atom silently reads a READY brick as not-ready.
  defp base_state_ready?(:BASE_BUILD_STATE_READY), do: true
  defp base_state_ready?(3), do: true
  defp base_state_ready?(_), do: false
end
