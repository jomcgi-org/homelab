defmodule Embervm.StatefulHandover do
  @moduledoc """
  Manual, control-plane-sequenced move of a BANKED stateful workload's volume
  from its current anchor node onto a peer (#4119 slice 4).

  ## Why this exists

  A stateful workload is pinned to its volume's anchor node by the single-writer
  fence: `volume.Manager` permits one writable attach, and the control plane
  never places a stateful wake anywhere but the anchor. That pin is correct for
  safety and total for availability. When the anchor node cannot host the wake,
  placement has exactly ONE candidate and retries it forever, which is the
  never-converging loop behind demo-postgres sitting unwakeable while a peer
  brick sat idle. Moving the anchor is the only thing that breaks that tie.

  ## What it is NOT

  Not live migration, and deliberately not overlapped: the workload is banked
  before the move and cold-boots on the target afterwards, so it is unreachable
  between its final bank on the source and its first wake on the target. That
  relaxation ("slow cutover due to activity is fine") is what lets the transfer
  be a plain store round-trip instead of a pre-staged standby costing double
  disk and continuous refresh reads on the hot-path NVMe.

  Volume-only by design. The stateful bundle is vendor-BOUND while the volume is
  a vendor-portable singleton, so moving only the volume is the mode that works
  across any pair of nodes; the target cold-boots from its own base rather than
  relighting a foreign memory image. #4119 names this as the cross-vendor
  behaviour, and taking it as the ONLY behaviour keeps the first version free of
  vendor-matching logic that a homogeneous fleet would never exercise.

  ## Ordering, and why it is this order

  Record before dispatch, then export, restore, re-anchor, evict. The invariant
  the order protects is that **at no instant does a node other than the anchor
  hold a writable claim**, and that a crash at any step leaves at most one
  legitimate copy:

    * export before restore, so the target reads bytes that exist;
    * restore before re-anchor, so the anchor never names a node without data;
    * re-anchor before evict, so the source copy outlives the switch and a crash
      mid-move is recoverable by pointing the anchor back.

  A crash between restore and re-anchor leaves a harmless extra copy on the
  target with the anchor still on the source: the workload keeps working and the
  leftover is exactly what #4119 slice 3's reconcile is for.

  ## The export gate is load-bearing

  The export refuses an unblessed generation (ADR embervm/011, enforced on both
  the sync verb and the async queue). A move is the first moment an exported
  copy becomes the AUTHORITATIVE one on another node, so a refused export must
  abort the move rather than be logged past: restoring an unblessed generation
  onto a peer and then pointing the anchor at it is precisely how a self-bumped
  writer's divergent state would win. `{:error, :source_export_refused}` is a
  correctness outcome here, not a transport problem.
  """

  require Logger

  alias Embervm.Node.V1.ArtifactRef
  alias Embervm.Node.V1.EvictArtifactRequest
  alias Embervm.Node.V1.ExportArtifactRequest
  alias Embervm.Node.V1.RestoreArtifactRequest
  alias Embervm.Node.V1.Trace
  alias Embervm.NodeCapacity
  alias Embervm.OpLog.Op
  alias Embervm.StatefulStore

  @typedoc "Outcome detail for a completed move."
  @type result :: %{
          workload: String.t(),
          from: String.t(),
          to: String.t(),
          generation: non_neg_integer(),
          source_evicted: boolean()
        }

  @doc """
  Move `workload`'s volume from its current anchor onto `target_node`.

  Refuses unless the workload is fully banked: a live or in-flight instance
  means someone holds a writable attach, and moving the volume out from under it
  is the split brain the fence exists to prevent.

  Options exist for the test seams (`:store`, `:capacity_table`, `:op_log`,
  `:append_fun`, `:channel_fun`, `:export_fun`, `:restore_fun`, `:evict_fun`,
  `:tenant`, `:clock`); production supplies none of them.
  """
  @spec move(String.t(), String.t(), keyword()) :: {:ok, result()} | {:error, term()}
  def move(workload, target_node, opts \\ [])
      when is_binary(workload) and is_binary(target_node) do
    ctx = context(opts)

    with {:ok, volume} <- fetch_volume(ctx, workload),
         {:ok, source} <- anchor_of(volume),
         :ok <- refuse_same_node(source, target_node),
         :ok <- refuse_unless_banked(ctx, workload),
         {:ok, source_dial} <- dial_key(ctx, source),
         {:ok, target_dial} <- dial_key(ctx, target_node) do
      run(ctx, workload, source, target_node, source_dial, target_dial)
    end
  end

  # ---- sequence ---------------------------------------------------------------

  defp run(ctx, workload, source, target, source_dial, target_dial) do
    append_op(ctx, :stateful_handover_started, %{
      workload: workload,
      from: source,
      to: target
    })

    with {:ok, generation} <- export_source(ctx, workload, source_dial),
         :ok <- restore_target(ctx, workload, target_dial) do
      # Re-anchor. From here the workload's next wake plans against the target,
      # and StatefulManager issues a fresh blessed generation on that wake, so
      # nothing needs to pre-bless the restored copy.
      :ok = reanchor(ctx, workload, target)

      evicted = evict_source(ctx, workload, source_dial)

      append_op(ctx, :stateful_handover_finished, %{
        workload: workload,
        from: source,
        to: target,
        generation: generation,
        source_evicted: evicted
      })

      Logger.info("embervm stateful handover: volume moved",
        workload: workload,
        from: source,
        to: target,
        generation: generation,
        source_evicted: evicted
      )

      {:ok,
       %{
         workload: workload,
         from: source,
         to: target,
         generation: generation,
         source_evicted: evicted
       }}
    else
      {:error, reason} = error ->
        append_op(ctx, :stateful_handover_aborted, %{
          workload: workload,
          from: source,
          to: target,
          reason: inspect(reason)
        })

        Logger.warning("embervm stateful handover: aborted, anchor unchanged",
          workload: workload,
          from: source,
          to: target,
          reason: inspect(reason)
        )

        error
    end
  end

  # Flush the source's copy to the store at its current generation. `skipped`
  # means the daemon refused: either an unblessed generation (ADR 011) or a
  # store copy that is already newer (#4111's ordering fence). Both mean the
  # bytes a restore would read are NOT the ones this node holds, so the move
  # must not proceed.
  defp export_source(ctx, workload, source_dial) do
    req = %ExportArtifactRequest{
      artifact: volume_ref(workload),
      trace: %Trace{workload: workload}
    }

    case rpc(ctx, ctx.export_fun, source_dial, req) do
      {:ok, %{skipped: true}} ->
        {:error, :source_export_refused}

      {:ok, resp} ->
        {:ok, Map.get(resp, :generation, 0)}

      {:error, reason} ->
        {:error, {:export_failed, reason}}
    end
  end

  defp restore_target(ctx, workload, target_dial) do
    req = %RestoreArtifactRequest{
      artifact: volume_ref(workload),
      trace: %Trace{workload: workload}
    }

    case rpc(ctx, ctx.restore_fun, target_dial, req) do
      {:ok, _resp} -> :ok
      {:error, reason} -> {:error, {:restore_failed, reason}}
    end
  end

  defp reanchor(ctx, workload, target) do
    _ = StatefulStore.upsert_volume(ctx.store, workload, %{node_id: target})
    :ok
  end

  # Best-effort, and deliberately non-fatal. EvictArtifact refuses a volume that
  # is still paired with a local stateful bundle (standing decision 8), which is
  # the common case immediately after a bank, so making this fatal would fail
  # most otherwise-correct moves. The anchor has already moved, so the leftover
  # is a stale copy rather than a competing writer; #4119 slice 3's reconcile is
  # what converges it. Reported in the op and the result so it is never silent.
  defp evict_source(ctx, workload, source_dial) do
    req = %EvictArtifactRequest{
      artifact: volume_ref(workload),
      remote: false,
      trace: %Trace{workload: workload}
    }

    case rpc(ctx, ctx.evict_fun, source_dial, req) do
      {:ok, _resp} ->
        true

      {:error, reason} ->
        Logger.info("embervm stateful handover: source copy left in place",
          workload: workload,
          reason: inspect(reason)
        )

        false
    end
  end

  # ---- guards -----------------------------------------------------------------

  defp fetch_volume(ctx, workload) do
    case StatefulStore.get_volume(ctx.store, workload) do
      nil -> {:error, :no_volume}
      volume -> {:ok, volume}
    end
  end

  defp anchor_of(%{node_id: node_id}) when is_binary(node_id) and node_id != "",
    do: {:ok, node_id}

  defp anchor_of(_), do: {:error, :volume_node_missing}

  defp refuse_same_node(source, target) when source == target, do: {:error, :already_anchored}
  defp refuse_same_node(_source, _target), do: :ok

  # "Banked" means no LIVE instance. StatefulStore maintains `live` as
  # "non-terminal, non-banked", which is exactly the set that could be holding a
  # writable attach, so reading the counter beats re-deriving which states are
  # terminal. `list/2` would be the WRONG source here: it returns terminal
  # history alongside the current instance, so filtering it for "everything is
  # banked" would refuse every workload that has ever stopped an instance.
  defp refuse_unless_banked(ctx, workload) do
    case ctx.store |> StatefulStore.counts() |> Map.get(workload) do
      nil -> :ok
      %{live: 0} -> :ok
      %{live: live} -> {:error, {:not_banked, live}}
    end
  end

  # A VOLUME lives on the node's shared scratch, so ANY reporting brick instance
  # on that node can serve the transfer. NodeCapacity's bare-string form is the
  # node-scoped read built for exactly this ("volumes are NODE resources, not pod
  # resources"), and it doubles as a liveness check: an unreported node is one
  # whose daemon cannot move bytes.
  #
  # Deliberately NOT WakeInstance.select/2, the resolver the wake path uses. That
  # one filters candidates on VM capacity, which is right when the caller is
  # about to boot a VM and exactly wrong here: the SOURCE of a handover is very
  # often a node that is out of VM slots (that being the reason to move at all),
  # and it needs to push bytes, not host a guest. Selecting on capacity would
  # refuse precisely the moves worth making.
  #
  # `configured_id` first because that is the key NodeChannel is configured with
  # (`nodes: configured_nodes()`), so it is the name a dial resolves; node_id is
  # the fallback for facts that predate the field.
  defp dial_key(ctx, node_id) do
    case NodeCapacity.fetch(ctx.capacity_table, node_id) do
      {:ok, facts} -> {:ok, Map.get(facts, :configured_id) || node_id}
      :error -> {:error, {:node_not_reporting, node_id}}
    end
  end

  # ---- plumbing ---------------------------------------------------------------

  defp volume_ref(workload) do
    %ArtifactRef{kind: :ARTIFACT_KIND_VOLUME, workload: workload}
  end

  defp rpc(ctx, fun, dial_id, req) do
    case ctx.channel_fun.(dial_id) do
      {:ok, channel} -> fun.(channel, req)
      {:error, reason} -> {:error, {:dial_failed, reason}}
    end
  rescue
    e -> {:error, {:rpc_raised, inspect(e)}}
  end

  defp append_op(ctx, kind, payload) do
    op = %Op{kind: kind, tenant: ctx.tenant, ts: ctx.clock.(), payload: payload}
    _ = ctx.append_fun.(ctx.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm stateful handover: op-log append raised",
        kind: kind,
        error: inspect(e)
      )

      :ok
  end

  defp context(opts) do
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)

    %{
      store: Keyword.get(opts, :store, StatefulStore),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      op_log: Keyword.get(opts, :op_log, op_log_mod),
      append_fun: Keyword.get(opts, :append_fun, &default_append/2),
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      export_fun: Keyword.get(opts, :export_fun, &default_export/2),
      restore_fun: Keyword.get(opts, :restore_fun, &default_restore/2),
      evict_fun: Keyword.get(opts, :evict_fun, &default_evict/2),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      clock: Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end)
    }
  end

  defp default_append(op_log, op), do: Embervm.OpLog.SQLite.append(op_log, op)

  defp default_export(channel, req),
    do: Embervm.Node.V1.NodeService.Stub.export_artifact(channel, req)

  defp default_restore(channel, req),
    do: Embervm.Node.V1.NodeService.Stub.restore_artifact(channel, req)

  defp default_evict(channel, req),
    do: Embervm.Node.V1.NodeService.Stub.evict_artifact(channel, req)
end
