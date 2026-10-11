defmodule Embervm.Scheduler do
  @moduledoc """
  Unified placement entry point for brick-aware scheduling.

  `place/1` resolves explicit bricks or reads the capacity table, applies all
  requested filters in node, subnet, slots, memory, and base order, then returns
  the surviving brick maps in `Score` order. `:none` skips the base gate.
  `Brick.free_slots/1` is polymorphic across normalized bricks, raw facts, and
  PoolManager instance maps. A nil memory need skips the memory gate entirely,
  which is intentionally different from a need of zero.

  ## in-flight memory

  Before it filters or scores, `place/1` charges every candidate with the memory
  other placements have claimed against it and the brick has not yet reported
  (`Embervm.Scheduler.Inflight.pending_mib/2`, read from `req.inflight`). Both the
  memory gate and the PACK score see that assumed load, so a burst scored on one
  stale `NodeStatus` spreads instead of herding onto one head brick. The bricks
  returned are the caller's own maps, unchanged. `place_and_claim/2` places and
  claims the head in one step, so serial placements inside one process never
  herd.
  """

  alias Embervm.Brick
  alias Embervm.NodeCapacity
  alias Embervm.Scheduler.{Inflight, Request, Score}

  @type brick :: map()

  # Carries each working copy's position in the caller's list through Score.
  @index_key :__place_index__

  @spec place(Request.t()) :: [map()]
  def place(%Request{} = req) do
    bricks = req.bricks || Brick.bricks(req.table || NodeCapacity.table())
    originals = List.to_tuple(bricks)
    pending = pending_fun(req.inflight)

    # Score and filter a working copy carrying the pending debit, then hand back
    # the caller's own maps, so the debit never leaks into a caller's view (or an
    # equality a caller asserts).
    bricks
    |> Enum.with_index()
    |> Enum.map(fn {brick, index} -> {with_pending(brick, pending), index} end)
    |> Enum.filter(fn {b, _} -> node_match?(b, req.node_id) end)
    |> Enum.filter(fn {b, _} -> subnet_match?(b, req.require_subnet) end)
    |> Enum.filter(fn {b, _} -> Brick.free_slots(b) > 0 end)
    |> Enum.filter(fn {b, _} -> memory_match?(b, req.need_mib) end)
    |> Enum.filter(fn {b, _} -> base_match?(b, req.base, req.workload) end)
    |> Enum.map(fn {b, index} -> Map.put(b, @index_key, index) end)
    |> Score.order(req.key || req.workload)
    |> Enum.map(fn b -> elem(originals, Map.fetch!(b, @index_key)) end)
  end

  @doc """
  Places `req` like `place_with_demand/1`, then CLAIMS the first ordered
  candidate that still fits once its pending debit is re-read, under `ref`
  (`Embervm.Scheduler.Inflight.claim/4`, charging `Brick.charge_mib/2`). Returns
  `{:ok, [claimed | rest]}`: the claimed brick heads the list, followed by the
  rest of the frontier after it, which is the shape a `Scheduler.Retry` caller
  walks. The caller must `Inflight.settle/3` or `Inflight.release/3` the claim on
  the brick it finally boots on; every retry hop to another brick claims that
  brick under the same `ref` first.

  `{:error, :capacity}` (with the autoscaler denial, unless `record_demand:
  false`) when every candidate filled up between the read and the claim. Errors
  from `place_with_demand/1` pass through unchanged.
  """
  @spec place_and_claim(Request.t(), term()) ::
          {:ok, [map()]} | {:error, :no_bricks | :capacity | {:base_missing, term()}}
  def place_and_claim(%Request{} = req, ref) do
    with {:ok, candidates} <- place_or_deny(req) do
      case claim_first(candidates, req, ref) do
        {:ok, _} = ok ->
          ok

        :none ->
          if req.record_demand do
            Embervm.BrickController.note_denial(req.workload, req.need_mib || 0)
          end

          {:error, :capacity}
      end
    end
  end

  # place_with_demand/1 is the base-gated entry point; an ungated request gets
  # the same demand signals without the base-missing retry.
  defp place_or_deny(%Request{base: :none} = req) do
    bricks = req.bricks || Brick.bricks(req.table || NodeCapacity.table())

    cond do
      bricks == [] ->
        if req.record_demand, do: Embervm.BrickController.note_empty_demand(req.workload, req.need_mib || 0)
        {:error, :no_bricks}

      (candidates = place(%{req | bricks: bricks})) != [] ->
        {:ok, candidates}

      true ->
        if req.record_demand, do: Embervm.BrickController.note_denial(req.workload, req.need_mib || 0)
        {:error, :capacity}
    end
  end

  defp place_or_deny(req), do: place_with_demand(req)

  defp claim_first([], _req, _ref), do: :none

  defp claim_first([brick | rest], req, ref) do
    opts = inflight_opts(req.inflight)
    fresh = with_pending(brick, pending_fun(req.inflight))

    if req.need_mib == nil or mem_eligible?(fresh, req.need_mib) do
      _ = if opts, do: Inflight.claim(Brick.dial_id(brick), ref, Brick.charge_mib(brick, req.need_mib), opts)
      {:ok, [brick | rest]}
    else
      claim_first(rest, req, ref)
    end
  end

  @doc """
  Places a cold request and distinguishes capacity from a missing workload base.
  A base-gated miss is retried without the base gate using the same request.

  An empty resolved universe returns `:no_bricks`. From here the control plane
  cannot tell whether it is blind (just booted, no brick has dialed home yet) or
  the fleet is genuinely empty (every class was scaled to zero), so it reports
  the miss as UNCONFIRMED demand through
  `Embervm.BrickController.note_empty_demand/2`. The controller owns the
  replica counts and only promotes the miss to a scale-up denial when every
  class that could hold the need reads zero live replicas; while any such
  brick exists or is starting, the miss is dropped, so a blind boot still
  records no demand. Recording nothing at all here deadlocked a fleet drained
  to zero (2026-09-30): nothing could ever scale it back up.

  An empty ungated result from a non-empty universe records capacity demand; a
  non-empty ungated result tells the base builder to provision on the brick that
  would have been selected. `record_demand: false` suppresses both demand
  signals.
  """
  @spec place_with_demand(Request.t()) ::
          {:ok, [map()]} | {:error, :no_bricks | :capacity | {:base_missing, term()}}
  def place_with_demand(%Request{base: base} = req) when base == :ready or is_tuple(base) do
    bricks = req.bricks || Brick.bricks(req.table || NodeCapacity.table())

    case bricks do
      [] ->
        if req.record_demand do
          Embervm.BrickController.note_empty_demand(req.workload, req.need_mib || 0)
        end

        {:error, :no_bricks}

      _ ->
        case place(%{req | bricks: bricks}) do
          [_ | _] = candidates ->
            {:ok, candidates}

          [] ->
            case place(%{req | bricks: bricks, base: :none}) do
              [] ->
                if req.record_demand do
                  Embervm.BrickController.note_denial(req.workload, req.need_mib || 0)
                end

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
  The memory gate. A WILDCARD (empty size class) is always eligible: a brick
  under no cgroup limit reports `mem_headroom_mib = 0` yet can boot any guest, so
  gating it on the headroom it reports would deny every placement onto it.

  A BLIND classed brick (`Brick.blind?/1`, a zero budget) is never eligible: it
  cannot say what it would admit, so it fails closed.

  Any other brick needs `Brick.admittable_mib(brick) - pending_mib >= need`,
  where `admittable_mib/1` is the brick's own admission predicate (the daemon's
  reported `mem_admittable_mib`, or the per-model fallback; the observed one
  keeps the `mem_reject_floor_mib` term, because noded admits on need PLUS its
  floor and gating on need alone places workloads the node then refuses forever,
  issue #4137) and `:pending_mib` is the in-flight debit `place/1` charges
  (`Embervm.Scheduler.Inflight.debit_mib/3`: the control plane's claims the
  brick's report does not already count; absent reads as 0).
  """
  @spec mem_eligible?(brick(), non_neg_integer()) :: boolean()
  def mem_eligible?(brick, need_mib) do
    cond do
      Brick.wildcard?(brick) -> true
      Brick.blind?(brick) -> false
      true -> Brick.admittable_mib(brick) - pending_of(brick) >= need_mib
    end
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

  # The in-flight debit lookup for one request. `:default` reads the shared
  # ledger, an atom names a private table (tests), `false` disables the debit.
  defp pending_fun(false), do: fn _dial, _brick -> 0 end

  defp pending_fun(inflight) do
    opts = inflight_opts(inflight)
    fn dial, brick -> Inflight.debit_mib(dial, brick, opts) end
  end

  defp inflight_opts(false), do: nil
  defp inflight_opts(:default), do: [table: Inflight.table()]
  defp inflight_opts(table) when is_atom(table), do: [table: table]

  defp with_pending(brick, pending) do
    case pending.(Brick.dial_id(brick), brick) do
      0 -> brick
      mib -> Map.put(brick, :pending_mib, pending_of(brick) + mib)
    end
  end

  defp pending_of(brick) do
    case Map.get(brick, :pending_mib) do
      n when is_integer(n) and n > 0 -> n
      _ -> 0
    end
  end

  # The daemon reports base_state as a proto enum. Accept the protobuf-elixir atom
  # form AND the raw integer 3 defensively: both shapes reach us off the wire, and
  # matching only the atom silently reads a READY brick as not-ready.
  @doc "Whether a daemon base state uses either supported READY representation."
  @spec base_state_ready?(term()) :: boolean()
  def base_state_ready?(:BASE_BUILD_STATE_READY), do: true
  def base_state_ready?(3), do: true
  def base_state_ready?(_), do: false
end
