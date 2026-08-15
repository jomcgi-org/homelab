defmodule Embervm.WarmthReaper do
  @moduledoc """
  The reconciled warmth-retention sweep for STATEFUL bundles (`stateful/<ref>`)
  and GROUP bundle SETS (`group/<set_id>/`), the exact structural analogue of the
  BaseBuilder base-retention sweep (base-durability PR-3) extended to the two
  warmth kinds that leak identically.

  ## why a reconciled sweep, and why a NEW module

  `Embervm.StatefulSweeper` and `Embervm.GroupSweeper` already GC warmth, but they
  are EVENT-DRIVEN bankers: they evict a bundle on an FSM trigger (banked-TTL,
  broken-pair, partial-set, lifetime) for an instance the control plane STILL
  TRACKS. They can never reclaim an artifact the CP no longer tracks AT ALL, which
  is the whole leak: 143 orphaned `state-*` bundles (dead demo-pg instances whose
  CP row is terminal or gone) and 5 leaked `set-grp-*` sets. Those never fire a
  per-instance FSM trigger because there is no live instance to fire it.

  This reaper is the reconciled backstop, exactly like the base sweep: it
  enumerates from the node's REPORTED on-disk inventory (NodeStatus.stateful_bundles
  and NodeStatus.group_bundle_sets, projected into the shared NodeCapacity table by
  Embervm.NodeRegistry) and evicts every reported artifact NOT in the CP's desired
  set. It is a separate GenServer, not a fourth arm bolted onto the two sweepers,
  because its enumeration axis is the node inventory (reconcile), not the CP
  instance list (bank), so its shape is the base sweep's, not the sweepers'.

  ## desired set = non-terminal CP instances

  The desired set is the warmth held by any instance the CP tracks as NON-TERMINAL
  (live OR banked). For stateful that is every `StatefulStore.all/1` instance whose
  state is not `StatefulState.terminal?/1`, keyed by its `snapshot_ref`. For groups
  it is every `GroupStore.all/1` instance not `GroupState.terminal?/1`, keyed by its
  `set_id`. A reported artifact whose ref/set_id is in the desired set is NEVER a
  candidate: it is a live or banked instance's current warmth. Everything else is an
  ORPHAN.

  ## retention policy (Joe, 2026-07-21): disk N=1 / S3 N=2, orphans evicted ENTIRELY

  The retention shape is the same shallow-history model as the bases: keep the
  CURRENT snapshot on disk (N=1) and current+previous in S3 (N=2), no time travel.
  For stateful, disk N=1 is already an invariant of the bank path (noded's
  `RemoveStatefulBundle` drops any prior bundle before publishing a new one, so a
  workload only ever has ONE `stateful/<ref>` dir on disk); the S3 N=2 predecessor
  retention lives in the export path, not here. What this reaper adds is the missing
  ORPHAN reclaim: an instance the CP no longer tracks as non-terminal has NO ongoing
  durability requirement (the demo pg is wiped, ephemeral), so its warmth is evicted
  ENTIRELY, local AND remote (both S3 copies), reclaiming the disk cache and the
  object store together. Groups are the same: a `set-grp-*` set whose group instance
  is terminal/gone is evicted whole.

  ## guards (identical intent to the base sweep)

    * durability-before-eviction: an orphan is safe to remote-evict (its instance is
      gone, nothing will ever relight it), so no export floor gates an orphan. The
      base sweep's `skip_unexported` guard protects a workload's CURRENT base before
      trimming its superseded siblings; here the analogue is simply that a DESIRED
      (non-terminal) instance's warmth is never a candidate, so its durability is
      never at risk regardless of its exported flag. We never evict a desired ref.
    * in-use: noded's own EvictSnapshot / EvictArtifact refuses (FailedPrecondition,
      idempotent) a ref a live VM was restored from. That is the final backstop
      beneath the CP-side desired-set exclusion, exactly as for bases.

  ## the gate (mandatory off-by-default, mirrors EMBERVM_BASE_RETENTION_SWEEP)

  Gated behind `EMBERVM_WARMTH_RETENTION_SWEEP`, read in application.ex. When FALSE
  (the default, and what this PR ships) the sweep computes the plan and LOGS a
  per-kind dry-run line ("WOULD evict N ... (~BYTES bytes)") but deletes NOTHING.
  When TRUE it fires the idempotent evictions. Merging this PR is INERT: the timer
  runs but only logs, so the flip is a later deploy-values change with no code
  change, exactly like the base sweep.
  """

  use GenServer
  require Logger

  alias Embervm.{GroupState, GroupStore, NodeCapacity, S3Client, StatefulState, StatefulStore, WakeInstance}

  alias Embervm.Node.V1.{
    ArtifactRef,
    EvictArtifactRequest,
    EvictSnapshotRequest,
    NodeService,
    Trace
  }

  # How often the reconciled warmth-retention sweep runs. Like the base retention
  # sweep it is a slow-moving reconcile (an orphan is born only when an instance
  # terminalizes), never a hot path, so 300s is ample; the guarded, idempotent
  # evictions make a re-sweep harmless.
  @sweep_interval_ms 300_000

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Run one reconciled warmth-retention sweep synchronously and return the plan (the
  list of per-artifact `%{kind, id, workload, node_id, evict_bytes}` entries the
  sweep WOULD evict, or DID evict with the gate on). Lets a test drive the sweep
  deterministically without the timer, and an operator preview (gate off) what it
  would reclaim. Mirrors `BaseBuilder.retention_sweep_now/1`.
  """
  @spec sweep_now(GenServer.server()) :: [map()]
  def sweep_now(server \\ __MODULE__) do
    GenServer.call(server, :sweep_now)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    # Injected seams (defaults are the shared capacity table, the real stores, the
    # real NodeChannel dial, and the real gRPC stubs). Tests inject fakes.
    capacity_table = Keyword.get(opts, :capacity_table, NodeCapacity.table())
    stateful_store = Keyword.get(opts, :stateful_store, StatefulStore)
    group_store = Keyword.get(opts, :group_store, GroupStore)
    channel_fun = Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1)
    evict_snapshot_fun = Keyword.get(opts, :evict_snapshot_fun, &default_evict_snapshot/2)
    evict_artifact_fun = Keyword.get(opts, :evict_artifact_fun, &default_evict_artifact/2)
    remote_stateful_index_fun =
      Keyword.get(opts, :remote_stateful_index_fun, &default_remote_stateful_index/0)

    remote_group_index_fun =
      Keyword.get(opts, :remote_group_index_fun, &default_remote_group_index/0)

    # 0 disables the timer entirely (the unit-test default, so a test drives
    # :sweep_now explicitly and asserts deterministically); production uses the
    # module default.
    sweep_interval_ms = Keyword.get(opts, :sweep_interval_ms, @sweep_interval_ms)

    # The destructive gate. FALSE (the default, and what this PR ships) => the sweep
    # computes and LOGS a per-kind dry-run line but deletes NOTHING. TRUE => it fires
    # the idempotent evictions. application.ex reads it from
    # EMBERVM_WARMTH_RETENTION_SWEEP so it flips via deploy values, no code change.
    sweep_enabled = Keyword.get(opts, :sweep_enabled, false)

    state = %{
      capacity_table: capacity_table,
      stateful_store: stateful_store,
      group_store: group_store,
      channel_fun: channel_fun,
      evict_snapshot_fun: evict_snapshot_fun,
      evict_artifact_fun: evict_artifact_fun,
      remote_stateful_index_fun: remote_stateful_index_fun,
      remote_group_index_fun: remote_group_index_fun,
      sweep_interval_ms: sweep_interval_ms,
      sweep_enabled: sweep_enabled
    }

    schedule_sweep(state)
    {:ok, state}
  end

  @impl true
  def handle_call(:sweep_now, _from, state) do
    plan = sweep_plan(state)
    apply_plan(state, plan)
    {:reply, plan, state}
  end

  @impl true
  def handle_info(:sweep, state) do
    plan = sweep_plan(state)
    apply_plan(state, plan)
    schedule_sweep(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- sweep plan --------------------------------------------------------------

  # Build the per-artifact eviction plan by reconciling every node's reported
  # warmth inventory against the CP's desired set. A plan entry names ONE orphaned
  # artifact (a stateful bundle or a whole group set) with its owning node, its
  # kind-specific id, the workload it namespaces under, and its byte size. The plan
  # is returned for tests/visibility; apply_plan/2 does the gated action.
  defp sweep_plan(state) do
    stateful_desired = stateful_desired_refs(state)
    group_desired = group_desired_set_ids(state)

    facts = NodeCapacity.all(state.capacity_table)

    stateful_orphans =
      for fact <- facts,
          node_id = Map.get(fact, :node_id),
          is_binary(node_id),
          bundle <- Map.get(fact, :stateful_bundles, []) || [],
          is_binary(bundle.snapshot_ref),
          bundle.snapshot_ref != "",
          bundle.snapshot_ref not in stateful_desired do
        %{
          kind: :stateful,
          id: bundle.snapshot_ref,
          workload: bundle.workload,
          node_id: node_id,
          evict_bytes: bundle.size_bytes || 0
        }
      end

    group_orphans =
      for fact <- facts,
          node_id = Map.get(fact, :node_id),
          is_binary(node_id),
          set <- Map.get(fact, :group_bundle_sets, []) || [],
          is_binary(set.set_id),
          set.set_id != "",
          set.set_id not in group_desired do
        %{
          kind: :group,
          id: set.set_id,
          workload: set.group_instance_id,
          node_id: node_id,
          member_refs: for(m <- set.members || [], is_binary(m.snapshot_ref), do: m.snapshot_ref),
          evict_bytes: (set.members || []) |> Enum.map(&(&1.size_bytes || 0)) |> Enum.sum()
        }
      end

    stateful_orphans ++ group_orphans
  end

  # The desired STATEFUL refs: the snapshot_ref of every non-terminal instance the
  # CP tracks (live OR banked). A banked instance's bundle is its warmth-to-relight,
  # so it stays; a terminal (evicted/destroyed/failed) instance holds none. A
  # nil/"" snapshot_ref (a live instance not yet banked) contributes nothing to the
  # protected set (there is no on-disk bundle keyed by it to protect).
  defp stateful_desired_refs(state) do
    for %{state: st, snapshot_ref: ref} <- StatefulStore.all(state.stateful_store),
        not StatefulState.terminal?(st),
        is_binary(ref),
        ref != "",
        into: MapSet.new(),
        do: ref
  end

  # The desired GROUP set_ids: the set_id of every non-terminal group instance the
  # CP tracks. A banked group's set is its warmth; a terminal group holds none. A
  # nil set_id (a group whose partial set was already cleared, or a fresh_booting
  # group with no bank yet) contributes nothing.
  defp group_desired_set_ids(state) do
    for %{state: st, set_id: set_id} <- GroupStore.all(state.group_store),
        not GroupState.terminal?(st),
        is_binary(set_id),
        set_id != "",
        into: MapSet.new(),
        do: set_id
  end

  # -- apply (gated) -----------------------------------------------------------

  # Apply the gated retention action. Gate OFF: log ONE per-kind dry-run line
  # (count + total bytes) and change nothing. Gate ON: fire the idempotent
  # evictions (noded's in-use/BUILDING guard is the final backstop) and log the
  # reclaim. An empty plan logs nothing.
  defp apply_plan(_state, []), do: :ok

  defp apply_plan(state, plan) do
    {stateful, group} = Enum.split_with(plan, &(&1.kind == :stateful))
    stateful_index = remote_stateful_index(state, stateful)
    group_index = remote_group_index(state, group)

    log_and_evict(state, :stateful, stateful, stateful_index)
    log_and_evict(state, :group, group, group_index)
    :ok
  end

  defp log_and_evict(_state, _kind, [], _index), do: :ok

  defp log_and_evict(state, kind, entries, index) do
    count = length(entries)
    bytes = entries |> Enum.map(& &1.evict_bytes) |> Enum.sum()

    if state.sweep_enabled do
      Logger.info(
        "embervm warmth reaper: warmth-retention sweep evicting #{count} orphaned #{kind} warmth artifact(s) (~#{bytes} bytes)"
      )

      Enum.each(entries, &evict_entry(state, &1, index))
    else
      Logger.info(
        "embervm warmth reaper: warmth-retention sweep (DRY RUN, gate off) WOULD evict #{count} orphaned #{kind} warmth artifact(s) (~#{bytes} bytes)"
      )

      Enum.each(entries, &log_dry_run_entry(&1, index))
    end

    :ok
  end

  # Evict one orphaned artifact ENTIRELY (local disk AND remote S3), fire-and-forget
  # in a spawned worker so a blocking RPC never freezes this GenServer. The instance
  # is gone (orphan), so both copies go (Joe's "orphaned instances evicted
  # entirely"). Every eviction is idempotent on an already-absent artifact.
  #
  # The remote (S3) copy is deleted ONLY when the local eviction succeeded (#38): a
  # local FAILED_PRECONDITION (a live VM was relit from the ref) or any local error
  # SKIPS the remote, so the reaper never destroys the off-node recovery copy of a
  # bundle the node itself refused to remove. See run_evict/2 for the ordering.
  #
  # STATEFUL: local is EvictArtifact{remote: false, kind: STATEFUL, ref}, which noded
  # routes to its stateful arm (evictStatefulSnapshot -> RemoveStatefulBundle(ref) ->
  # os.RemoveAll(stateful/<ref>)); remote is the same ref with remote: true (deletes
  # the S3 stateful/<vendor>/<workload>/<ref> prefix), fired only on local success.
  #
  # GROUP: local set eviction is PER-MEMBER (noded refuses a set-level local evict as
  # Unimplemented; a set is evicted locally by EvictSnapshot on each member's
  # snapshot_ref, which noded routes to its group-member arm ->
  # RemoveGroupMemberBundle(set_id, member_name), the R5 contract the GroupSweeper
  # uses); the single remote EvictArtifact{remote: true, kind: GROUP_SET, workload:
  # group_instance_id, ref: set_id} that drops the whole set prefix at once fires
  # ONLY when EVERY member's local eviction succeeded.
  # Empty on-disk group_instance_id bindings are recovered from a remote GROUP_SET
  # index keyed by set_id. The indexed value supplies the vendor and
  # group_instance_id needed to address the remote prefix safely.

  defp evict_entry(state, %{kind: :stateful, id: ref, workload: workload} = entry, {:ok, index})
       when workload in [nil, ""] do
    case Map.get(index, ref) do
      %{vendor: vendor, workload: recovered_workload} ->
        Logger.info(
          "embervm warmth reaper: recovered binding for orphaned stateful bundle #{inspect(ref)} from remote index (vendor #{inspect(vendor)}, workload #{inspect(recovered_workload)})"
        )

        evict_stateful(state, %{entry | workload: recovered_workload}, vendor)

      nil ->
        Logger.info(
          "embervm warmth reaper: evicting local-only orphaned stateful bundle #{inspect(ref)} (not found in remote index; no remote copy can be stranded)"
        )

        evict_stateful(state, entry, nil, false)
    end
  end

  defp evict_entry(_state, %{kind: :stateful, id: ref, workload: workload}, {:error, reason})
       when workload in [nil, ""] do
    Logger.info(
      "embervm warmth reaper: SKIP orphaned stateful bundle #{inspect(ref)} (remote index lookup failed: #{inspect(reason)}; leaving whole)"
    )

    :ok
  end

  defp evict_entry(state, %{kind: :group, id: set_id, workload: gid} = entry, {:ok, index})
       when gid in [nil, ""] do
    case Map.get(index, set_id) do
      %{vendor: vendor, group_instance_id: recovered_gid} ->
        Logger.info(
          "embervm warmth reaper: recovered binding for orphaned group set #{inspect(set_id)} from remote index (vendor #{inspect(vendor)}, group_instance_id #{inspect(recovered_gid)})"
        )

        evict_group(state, %{entry | workload: recovered_gid}, vendor)

      nil ->
        Logger.info(
          "embervm warmth reaper: evicting local-only orphaned group set #{inspect(set_id)} (not found in remote index; no remote copy can be stranded)"
        )

        evict_group(state, entry, nil, false)
    end
  end

  defp evict_entry(_state, %{kind: :group, id: set_id, workload: gid}, {:error, reason})
       when gid in [nil, ""] do
    Logger.info(
      "embervm warmth reaper: SKIP orphaned group set #{inspect(set_id)} (remote index lookup failed: #{inspect(reason)}; leaving whole)"
    )

    :ok
  end

  defp evict_entry(state, %{kind: :stateful} = entry, _index), do: evict_stateful(state, entry)

  defp evict_stateful(state, %{id: ref, workload: workload, node_id: node_id}, vendor \\ nil, remote \\ true) do
    dial_key = WakeInstance.dial_for_bundle(state.capacity_table, node_id, ref)
    artifact = %ArtifactRef{kind: :ARTIFACT_KIND_STATEFUL, workload: workload, ref: ref}

    local = {:artifact, %EvictArtifactRequest{artifact: artifact, remote: false, trace: %Trace{workload: workload}}}
    remote_request = %EvictArtifactRequest{artifact: artifact, remote: true, vendor: vendor || "", trace: %Trace{workload: workload}}

    spawn(fn ->
      with {:ok, channel} <- safe_channel(state, dial_key) do
        # Remote (S3) evict fires ONLY if the local disk evict succeeded: never
        # delete the recovery copy of a bundle the node refused/failed to remove.
        run_evict(state, channel, [local], if(remote, do: {:artifact, remote_request}, else: nil))
        release_channel(state, channel)
      end
    end)

    :ok
  end

  defp evict_entry(state, %{kind: :group} = entry, _index), do: evict_group(state, entry)

  defp evict_group(
         state,
         %{id: set_id, workload: group_instance_id, node_id: node_id, member_refs: member_refs},
         vendor \\ nil,
         remote \\ true
       ) do
    dial_key = WakeInstance.dial_for_group(state.capacity_table, node_id, group_instance_id)

    remote_request = %EvictArtifactRequest{
      artifact: %ArtifactRef{kind: :ARTIFACT_KIND_GROUP_SET, workload: group_instance_id, ref: set_id},
      remote: true,
      vendor: vendor || "",
      trace: %Trace{workload: group_instance_id}
    }

    per_member = for ref <- member_refs, do: {:snapshot, %EvictSnapshotRequest{trace: %Trace{}, snapshot_ref: ref}}

    spawn(fn ->
      with {:ok, channel} <- safe_channel(state, dial_key) do
        # The single remote GROUP_SET evict (drops the whole S3 set prefix at once)
        # fires ONLY if EVERY member's local evict succeeded: one refused/failed
        # member (e.g. a live relit member) leaves the set's recovery copy intact.
        run_evict(state, channel, per_member, if(remote, do: {:artifact, remote_request}, else: nil))
        release_channel(state, channel)
      end
    end)

    :ok
  end

  defp remote_stateful_index(_state, []), do: {:ok, %{}}

  defp remote_stateful_index(state, entries) do
    if Enum.any?(entries, &(&1.workload in [nil, ""])) do
      state.remote_stateful_index_fun.()
    else
      {:ok, %{}}
    end
  rescue
    e -> {:error, {:lookup_raised, e}}
  catch
    kind, reason -> {:error, {:lookup_thrown, {kind, reason}}}
  end

  defp remote_group_index(_state, []), do: {:ok, %{}}

  defp remote_group_index(state, entries) do
    if Enum.any?(entries, &(&1.workload in [nil, ""])) do
      state.remote_group_index_fun.()
    else
      {:ok, %{}}
    end
  rescue
    e -> {:error, {:lookup_raised, e}}
  catch
    kind, reason -> {:error, {:lookup_thrown, {kind, reason}}}
  end

  defp log_dry_run_entry(%{kind: :stateful, id: ref, workload: workload}, {:ok, index})
       when workload in [nil, ""] do
    action = if Map.has_key?(index, ref), do: "local and remote", else: "local-only"
    Logger.info("embervm warmth reaper: DRY RUN WOULD evict #{action} for empty-binding stateful bundle #{inspect(ref)}")
  end

  defp log_dry_run_entry(%{kind: :stateful, id: ref, workload: workload}, {:error, reason})
       when workload in [nil, ""] do
    Logger.info("embervm warmth reaper: DRY RUN WOULD skip empty-binding stateful bundle #{inspect(ref)} because remote index lookup failed: #{inspect(reason)}")
  end

  defp log_dry_run_entry(%{kind: :group, id: set_id, workload: gid}, {:ok, index})
       when gid in [nil, ""] do
    action = if Map.has_key?(index, set_id), do: "local and remote", else: "local-only"
    Logger.info("embervm warmth reaper: DRY RUN WOULD evict #{action} for empty-binding group set #{inspect(set_id)}")
  end

  defp log_dry_run_entry(%{kind: :group, id: set_id, workload: gid}, {:error, reason})
       when gid in [nil, ""] do
    Logger.info("embervm warmth reaper: DRY RUN WOULD skip empty-binding group set #{inspect(set_id)} because remote index lookup failed: #{inspect(reason)}")
  end

  defp log_dry_run_entry(_entry, _index), do: :ok

  defp default_remote_stateful_index do
    endpoint = System.get_env("EMBERVM_STORE_ENDPOINT", "") |> String.trim()
    bucket = System.get_env("EMBERVM_STORE_BUCKET", "embervm")

    case S3Client.new(endpoint, bucket) do
      nil -> {:error, :store_disabled}
      client ->
        with {:ok, entries} <- S3Client.list_all(client, "stateful/") do
          Enum.reduce_while(entries, {:ok, %{}}, fn %{key: key}, {:ok, index} ->
            case String.split(key, "/") do
              ["stateful", vendor, workload, ref, file]
              when vendor != "" and workload != "" and ref != "" and file != "" ->
                binding = %{vendor: vendor, workload: workload}

                case Map.get(index, ref) do
                  nil -> {:cont, {:ok, Map.put(index, ref, binding)}}
                  ^binding -> {:cont, {:ok, index}}
                  _ -> {:halt, {:error, {:conflicting_bindings, ref}}}
                end

              _ -> {:cont, {:ok, index}}
            end
          end)
        end
    end
  end

  defp default_remote_group_index do
    endpoint = System.get_env("EMBERVM_STORE_ENDPOINT", "") |> String.trim()
    bucket = System.get_env("EMBERVM_STORE_BUCKET", "embervm")

    case S3Client.new(endpoint, bucket) do
      nil -> {:error, :store_disabled}
      client ->
        with {:ok, entries} <- S3Client.list_all(client, "group_set/") do
          Enum.reduce_while(entries, {:ok, %{}}, fn %{key: key}, {:ok, index} ->
            case String.split(key, "/") do
              ["group_set", vendor, gid, set_id, member, file]
              when vendor != "" and gid != "" and set_id != "" and member != "" and file != "" ->
                binding = %{vendor: vendor, group_instance_id: gid}

                case Map.get(index, set_id) do
                  nil -> {:cont, {:ok, Map.put(index, set_id, binding)}}
                  ^binding -> {:cont, {:ok, index}}
                  _ -> {:halt, {:error, {:conflicting_bindings, set_id}}}
                end

              _ -> {:cont, {:ok, index}}
            end
          end)
        end
    end
  end

  # Fire an entry's LOCAL evictions then, ONLY if every local succeeded, the single
  # REMOTE (S3) evict, on an already-dialled channel (#38). Locals are best-effort
  # among themselves (each is independent and idempotent, so one failure is logged
  # and does not abort the rest), but a local failure of ANY kind (a warning return
  # OR a raised/thrown call, including a FAILED_PRECONDITION from noded's in-use
  # guard) withholds the remote: the reaper must never delete the off-node recovery
  # copy of a bundle the node itself refused/failed to remove locally. A local
  # success is a genuine (idempotent) removal, so the remote copy is then safe to
  # drop. Kept out of the spawn body so both evict_entry clauses share it.
  defp run_evict(state, channel, locals, remote) do
    all_local_ok =
      Enum.reduce(locals, true, fn req, acc ->
        # Reduce, not all?/2 with side effects: run EVERY local (they are
        # independent) while tracking whether all succeeded, so a group set still
        # attempts each member's local evict even when an earlier one failed.
        case run_one(state, channel, req) do
          :ok -> acc
          :error -> false
        end
      end)

    if all_local_ok and not is_nil(remote) do
      run_one(state, channel, remote)
    else
      if not is_nil(remote) do
        Logger.info(
          "embervm warmth reaper: skipping remote evict #{inspect(remote_req(remote))} (a local eviction was refused or failed; recovery copy kept)"
        )
      end
    end

    :ok
  end

  defp run_one(state, channel, {:artifact, req}), do: safe_call(fn -> state.evict_artifact_fun.(channel, req) end, req)
  defp run_one(state, channel, {:snapshot, req}), do: safe_call(fn -> state.evict_snapshot_fun.(channel, req) end, req)

  defp remote_req({_tag, req}), do: req

  # Returns :ok on a genuine (idempotent) eviction, :error on any refusal, error
  # return, raise, or throw. The caller uses :error to withhold the paired remote
  # evict; every path still logs so a best-effort sweep stays observable.
  defp safe_call(fun, req) do
    case fun.() do
      {:ok, _} ->
        :ok

      other ->
        Logger.warning("embervm warmth reaper: eviction #{inspect(req)} failed: #{inspect(other)}")
        :error
    end
  rescue
    e ->
      Logger.warning("embervm warmth reaper: eviction #{inspect(req)} raised: #{inspect(e)}")
      :error
  catch
    kind, reason ->
      Logger.warning("embervm warmth reaper: eviction #{inspect(req)} threw: #{inspect({kind, reason})}")
      :error
  end

  defp schedule_sweep(%{sweep_interval_ms: ms}) when ms > 0 do
    Process.send_after(self(), :sweep, ms)
    :ok
  end

  defp schedule_sweep(_state), do: :ok

  # -- channel + default seams -------------------------------------------------

  defp safe_channel(state, dial_key) do
    state.channel_fun.(dial_key)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # NodeChannel pools channels per dial key; release is a no-op for a keyed pool but
  # kept so a test channel_fun that returns a per-call channel can be torn down.
  defp release_channel(_state, _channel), do: :ok

  defp default_evict_snapshot(channel, req) do
    NodeService.Stub.evict_snapshot(channel, req)
  end

  defp default_evict_artifact(channel, req) do
    NodeService.Stub.evict_artifact(channel, req)
  end
end
