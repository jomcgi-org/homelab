defmodule Embervm.CapacityReportTest do
  use ExUnit.Case, async: false

  alias Embervm.{CapacityReport, NodeCapacity, WorkloadCatalog}

  defp table(prefix), do: String.to_atom("#{prefix}_#{System.unique_integer([:positive])}")

  defp fixed_now, do: ~U[2026-09-14 12:00:30Z]

  defp seed_tables do
    capacity_table = NodeCapacity.create(table("capacity_report_nodes"))
    catalog_table = WorkloadCatalog.create(table("capacity_report_catalog"))

    NodeCapacity.put(capacity_table, {"node-4", "pod-old"}, %{
      node_id: "node-4",
      pod_uid: "pod-old",
      instance_id: "node-4/pod-old",
      size_class: "4gi",
      mem_budget_mib: 3_584,
      mem_headroom_mib: 1_024,
      cpu_budget_millicores: 2_000,
      cpu_headroom_millicores: 1_250,
      live_vms: 2,
      max_live_vms: 6,
      workloads: %{
        "wl-a" => %{free_primed_slots: 2},
        "wl-b" => %{free_primed_slots: 1}
      }
    })

    NodeCapacity.put(capacity_table, {"node-4", "pod-new"}, %{
      node_id: "node-4",
      pod_uid: "pod-new",
      instance_id: "node-4/pod-new",
      size_class: "4gi",
      mem_budget_mib: 3_584,
      mem_headroom_mib: 2_048,
      cpu_budget_millicores: 2_000,
      cpu_headroom_millicores: 1_500,
      live_vms: 1,
      max_live_vms: 4,
      workloads: %{"wl-a" => %{free_primed_slots: 1}}
    })

    WorkloadCatalog.upsert(catalog_table, "wl-a", %{
      name: "wl-a",
      class: "task",
      floor: 2,
      cap: 8,
      triggers: [%{cron: "*/15 * * * *", payload: %{}}]
    })

    WorkloadCatalog.upsert(catalog_table, "wl-b", %{
      name: "wl-b",
      class: "session",
      floor: 1,
      cap: 4,
      triggers: []
    })

    [
      capacity_table: capacity_table,
      catalog_table: catalog_table,
      dispatcher_stats: %{queue_depth: %{"wl-a" => 3, "orphan" => 2}},
      capacity_health: %{
        ok: false,
        conditions: [
          %{
            workload: "wl-b",
            reason: :ceiling_exhausted,
            size_class: "8gi",
            need_mib: 4_096
          }
        ],
        ceilings: [
          %{size_class: "8gi", bootstrap_max: 1, operative_ceiling: 2, ceiling_bound: 2}
        ]
      },
      now: fixed_now(),
      horizon_seconds: 3_600
    ]
  end

  test "assembles instance, workload, and three-tier demand shape from authoritative inputs" do
    report = CapacityReport.build(seed_tables())

    assert report.generated_at == "2026-09-14T12:00:30Z"
    assert report.horizon_seconds == 3_600
    assert report.semantics.memory =~ "reclaimable_file_cache_excluding_shmem"
    assert report.semantics.aggregation =~ "not a placement guarantee"
    refute report.health.ok
    assert hd(report.health.conditions).reason == :ceiling_exhausted

    assert Enum.map(report.instances, & &1.instance_id) == ["node-4/pod-new", "node-4/pod-old"]
    assert Enum.uniq(Enum.map(report.instances, & &1.node_id)) == ["node-4"]

    old = Enum.find(report.instances, &(&1.pod_uid == "pod-old"))
    assert old.mem_budget_mib == 3_584
    assert old.mem_headroom_mib == 1_024
    assert old.cpu_budget_millicores == 2_000
    assert old.cpu_headroom_millicores == 1_250
    assert old.live_vms == 2
    assert old.max_live_vms == 6
    assert old.free_vm_slots == 4

    wl_a = Enum.find(report.workloads, &(&1.workload == "wl-a"))
    assert wl_a.class == "task"
    assert wl_a.cap == 8
    assert wl_a.primed_occupancy == 3
    assert wl_a.free_slots == 7
    assert wl_a.queue_depth == 3
    assert wl_a.cron_firings == 4
    assert wl_a.demand == %{floor: 2, committed: 4, observed: 3}

    assert report.demand.floors == %{
             total: 3,
             by_class: %{"session" => 1, "task" => 2, "unknown" => 0},
             by_workload: %{"orphan" => 0, "wl-a" => 2, "wl-b" => 1}
           }

    assert report.demand.committed.total == 4
    assert report.demand.committed.by_workload == %{"orphan" => 0, "wl-a" => 4, "wl-b" => 0}
    assert report.demand.observed.total == 5
    assert report.demand.observed.by_workload == %{"orphan" => 2, "wl-a" => 3, "wl-b" => 0}
  end

  test "cron committed demand stops at the inclusive configurable horizon" do
    opts = seed_tables()

    short = CapacityReport.build(Keyword.put(opts, :horizon_seconds, 869))
    boundary = CapacityReport.build(Keyword.put(opts, :horizon_seconds, 870))

    assert short.demand.committed.by_workload["wl-a"] == 0
    assert boundary.demand.committed.by_workload["wl-a"] == 1
  end

  test "observable gauges carry stable workload and per-instance labels" do
    gauges = CapacityReport.build(seed_tables()) |> CapacityReport.gauge_observations()

    memory = Enum.find(gauges, &(&1.name == :"embervm.capacity.instance.memory_budget"))
    assert length(memory.observations) == 2

    assert Enum.all?(memory.observations, fn {_value, labels} ->
             Map.keys(labels) |> Enum.sort() ==
               ["ember.instance.id", "ember.node.id", "ember.pod.uid", "ember.size_class"]
           end)

    assert memory.observations
           |> Enum.map(fn {_value, labels} -> labels["ember.instance.id"] end)
           |> Enum.sort() == ["node-4/pod-new", "node-4/pod-old"]

    primed = Enum.find(gauges, &(&1.name == :"embervm.capacity.workload.primed_occupancy"))

    assert Enum.all?(primed.observations, fn {_value, labels} ->
             Map.keys(labels) |> Enum.sort() == ["ember.workload", "ember.workload.class"]
           end)

    demand = Enum.find(gauges, &(&1.name == :"embervm.capacity.demand"))

    assert {2,
            %{
              "ember.workload" => "wl-a",
              "ember.workload.class" => "task",
              "ember.demand.tier" => "floor"
            }} in demand.observations
  end

  test "desired_capacity parsing is typed and the knob has no report or scaling effect" do
    assert CapacityReport.parse_desired_capacity(nil) == {:ok, nil}
    assert CapacityReport.parse_desired_capacity("") == {:ok, nil}
    assert CapacityReport.parse_desired_capacity("0") == {:ok, 0}
    assert CapacityReport.parse_desired_capacity("12") == {:ok, 12}
    assert CapacityReport.parse_desired_capacity("-1") == {:error, :invalid_desired_capacity}
    assert CapacityReport.parse_desired_capacity("1.5") == {:error, :invalid_desired_capacity}

    assert CapacityReport.parse_horizon_seconds(nil) == {:ok, 3_600}
    assert CapacityReport.parse_horizon_seconds("86400") == {:ok, 86_400}
    assert CapacityReport.parse_horizon_seconds("0") == {:error, :invalid_capacity_horizon}

    opts = seed_tables()
    baseline = CapacityReport.build(opts)
    previous = Application.get_env(:embervm, :desired_capacity, :not_set)

    on_exit(fn ->
      case previous do
        :not_set -> Application.delete_env(:embervm, :desired_capacity)
        value -> Application.put_env(:embervm, :desired_capacity, value)
      end
    end)

    Application.put_env(:embervm, :desired_capacity, 99)
    assert CapacityReport.build(opts) == baseline
  end

  test "diagnostic gauges omit unsupported and overflowing values with bounded instance labels" do
    opts = seed_tables()
    table = Keyword.fetch!(opts, :capacity_table)
    {:ok, facts} = NodeCapacity.fetch(table, {"node-4", "pod-old"})
    NodeCapacity.put(table, {"node-4", "pod-old"}, Map.merge(facts, %{
      vmm_exit_counts: %{"host_requested" => 0, "host_cgroup_oom" => 9_223_372_036_854_775_807,
        "unclassified" => 18_446_744_073_709_551_615, "guest-label" => 1},
      guest_memory_state_counts: %{"pending" => 1, "ok" => 2, "stale" => 3, "unsupported" => 4, "error" => 5, "vm-id" => 1},
      guest_oom_count: 18_446_744_073_709_551_615
    }))
    report = CapacityReport.build(opts)
    assert report.semantics.diagnostics =~ "Diagnostic only"
    supported = Enum.find(report.instances, &(&1.pod_uid == "pod-old"))
    assert supported.guest_oom_count == 18_446_744_073_709_551_615
    assert map_size(supported.guest_memory_state_counts) == 5
    unsupported = Enum.find(report.instances, &(&1.pod_uid == "pod-new"))
    assert unsupported.vmm_exit_counts == nil
    assert unsupported.guest_oom_count == nil
    gauges = CapacityReport.gauge_observations(report)
    exits = Enum.find(gauges, &(&1.name == :"embervm.capacity.instance.vmm_exit_count"))
    assert Enum.sort(Enum.map(exits.observations, &elem(&1, 0))) == [0, 9_223_372_036_854_775_807]
    assert Enum.all?(exits.observations, fn {_, labels} ->
      Map.keys(labels) |> Enum.sort() == ["ember.instance.id", "ember.node.id", "ember.pod.uid", "ember.size_class", "reason"]
    end)
    states = Enum.find(gauges, &(&1.name == :"embervm.capacity.instance.guest_memory_state_count"))
    assert length(states.observations) == 5
    assert Enum.all?(states.observations, fn {_, labels} ->
      Map.keys(labels) |> Enum.sort() == ["ember.instance.id", "ember.node.id", "ember.pod.uid", "ember.size_class", "state"]
    end)
    oom = Enum.find(gauges, &(&1.name == :"embervm.capacity.instance.guest_oom_count"))
    assert oom.observations == []
    NodeCapacity.put(table, {"node-4", "pod-old"}, Map.put(supported, :guest_oom_count, 7))
    assert [{7, _}] = CapacityReport.build(opts) |> CapacityReport.gauge_observations() |>
      Enum.find(&(&1.name == :"embervm.capacity.instance.guest_oom_count")) |> Map.fetch!(:observations)
    NodeCapacity.drop(table, {"node-4", "pod-old"})
    assert Enum.all?(CapacityReport.build(opts) |> CapacityReport.gauge_observations() |>
      Enum.filter(&(&1.name in [exits.name, states.name, oom.name])), &(&1.observations == []))
  end
end
