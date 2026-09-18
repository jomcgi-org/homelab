defmodule Embervm.CapacityReport do
  @moduledoc """
  Read-only assembly of the control plane's existing capacity and demand facts.

  The report owns no state. It reads the `NodeCapacity` and `WorkloadCatalog`
  ETS tables, asks the dispatcher for its current queue-depth snapshot, and
  calculates future cron firings from catalog entries. Callers may inject those
  inputs, the clock, and the horizon for deterministic tests.

  Memory values retain noded's cgroup v2 semantics. `mem_budget_mib` is
  `memory.max` minus the configured daemon RSS reserve. `mem_headroom_mib` is
  `memory.max - non_reclaimable`, where non-reclaimable is `memory.current`
  minus `max(memory.stat:file - memory.stat:shmem, 0)`. File-backed page cache,
  active or inactive, is therefore treated as reclaimable, while shmem is not.
  If `memory.stat` cannot be read, no cache is assumed reclaimable. This answers
  whether a guest allocation can fit and is deliberately not the same value as
  kubelet working set or `kubectl top`.

  Instance rows remain keyed and labeled by `instance_id` and `pod_uid`. A
  surge can have two instances with the same `node_id`, so grouping by node id
  would erase real capacity. Aggregate headroom and free-slot counts are
  descriptive only: they are never a placement guarantee or a scaling target.
  The observable gauges land in Honeycomb through the collector metrics pipeline.
  """

  alias Embervm.{Cron, Dispatcher, NodeCapacity, WorkloadCatalog}

  @default_horizon_seconds 3_600
  @max_horizon_seconds 86_400

  @memory_semantics "cgroup_v2_memory_max_minus_non_reclaimable_including_reclaimable_file_cache_excluding_shmem"

  @gauge_specs [
    %{name: :"embervm.capacity.instance.memory_budget", unit: :MiBy, description: "Guest-schedulable cgroup memory budget"},
    %{name: :"embervm.capacity.instance.memory_headroom", unit: :MiBy, description: "Reclaimable-aware cgroup memory headroom"},
    %{name: :"embervm.capacity.instance.cpu_budget", unit: :mcores, description: "Cgroup CPU quota budget"},
    %{name: :"embervm.capacity.instance.cpu_headroom", unit: :mcores, description: "Sampled cgroup CPU headroom"},
    %{name: :"embervm.capacity.instance.vm_slots_live", unit: :vm, description: "Live VM slots in use"},
    %{name: :"embervm.capacity.instance.vm_slots_max", unit: :vm, description: "Maximum live VM slots"},
    %{name: :"embervm.capacity.instance.vm_slots_free", unit: :vm, description: "Free live VM slots"},
    %{name: :"embervm.capacity.workload.primed_occupancy", unit: :vm, description: "Parked pristine VMs"},
    %{name: :"embervm.capacity.workload.free_slots", unit: :vm, description: "Free VM slots on instances reporting the workload"},
    %{name: :"embervm.capacity.demand", unit: :task, description: "Capacity demand by tier"}
  ]

  @doc "Build a JSON-safe capacity report from authoritative live inputs."
  @spec build(keyword()) :: map()
  def build(opts \\ []) do
    now = Keyword.get_lazy(opts, :now, fn -> clock(opts).() end)
    horizon_seconds = Keyword.get(opts, :horizon_seconds, configured_horizon_seconds())
    facts = Keyword.get_lazy(opts, :capacity_facts, fn -> NodeCapacity.all(capacity_table(opts)) end)
    entries = Keyword.get_lazy(opts, :catalog_entries, fn -> catalog_entries(catalog_table(opts)) end)
    dispatcher_stats = Keyword.get_lazy(opts, :dispatcher_stats, &safe_dispatcher_stats/0)

    instances = facts |> Enum.map(&instance_row/1) |> Enum.sort_by(& &1.instance_id)
    catalog = catalog_map(entries)
    queue_depth = queue_depth(dispatcher_stats)

    workloads =
      catalog
      |> workload_names(instances, queue_depth)
      |> Enum.map(&workload_row(&1, catalog, instances, queue_depth, now, horizon_seconds))

    %{
      generated_at: DateTime.to_iso8601(now),
      horizon_seconds: horizon_seconds,
      semantics: %{
        memory: @memory_semantics,
        memory_detail:
          "MiB from noded's own cgroup v2: budget is memory.max minus daemon reserve; headroom treats file cache except shmem as reclaimable. It is not kubectl top working set.",
        aggregation:
          "Summed headroom and free slots are descriptive signals, not a placement guarantee, reservation, scaling decision, or target."
      },
      instances: instances,
      workloads: workloads,
      demand: %{
        floors: demand_tier(workloads, :floor),
        committed: demand_tier(workloads, :committed),
        observed: demand_tier(workloads, :observed)
      }
    }
  end

  @doc "Parse the inert future-facing desired-capacity knob."
  @spec parse_desired_capacity(String.t() | nil) :: {:ok, non_neg_integer() | nil} | {:error, :invalid_desired_capacity}
  def parse_desired_capacity(nil), do: {:ok, nil}
  def parse_desired_capacity(""), do: {:ok, nil}

  def parse_desired_capacity(raw) when is_binary(raw) do
    case Integer.parse(raw) do
      {value, ""} when value >= 0 -> {:ok, value}
      _ -> {:error, :invalid_desired_capacity}
    end
  end

  def parse_desired_capacity(_), do: {:error, :invalid_desired_capacity}

  @doc "Parse and bound the cron demand horizon."
  @spec parse_horizon_seconds(String.t() | nil) :: {:ok, pos_integer()} | {:error, :invalid_capacity_horizon}
  def parse_horizon_seconds(nil), do: {:ok, @default_horizon_seconds}
  def parse_horizon_seconds(""), do: {:ok, @default_horizon_seconds}

  def parse_horizon_seconds(raw) when is_binary(raw) do
    case Integer.parse(raw) do
      {value, ""} when value > 0 and value <= @max_horizon_seconds -> {:ok, value}
      _ -> {:error, :invalid_capacity_horizon}
    end
  end

  def parse_horizon_seconds(_), do: {:error, :invalid_capacity_horizon}

  @doc "Return OTel observable-gauge observations derived from one report."
  @spec gauge_observations(map()) :: [map()]
  def gauge_observations(report) do
    instance_gauges(report.instances) ++ workload_gauges(report.workloads)
  end

  @doc false
  def observe_all(opts) do
    opts
    |> build()
    |> gauge_observations()
    |> Enum.map(fn gauge -> {gauge.name, gauge.observations} end)
  end

  @doc "Register one OTel observable gauge per report measurement."
  @spec register_gauges(keyword()) :: :ok
  def register_gauges(opts \\ []) do
    meter = :opentelemetry_experimental.get_meter(:opentelemetry.get_application_scope(__MODULE__))

    instruments =
      Enum.map(@gauge_specs, fn spec ->
        :otel_meter.create_observable_gauge(meter, spec.name, %{
          unit: spec.unit,
          description: spec.description
        })
      end)

    :ok = :otel_meter.register_callback(meter, instruments, &__MODULE__.observe_all/1, opts)
  end

  defp capacity_table(opts), do: Keyword.get(opts, :capacity_table, NodeCapacity.table())
  defp catalog_table(opts), do: Keyword.get(opts, :catalog_table, WorkloadCatalog.table())
  defp clock(opts), do: Keyword.get(opts, :clock, &DateTime.utc_now/0)

  defp configured_horizon_seconds do
    Application.get_env(:embervm, :capacity_horizon_seconds, @default_horizon_seconds)
  end

  defp catalog_entries(table) do
    if :ets.whereis(table) == :undefined do
      []
    else
      :ets.select(table, [{{:"$1", :"$2"}, [], [{{:"$1", :"$2"}}]}])
    end
  end

  defp safe_dispatcher_stats do
    Dispatcher.stats()
  rescue
    _ -> %{}
  catch
    _, _ -> %{}
  end

  defp instance_row(facts) do
    node_id = text(Map.get(facts, :node_id))
    pod_uid = text(Map.get(facts, :pod_uid))
    live_vms = non_negative(Map.get(facts, :live_vms))
    max_live_vms = non_negative(Map.get(facts, :max_live_vms))

    %{
      instance_id: instance_id(facts, node_id, pod_uid),
      node_id: node_id,
      pod_uid: pod_uid,
      size_class: text(Map.get(facts, :size_class)),
      mem_budget_mib: non_negative(Map.get(facts, :mem_budget_mib)),
      mem_headroom_mib: non_negative(Map.get(facts, :mem_headroom_mib)),
      cpu_budget_millicores: non_negative(Map.get(facts, :cpu_budget_millicores)),
      cpu_headroom_millicores: non_negative(Map.get(facts, :cpu_headroom_millicores)),
      live_vms: live_vms,
      max_live_vms: max_live_vms,
      free_vm_slots: max(max_live_vms - live_vms, 0),
      workloads:
        facts
        |> Map.get(:workloads, %{})
        |> Enum.map(fn {workload, capacity} ->
          %{workload: text(workload), primed_occupancy: non_negative(Map.get(capacity, :free_primed_slots))}
        end)
        |> Enum.sort_by(& &1.workload)
    }
  end

  defp instance_id(facts, node_id, pod_uid) do
    case text(Map.get(facts, :instance_id)) do
      "" when pod_uid != "" -> "#{node_id}/#{pod_uid}"
      "" -> node_id
      id -> id
    end
  end

  defp catalog_map(entries) do
    Map.new(entries, fn {name, entry} -> {text(name), entry} end)
  end

  defp workload_names(catalog, instances, queue_depth) do
    instance_names = for instance <- instances, capacity <- instance.workloads, do: capacity.workload

    (Map.keys(catalog) ++ Map.keys(queue_depth) ++ instance_names)
    |> Enum.uniq()
    |> Enum.sort()
  end

  defp workload_row(name, catalog, instances, queue_depth, now, horizon_seconds) do
    entry = Map.get(catalog, name, %{})
    class = text(Map.get(entry, :class, "unknown"))
    primed_occupancy = primed_occupancy(instances, name)
    free_slots = reported_workload_free_slots(instances, name)
    floor = non_negative(Map.get(entry, :floor))
    committed = cron_firings(entry, now, horizon_seconds)
    observed = non_negative(Map.get(queue_depth, name))

    %{
      workload: name,
      class: class,
      cap: non_negative(Map.get(entry, :cap)),
      primed_occupancy: primed_occupancy,
      free_slots: free_slots,
      queue_depth: observed,
      cron_firings: committed,
      demand: %{floor: floor, committed: committed, observed: observed}
    }
  end

  defp primed_occupancy(instances, name) do
    Enum.reduce(instances, 0, fn instance, total ->
      total +
        (instance.workloads
         |> Enum.find(%{primed_occupancy: 0}, &(&1.workload == name))
         |> Map.get(:primed_occupancy, 0))
    end)
  end

  defp reported_workload_free_slots(instances, name) do
    Enum.reduce(instances, 0, fn instance, total ->
      if Enum.any?(instance.workloads, &(&1.workload == name)) do
        total + instance.free_vm_slots
      else
        total
      end
    end)
  end

  defp queue_depth(stats) when is_map(stats) do
    stats
    |> Map.get(:queue_depth, Map.get(stats, "queue_depth", %{}))
    |> case do
      depths when is_map(depths) -> Map.new(depths, fn {name, depth} -> {text(name), non_negative(depth)} end)
      _ -> %{}
    end
  end

  defp queue_depth(_), do: %{}

  defp cron_firings(entry, now, horizon_seconds) do
    deadline = DateTime.add(now, horizon_seconds, :second)

    entry
    |> Map.get(:triggers, [])
    |> Enum.reduce(0, fn trigger, total ->
      case Cron.parse(Map.get(trigger, :cron)) do
        {:ok, cron} -> total + count_firings(cron, first_future_minute(now), deadline, 0)
        {:error, _} -> total
      end
    end)
  end

  # Walk only the configured horizon, at most 1,440 minutes. Calling Cron.next/2
  # for an impossible but parseable calendar combination could search its full
  # one-year safety window on every report request.
  defp count_firings(cron, cursor, deadline, count) do
    if DateTime.compare(cursor, deadline) in [:lt, :eq] do
      next_count = if Cron.matches?(cron, cursor), do: count + 1, else: count
      count_firings(cron, DateTime.add(cursor, 60, :second), deadline, next_count)
    else
      count
    end
  end

  defp first_future_minute(now) do
    now
    |> Map.put(:second, 0)
    |> Map.put(:microsecond, {0, 0})
    |> DateTime.add(60, :second)
  end

  defp demand_tier(workloads, tier) do
    by_workload = Map.new(workloads, &{&1.workload, Map.fetch!(&1.demand, tier)})

    by_class =
      Enum.reduce(workloads, %{}, fn workload, totals ->
        Map.update(totals, workload.class, Map.fetch!(workload.demand, tier), fn value ->
          value + Map.fetch!(workload.demand, tier)
        end)
      end)

    %{total: Enum.sum(Map.values(by_workload)), by_class: by_class, by_workload: by_workload}
  end

  defp instance_gauges(instances) do
    for {name, field} <- [
          {:"embervm.capacity.instance.memory_budget", :mem_budget_mib},
          {:"embervm.capacity.instance.memory_headroom", :mem_headroom_mib},
          {:"embervm.capacity.instance.cpu_budget", :cpu_budget_millicores},
          {:"embervm.capacity.instance.cpu_headroom", :cpu_headroom_millicores},
          {:"embervm.capacity.instance.vm_slots_live", :live_vms},
          {:"embervm.capacity.instance.vm_slots_max", :max_live_vms},
          {:"embervm.capacity.instance.vm_slots_free", :free_vm_slots}
        ] do
      %{
        name: name,
        observations:
          Enum.map(instances, fn instance ->
            {Map.fetch!(instance, field), instance_labels(instance)}
          end)
      }
    end
  end

  defp workload_gauges(workloads) do
    capacity =
      for {name, field} <- [
            {:"embervm.capacity.workload.primed_occupancy", :primed_occupancy},
            {:"embervm.capacity.workload.free_slots", :free_slots}
          ] do
        %{
          name: name,
          observations:
            Enum.map(workloads, fn workload ->
              {Map.fetch!(workload, field), workload_labels(workload)}
            end)
        }
      end

    demand = %{
      name: :"embervm.capacity.demand",
      observations:
        for workload <- workloads, tier <- [:floor, :committed, :observed] do
          labels = Map.put(workload_labels(workload), "ember.demand.tier", Atom.to_string(tier))
          {Map.fetch!(workload.demand, tier), labels}
        end
    }

    capacity ++ [demand]
  end

  defp instance_labels(instance) do
    %{
      "ember.instance.id" => instance.instance_id,
      "ember.node.id" => instance.node_id,
      "ember.pod.uid" => instance.pod_uid,
      "ember.size_class" => instance.size_class
    }
  end

  defp workload_labels(workload) do
    %{
      "ember.workload" => workload.workload,
      "ember.workload.class" => workload.class
    }
  end

  defp text(nil), do: ""
  defp text(value) when is_binary(value), do: value
  defp text(value), do: to_string(value)

  defp non_negative(value) when is_integer(value) and value >= 0, do: value
  defp non_negative(_), do: 0
end
