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

  NodeStatus does not report declared memory for any live VM. `adopt/3` therefore
  requires a workload-to-memory catalog lookup supplied in its options; it must
  not derive memory from observed node usage.
  """

  use GenServer

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

  @spec release(term(), term()) :: :ok
  def release(instance_id, ref), do: release(__MODULE__, instance_id, ref)

  @spec release(GenServer.server(), term(), term()) :: :ok
  def release(server, instance_id, ref) do
    GenServer.call(server, {:release, instance_id, ref})
  end

  @spec set_pool_target(term(), term(), non_neg_integer(), non_neg_integer()) :: :ok
  def set_pool_target(instance_id, workload, count, mem_mib),
    do: set_pool_target(__MODULE__, instance_id, workload, count, mem_mib)

  @spec set_pool_target(GenServer.server(), term(), term(), non_neg_integer(), non_neg_integer()) :: :ok
  def set_pool_target(server, instance_id, workload, count, mem_mib) do
    GenServer.call(server, {:set_pool_target, instance_id, workload, count, mem_mib})
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

  @spec drop_instance(term()) :: :ok
  def drop_instance(instance_id), do: drop_instance(__MODULE__, instance_id)

  @spec drop_instance(GenServer.server(), term()) :: :ok
  def drop_instance(server, instance_id), do: GenServer.call(server, {:drop_instance, instance_id})

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

  That is safe in the direction it fails. An under-count means the CP believes
  a brick has MORE room than it does, so it may over-place; noded's own
  declared-memory admission then refuses, costing one rejected RPC and never an
  overcommit. An over-count would be the dangerous direction, and adoption
  cannot produce one.

  It is NOT safe to mistake for a leak. Reading the shadow-mode divergence
  data, a persistent CP-below-node gap after a restart is this, not a lost
  release, and it shrinks as assigned tasks complete rather than staying flat.
  """
  @spec adopt(term(), Enumerable.t(), keyword()) :: :ok
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
    existing = Map.get(row_map(instance_id, state.table), ref)
    confirmed_at_ms = if existing, do: existing.confirmed_at_ms, else: nil
    entry = build_entry(ref, opts, now_ms, confirmed_at_ms)
    put_entry(state.table, instance_id, entry)
    {:reply, :ok, state}
  end

  def handle_call({:release, instance_id, ref}, _from, state) do
    update_row(state.table, instance_id, fn row -> Map.delete(row, ref) end)
    {:reply, :ok, state}
  end

  def handle_call({:set_pool_target, instance_id, workload, count, mem_mib}, _from, state) do
    ref = {:pool, workload}

    update_row(state.table, instance_id, fn row ->
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

    {:reply, :ok, state}
  end

  def handle_call({:reconcile, instance_id, live_refs, now_ms}, _from, state) do
    current = row_map(instance_id, state.table)

    {next, released} =
      Enum.reduce(current, {%{}, []}, fn {ref, entry}, {kept, released} ->
        cond do
          entry.kind == :pool_target -> {Map.put(kept, ref, entry), released}
          MapSet.member?(live_refs, ref) ->
            confirmed = if is_nil(entry.confirmed_at_ms), do: %{entry | confirmed_at_ms: now_ms}, else: entry
            {Map.put(kept, ref, confirmed), released}
          now_ms - entry.claimed_at_ms >= state.grace_age_ms ->
            {kept, [ref | released]}
          true ->
            {Map.put(kept, ref, entry), released}
        end
      end)

    write_row(state.table, instance_id, next)
    {:reply, {:ok, Enum.reverse(released)}, state}
  end

  def handle_call({:drop_instance, instance_id}, _from, state) do
    :ets.delete(state.table, instance_id)
    {:reply, :ok, state}
  end

  def handle_call({:adopt, instance_id, live_refs, opts}, _from, state) do
    now_ms = Keyword.get(opts, :now_ms, System.system_time(:millisecond))

    Enum.each(live_refs, fn live ->
      {ref, workload} = live_ref(live)
      mem_mib = catalog_memory!(workload, live, opts)
      put_entry(state.table, instance_id, build_entry(ref, [workload: workload, mem_mib: mem_mib], now_ms, now_ms))
    end)

    {:reply, :ok, state}
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
  defp live_ref({ref, workload}), do: {ref, workload}

  defp catalog_memory!(workload, live, opts) do
    source = Keyword.get(opts, :mem_mib, Keyword.get(opts, :memory_by_workload, Keyword.get(opts, :catalog)))

    value =
      cond do
        is_function(source, 1) -> source.(workload)
        is_map(source) -> Map.get(source, workload)
        is_integer(source) -> source
        is_map(live) and is_integer(live[:mem_mib]) -> live[:mem_mib]
        true -> nil
      end

    if is_integer(value) and value >= 0 do
      value
    else
      raise ArgumentError, "adopt requires declared memory from the workload catalog"
    end
  end

  defp put_entry(table, instance_id, entry) do
    row = row_map(instance_id, table)
    write_row(table, instance_id, Map.put(row, entry.ref, entry))
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
