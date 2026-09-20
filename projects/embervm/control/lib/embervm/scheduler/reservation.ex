defmodule Embervm.Scheduler.Reservation do
  @moduledoc """
  Shadow reservation ledger for declared VM memory.

  The ledger is CP intent, not node runtime truth. It stores per-reference entries
  in the public `:embervm_reservations` ETS table so rows can be reconciled as a
  set rather than as a drifting counter. Claims are never refused in shadow.

  Reconciliation has a grace age because a newly claimed VM may not have appeared
  in the next node report yet. Pool targets are CP intent without one corresponding
  reported VM and are never absence-collected. The table starts empty after a CP
  restart, which is correct because NodeCapacity is fail-closed empty until a node
  reports again.

  Because absence collection deliberately never reclaims a `:pool_target`, callers
  must zero the target when retiring a workload and drop the instance when a brick
  expires. The wiring PR that handles those lifecycle events owns that obligation;
  otherwise a deleted workload's pool reservation persists for the lifetime of the
  CP process.

  NodeStatus does not report declared memory for any live VM. `adopt/3` therefore
  requires a workload-to-memory catalog lookup supplied in its options; it must
  not derive memory from observed node usage. The per-VM declared memory is absent
  from `NodeStatus`, but the node-level declared SUM crosses the wire as
  `NodeStatus.mem_reserved_mib` (proto field 35), populated by noded in both
  admission models. That makes the divergence directly measurable per brick in
  shadow mode.
  """

  use GenServer
  require Logger

  @table :embervm_reservations
  @default_liveness_interval_ms 5_000

  @type entry :: %{
          ref: term(),
          workload: term(),
          mem_mib: non_neg_integer(),
          count: pos_integer(),
          kind: :instance | :pool_target,
          claimed_at_ms: integer(),
          confirmed_at_ms: integer() | nil
        }

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @spec table() :: atom()
  def table, do: @table

  @spec claim(term(), term(), keyword()) :: :ok
  def claim(instance_id, ref, opts) when is_list(opts) do
    claim(Keyword.get(opts, :server, __MODULE__), instance_id, ref, opts)
  end

  @spec claim(GenServer.server(), term(), term(), keyword()) :: :ok
  def claim(server, instance_id, ref, opts) when is_list(opts) do
    GenServer.call(server, {:claim, instance_id, ref, opts})
  end

  @doc "Records a shadow claim without blocking or failing the lifecycle caller."
  @spec claim_shadow(term(), term(), keyword()) :: :ok
  def claim_shadow(instance_id, ref, opts) when is_list(opts) do
    server = Keyword.get(opts, :server, __MODULE__)
    safe_cast(server, {:claim, instance_id, ref, opts})
  end

  @spec release(term(), term()) :: :ok
  def release(instance_id, ref), do: release(__MODULE__, instance_id, ref)

  @spec release(GenServer.server(), term(), term()) :: :ok
  def release(server, instance_id, ref) do
    GenServer.call(server, {:release, instance_id, ref})
  end

  @doc "Releases a claim only when the node explicitly confirmed teardown."
  @spec release_confirmed(term(), term(), boolean(), keyword()) :: :ok
  def release_confirmed(instance_id, ref, confirmed, opts \\ [])

  def release_confirmed(instance_id, ref, true, opts) when is_list(opts) do
    server = Keyword.get(opts, :server, __MODULE__)
    safe_cast(server, {:release, instance_id, ref, :node_confirmed_teardown})
  end

  def release_confirmed(_instance_id, _ref, false, _opts), do: :ok

  @spec set_pool_target(term(), term(), non_neg_integer(), non_neg_integer()) :: :ok
  def set_pool_target(instance_id, workload, count, mem_mib),
    do: set_pool_target(__MODULE__, instance_id, workload, count, mem_mib)

  @spec set_pool_target(GenServer.server(), term(), term(), non_neg_integer(), non_neg_integer()) :: :ok
  def set_pool_target(server, instance_id, workload, count, mem_mib) do
    GenServer.call(server, {:set_pool_target, instance_id, workload, count, mem_mib})
  end

  @doc "Updates a task-pool target without making refill depend on the ledger."
  @spec set_pool_target_shadow(term(), term(), non_neg_integer(), non_neg_integer(), keyword()) :: :ok
  def set_pool_target_shadow(instance_id, workload, count, mem_mib, opts \\ []) do
    server = Keyword.get(opts, :server, __MODULE__)
    safe_cast(server, {:set_pool_target, instance_id, workload, count, mem_mib})
  end

  @spec reserved_mib(term(), atom()) :: non_neg_integer()
  def reserved_mib(instance_id, table \\ @table) do
    instance_id
    |> row(table)
    |> Enum.reduce(0, fn entry, total -> total + entry.mem_mib * entry.count end)
  end

  @spec entries(term(), atom()) :: [entry()]
  def entries(instance_id, table \\ @table), do: row(instance_id, table)

  @spec reconcile(term(), MapSet.t() | Enumerable.t(), integer()) :: {:ok, [term()]}
  def reconcile(instance_id, live_refs, now_ms),
    do: reconcile(__MODULE__, instance_id, live_refs, now_ms)

  @spec reconcile(GenServer.server(), term(), MapSet.t() | Enumerable.t(), integer()) :: {:ok, [term()]}
  def reconcile(server, instance_id, live_refs, now_ms) do
    GenServer.call(server, {:reconcile, instance_id, MapSet.new(live_refs), now_ms})
  end

  @doc "Adopts node-reported live VMs and absence-collects old claims."
  @spec observe(GenServer.server(), term(), Enumerable.t(), keyword()) ::
          {:ok, %{adopted: non_neg_integer(), skipped: non_neg_integer(), released: [term()]}}
  def observe(server, instance_id, live_refs, opts) when is_list(opts) do
    GenServer.call(server, {:observe, instance_id, Enum.to_list(live_refs), opts})
  end

  @doc "Queues node truth for reconciliation without making NodeRegistry depend on the ledger."
  @spec observe_shadow(term(), Enumerable.t(), keyword()) :: :ok
  def observe_shadow(instance_id, live_refs, opts \\ []) when is_list(opts) do
    server = Keyword.get(opts, :server, __MODULE__)
    safe_cast(server, {:observe, instance_id, Enum.to_list(live_refs), opts})
  end

  @spec drop_instance(term()) :: :ok
  def drop_instance(instance_id), do: drop_instance(__MODULE__, instance_id)

  @spec drop_instance(GenServer.server(), term()) :: :ok
  def drop_instance(server, instance_id), do: GenServer.call(server, {:drop_instance, instance_id})

  @doc "Drops an expired brick row without making expiry depend on the ledger."
  @spec drop_instance_shadow(term(), keyword()) :: :ok
  def drop_instance_shadow(instance_id, opts \\ []) do
    server = Keyword.get(opts, :server, __MODULE__)
    safe_cast(server, {:drop_instance, instance_id})
  end

  @spec all(atom()) :: %{term() => [entry()]}
  def all(table \\ @table) do
    :ets.foldl(fn {instance_id, row}, acc -> Map.put(acc, instance_id, Map.values(row)) end, %{}, table)
  end

  @doc """
  Seeds `:instance` entries from an instance's node-reported live VMs, for
  rebuilding the row after a control-plane restart. Declared memory comes from
  the catalog, NOT from the node: `NodeStatus` carries no per-VM memory field.

  ## adoption is INCOMPLETE, by the wire format

  `NodeStatus` enumerates live VMs in five mutually exclusive lists
  (`WorkloadCapacity.primed_vm_ids`, `session_vms`, `serving_vms`,
  `stateful_vms`, `group_member_vms`). An ASSIGNED task VM is in none of them:
  it leaves `primed_vm_ids` the moment it stops being parked, and the only
  other signal is `live_vms`, an aggregate COUNT with no ids. So a row rebuilt
  by adoption under-counts by the number of in-flight task assignments.

  That fails safe in the direction it fails: an under-count means the CP believes
  a brick has more room than it does, so it may over-place. On today's fleet the
  backstop is noded's OBSERVED headroom gate, because the missing VM's memory is
  already inside the brick's cgroup usage: the node charges it by measurement,
  with no ledger involved, and a genuinely full brick refuses the boot for one
  rejected RPC. That protection is lag-bounded rather than absolute, since a
  just-restored guest faults its pages in lazily, which is the same lag this ledger
  exists to close. Once admission flips to `reserved` it becomes exact: noded's
  claims are a projection of its own live VM map, which DOES include in-flight
  assigned task VMs, so the node refuses precisely the placements an under-counted
  CP row would allow.

  A missing catalog entry is skipped rather than raised. Adoption runs in this
  GenServer, which is supervised under `:rest_for_one`; raising here would restart
  every control-plane child after `Reservation`, including the HTTP listener.

  It is NOT safe to mistake for a leak. Reading the shadow-mode divergence
  data, a persistent CP-below-node gap after a restart is this, not a lost
  release, and it shrinks as assigned tasks complete rather than staying flat.
  """
  @spec adopt(term(), Enumerable.t(), keyword()) ::
          {:ok, %{adopted: non_neg_integer(), skipped: non_neg_integer()}}
  def adopt(instance_id, live_refs, opts) when is_list(opts) do
    server = Keyword.get(opts, :server, __MODULE__)
    GenServer.call(server, {:adopt, instance_id, Enum.to_list(live_refs), opts})
  end

  @impl true
  def init(opts) do
    table = Keyword.get(opts, :table, @table)
    create_empty_table(table)

    liveness_interval_ms =
      Keyword.get(
        opts,
        :liveness_interval_ms,
        Application.get_env(:embervm, :node_liveness_interval_ms, @default_liveness_interval_ms)
      )

    {:ok, %{table: table, grace_age_ms: Keyword.get(opts, :grace_age_ms, liveness_interval_ms * 2)}}
  end

  @impl true
  def handle_call({:claim, instance_id, ref, opts}, _from, state) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))
    put_claim_entry(state.table, instance_id, ref, opts, now_ms)
    {:reply, :ok, state}
  end

  def handle_call({:release, instance_id, ref}, _from, state) do
    release_entry(state.table, instance_id, ref, :explicit)
    {:reply, :ok, state}
  end

  def handle_call({:set_pool_target, instance_id, workload, count, mem_mib}, _from, state) do
    set_pool_target_entry(state.table, instance_id, workload, count, mem_mib)
    {:reply, :ok, state}
  end

  def handle_call({:reconcile, instance_id, live_refs, now_ms}, _from, state) do
    {next, released} = reconcile_row(row_map(instance_id, state.table), live_refs, now_ms, state.grace_age_ms)
    write_row(state.table, instance_id, next)
    {:reply, {:ok, Enum.reverse(released)}, state}
  end

  def handle_call({:observe, instance_id, live_refs, opts}, _from, state) do
    {result, state} = observe_live(state, instance_id, live_refs, opts)
    {:reply, {:ok, result}, state}
  end

  def handle_call({:drop_instance, instance_id}, _from, state) do
    :ets.delete(state.table, instance_id)
    {:reply, :ok, state}
  end

  def handle_call({:adopt, instance_id, live_refs, opts}, _from, state) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))

    counts = Enum.reduce(live_refs, %{adopted: 0, skipped: 0}, fn live, counts ->
      {ref, workload} = live_ref(live)

      case catalog_memory(workload, Keyword.put(opts, :live, live)) do
        {:ok, mem_mib} ->
          put_entry(
            state.table,
            instance_id,
            build_entry(ref, [workload: workload, mem_mib: mem_mib], now_ms, now_ms)
          )

          %{counts | adopted: counts.adopted + 1}

        :error ->
          %{counts | skipped: counts.skipped + 1}
      end
    end)

    {:reply, {:ok, counts}, state}
  end

  @impl true
  def handle_cast({:claim, instance_id, ref, opts}, state) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))
    put_claim_entry(state.table, instance_id, ref, opts, now_ms)
    {:noreply, state}
  end

  def handle_cast({:release, instance_id, ref, reason}, state) do
    release_entry(state.table, instance_id, ref, reason)
    {:noreply, state}
  end

  def handle_cast({:set_pool_target, instance_id, workload, count, mem_mib}, state) do
    set_pool_target_entry(state.table, instance_id, workload, count, mem_mib)
    {:noreply, state}
  end

  def handle_cast({:observe, instance_id, live_refs, opts}, state) do
    {_result, state} = observe_live(state, instance_id, live_refs, opts)
    {:noreply, state}
  end

  def handle_cast({:drop_instance, instance_id}, state) do
    :ets.delete(state.table, instance_id)
    {:noreply, state}
  end

  defp build_entry(ref, opts, now_ms, confirmed_at_ms) do
    %{
      ref: ref,
      workload: Keyword.fetch!(opts, :workload),
      mem_mib: Keyword.fetch!(opts, :mem_mib),
      count: Keyword.get(opts, :count, 1),
      kind: Keyword.get(opts, :kind, :instance),
      claimed_at_ms: now_ms,
      confirmed_at_ms: confirmed_at_ms
    }
  end

  defp live_ref(%{ref: ref, workload: workload}), do: {ref, workload}
  defp live_ref(%{vm_id: ref, workload: workload}), do: {ref, workload}
  defp live_ref(%{ref: ref}), do: {ref, nil}
  defp live_ref(%{vm_id: ref}), do: {ref, nil}
  defp live_ref({ref, workload}), do: {ref, workload}

  defp catalog_memory(workload, opts) do
    source =
      Keyword.get(
        opts,
        :mem_mib,
        Keyword.get(opts, :memory_by_workload, Keyword.get(opts, :catalog, :workload_catalog))
      )
    live = Keyword.get(opts, :live)

    value =
      cond do
        is_function(source, 1) -> source.(workload)
        is_map(source) -> Map.get(source, workload)
        is_integer(source) -> source
        is_map(live) and is_integer(live[:mem_mib]) -> live[:mem_mib]
        source == :workload_catalog -> catalog_entry_memory(workload, live)
        true -> nil
      end

    if is_integer(value) and value >= 0 do
      {:ok, value}
    else
      :error
    end
  end

  defp catalog_entry_memory(workload, live) do
    case Embervm.WorkloadCatalog.fetch(workload) do
      {:ok, entry} -> Map.get(entry, :mem_mib) || group_member_memory(entry, live)
      :error -> nil
    end
  rescue
    _ -> nil
  catch
    _, _ -> nil
  end

  defp group_member_memory(%{group: %{members: members}}, %{member_name: member_name})
       when is_list(members) and is_binary(member_name) do
    members
    |> Enum.find(&group_member_name?(&1, member_name))
    |> case do
      nil -> nil
      member -> Map.get(member, :mem_mib)
    end
  end

  defp group_member_memory(_entry, _live), do: nil

  defp group_member_name?(member, member_name) do
    name = Map.get(member, :name)

    case Map.get(member, :replicas) do
      replicas when is_integer(replicas) and replicas > 1 ->
        Enum.any?(0..(replicas - 1), &(member_name == "#{name}-#{&1}"))

      _ ->
        member_name == name
    end
  end

  defp observe_live(state, instance_id, live_refs, opts) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))
    current = row_map(instance_id, state.table)

    {adopted, counts} =
      Enum.reduce(live_refs, {current, %{adopted: 0, skipped: 0}}, fn live, {row, counts} ->
        {ref, reported_workload} = live_ref(live)
        workload = reported_workload || group_workload(live)

        cond do
          Map.has_key?(row, ref) ->
            {row, counts}

          is_nil(workload) ->
            {row, %{counts | skipped: counts.skipped + 1}}

          true ->
            case catalog_memory(workload, Keyword.put(opts, :live, live)) do
              {:ok, mem_mib} ->
                entry = build_entry(ref, [workload: workload, mem_mib: mem_mib], now_ms, now_ms)
                {Map.put(row, ref, entry), %{counts | adopted: counts.adopted + 1}}

              :error ->
                {row, %{counts | skipped: counts.skipped + 1}}
            end
        end
      end)

    live_ref_set = MapSet.new(live_refs, fn live -> elem(live_ref(live), 0) end)
    {next, released} = reconcile_row(adopted, live_ref_set, now_ms, state.grace_age_ms)
    write_row(state.table, instance_id, next)

    Enum.each(released, fn ref ->
      entry = Map.fetch!(adopted, ref)

      Logger.info("embervm reservation released",
        reservation_event: :absence_gc,
        release_reason: :absence_gc,
        instance_id: instance_id,
        vm_id: ref,
        workload: entry.workload,
        mem_mib: entry.mem_mib,
        claim_age_ms: max(0, now_ms - entry.claimed_at_ms)
      )
    end)

    {%{adopted: counts.adopted, skipped: counts.skipped, released: Enum.reverse(released)}, state}
  end

  defp group_workload(%{group_instance_id: group_instance_id}) when is_binary(group_instance_id) do
    case Embervm.GroupStore.get(group_instance_id) do
      {:ok, instance} -> Map.get(instance, :workload)
      :error -> nil
    end
  rescue
    _ -> nil
  catch
    _, _ -> nil
  end

  defp group_workload(_live), do: nil

  defp reconcile_row(current, live_refs, now_ms, grace_age_ms) do
    Enum.reduce(current, {%{}, []}, fn {ref, entry}, {kept, released} ->
      cond do
        entry.kind == :pool_target ->
          {Map.put(kept, ref, entry), released}

        MapSet.member?(live_refs, ref) ->
          confirmed = if is_nil(entry.confirmed_at_ms), do: %{entry | confirmed_at_ms: now_ms}, else: entry
          {Map.put(kept, ref, confirmed), released}

        now_ms - entry.claimed_at_ms >= grace_age_ms ->
          {kept, [ref | released]}

        true ->
          {Map.put(kept, ref, entry), released}
      end
    end)
  end

  defp release_entry(table, instance_id, ref, reason) do
    row = row_map(instance_id, table)

    case Map.pop(row, ref) do
      {nil, _row} ->
        :ok

      {entry, next} ->
        write_row(table, instance_id, next)

        Logger.info("embervm reservation released",
          reservation_event: :release,
          release_reason: reason,
          instance_id: instance_id,
          vm_id: ref,
          workload: entry.workload,
          mem_mib: entry.mem_mib
        )
    end
  end

  defp set_pool_target_entry(table, instance_id, workload, count, mem_mib) do
    ref = {:pool, workload}

    update_row(table, instance_id, fn row ->
      if count == 0 do
        Map.delete(row, ref)
      else
        existing = Map.get(row, ref)
        claimed_at_ms = if existing, do: existing.claimed_at_ms, else: System.system_time(:millisecond)

        Map.put(row, ref, %{
          ref: ref,
          workload: workload,
          mem_mib: mem_mib,
          count: count,
          kind: :pool_target,
          claimed_at_ms: claimed_at_ms,
          confirmed_at_ms: nil
        })
      end
    end)
  end

  defp safe_cast(server, message) do
    GenServer.cast(server, message)
    :ok
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  defp put_entry(table, instance_id, entry) do
    row = row_map(instance_id, table)
    write_row(table, instance_id, Map.put(row, entry.ref, entry))
  end

  defp put_claim_entry(table, instance_id, ref, opts, now_ms) do
    existing = Map.get(row_map(instance_id, table), ref)
    confirmed_at_ms = if existing, do: existing.confirmed_at_ms, else: nil
    entry = build_entry(ref, opts, now_ms, confirmed_at_ms)
    entry = if existing, do: %{entry | claimed_at_ms: existing.claimed_at_ms}, else: entry
    put_entry(table, instance_id, entry)
  end

  defp update_row(table, instance_id, fun) do
    next = fun.(row_map(instance_id, table))
    write_row(table, instance_id, next)
  end

  defp write_row(table, instance_id, row) when map_size(row) == 0, do: :ets.delete(table, instance_id)
  defp write_row(table, instance_id, row), do: :ets.insert(table, {instance_id, row})

  defp row(instance_id, table), do: Map.values(row_map(instance_id, table))
  defp row_map(instance_id, table) do
    case :ets.lookup(table, instance_id) do
      [{^instance_id, row}] -> row
      [] -> %{}
    end
  end

  defp create_empty_table(table) do
    try do
      :ets.new(table, [:set, :public, :named_table, read_concurrency: true])
    rescue
      ArgumentError ->
        :ets.delete_all_objects(table)
        table
    end
  end
end
