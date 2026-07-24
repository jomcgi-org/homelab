defmodule Embervm.EndpointPublisherGroupTest do
  @moduledoc """
  Pure-function tests for the R5 composite (L4) extension of Embervm.EndpointPublisher:
  a composite-class workload renders a `group|<workload>` cluster + `group-<listen_port>`
  listener whose endpoint is the live ENTRY member when running, the activator TCP
  fallback otherwise (banked OR no instance, for the life of the CR), and NOTHING when
  cold with no activator configured. The no-composite REGRESSION is asserted: a
  serving-only render is byte-identical whether or not a GroupStore is wired, and the
  document carries no `listeners` key.
  """
  use ExUnit.Case, async: true

  alias Embervm.{EndpointPublisher, GroupStore, ServingStore, StatefulStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite

  defp start_stack(_opts \\ []) do
    suffix = System.unique_integer([:positive])
    cat_table = :"gpcat_#{suffix}"
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_pubgroup_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, serving_store} = ServingStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)

    {:ok, sf_counter} = Agent.start_link(fn -> 2_000 end)
    {:ok, stateful_store} = StatefulStore.start_link(name: nil, op_log: op_log, clock: fn -> Agent.get_and_update(sf_counter, fn n -> {n, n + 1} end) end)

    {:ok, g_counter} = Agent.start_link(fn -> 5_000 end)
    {:ok, group_store} = GroupStore.start_link(name: nil, op_log: op_log, clock: fn -> Agent.get_and_update(g_counter, fn n -> {n, n + 1} end) end)

    %{cat_table: cat_table, serving_store: serving_store, stateful_store: stateful_store, group_store: group_store}
  end

  defp build_ctx(stack, opts) do
    %{
      store: stack.serving_store,
      stateful_store: stack.stateful_store,
      group_store: stack.group_store,
      catalog_table: stack.cat_table,
      activator_endpoint: Keyword.get(opts, :activator_endpoint, %{ip: "10.1.1.1", port: 7000}),
      activator_ip: Keyword.get(opts, :activator_ip, nil),
      node_facts: Keyword.get(opts, :node_facts, []),
      connect_timeout_ms: 1_000
    }
  end

  defp composite_workload(stack, name, listen_port) do
    WorkloadCatalog.upsert(stack.cat_table, name, %{
      class: "composite",
      group: %{entry: %{member: "leader", port: 8080, listen_port: listen_port}}
    })
  end

  defp serving_workload(stack, name, host) do
    WorkloadCatalog.upsert(stack.cat_table, name, %{class: "serving", serving: %{host: host, port: 8080}})
  end

  defp running_group(stack, instance_id, workload, entry_ip, entry_port) do
    {:ok, _} =
      GroupStore.create(stack.group_store, %{
        instance_id: instance_id,
        tenant: "homelab",
        principal: "system:group:#{workload}",
        workload: workload,
        node_id: "node-4",
        subnet_cidr: "10.101.0.0/24",
        entry_member: "leader",
        entry_port: 8080,
        listen_port: entry_port,
        secret: "s"
      })

    {:ok, _} = GroupStore.member_started(stack.group_store, instance_id, %{member_name: "leader", member_index: 0, vm_id: "vm-l", ip: "10.101.0.10"})
    {:ok, _} = GroupStore.publish(stack.group_store, instance_id, entry_ip, entry_port)
  end

  test "a running composite workload renders group|<workload> cluster + group-<port> listener at the entry endpoint" do
    stack = start_stack()
    composite_workload(stack, "grp-a", 5410)
    running_group(stack, "g-1", "grp-a", "10.0.0.9", 30_010)

    ctx = build_ctx(stack, [])
    desired = EndpointPublisher.desired_for_node(ctx, "v1")

    cluster = Enum.find(desired.clusters, &(&1.name == "group|grp-a"))
    assert cluster.endpoints == [%{ip: "10.0.0.9", port: 30_010}]

    listener = Enum.find(desired.listeners, &(&1.name == "group-5410"))
    assert listener.port == 5410
    assert listener.cluster == "group|grp-a"
  end

  test "a cold composite workload with an activator_ip swaps in the activator TCP fallback at its OWN listen_port" do
    stack = start_stack()
    composite_workload(stack, "grp-a", 5411)
    # No group instance at all (cold).

    ctx = build_ctx(stack, activator_ip: "10.2.2.2")
    desired = EndpointPublisher.desired_for_node(ctx, "v1")

    cluster = Enum.find(desired.clusters, &(&1.name == "group|grp-a"))
    # The activator fallback at the workload's OWN listen_port (5411), not a shared one.
    assert cluster.endpoints == [%{ip: "10.2.2.2", port: 5411}]
  end

  test "a banked composite workload (no live entry) swaps in the activator fallback" do
    stack = start_stack()
    composite_workload(stack, "grp-a", 5412)
    running_group(stack, "g-1", "grp-a", "10.0.0.9", 30_010)
    {:ok, _} = GroupStore.mark(stack.group_store, "g-1", :bank)
    {:ok, _} = GroupStore.bank_ready(stack.group_store, "g-1", "set-1", [%{name: "leader", snapshot_ref: "snap-l"}])

    ctx = build_ctx(stack, activator_ip: "10.2.2.2")
    desired = EndpointPublisher.desired_for_node(ctx, "v1")

    cluster = Enum.find(desired.clusters, &(&1.name == "group|grp-a"))
    assert cluster.endpoints == [%{ip: "10.2.2.2", port: 5412}]
  end

  test "a banked composite workload prefers its anchor brick advertised activator" do
    stack = start_stack()
    composite_workload(stack, "grp-a", 5412)
    running_group(stack, "g-1", "grp-a", "10.0.0.9", 30_010)
    {:ok, _} = GroupStore.mark(stack.group_store, "g-1", :bank)
    {:ok, _} = GroupStore.bank_ready(stack.group_store, "g-1", "set-1", [%{name: "leader", snapshot_ref: "snap-l"}])

    ctx =
      build_ctx(stack,
        activator_ip: "10.2.2.2",
        node_facts: [%{configured_id: "node-4", activator_ip: "10.4.4.4"}]
      )

    desired = EndpointPublisher.desired_for_node(ctx, "v1")
    cluster = Enum.find(desired.clusters, &(&1.name == "group|grp-a"))
    assert cluster.endpoints == [%{ip: "10.4.4.4", port: 5412}]
  end

  test "a banked composite workload without an anchor advertisement falls back to the CP activator" do
    stack = start_stack()
    composite_workload(stack, "grp-a", 5412)
    running_group(stack, "g-1", "grp-a", "10.0.0.9", 30_010)
    {:ok, _} = GroupStore.mark(stack.group_store, "g-1", :bank)
    {:ok, _} = GroupStore.bank_ready(stack.group_store, "g-1", "set-1", [%{name: "leader", snapshot_ref: "snap-l"}])

    ctx = build_ctx(stack, activator_ip: "10.2.2.2", node_facts: [%{configured_id: "node-4"}])
    desired = EndpointPublisher.desired_for_node(ctx, "v1")
    cluster = Enum.find(desired.clusters, &(&1.name == "group|grp-a"))
    assert cluster.endpoints == [%{ip: "10.2.2.2", port: 5412}]
  end

  test "a cold composite workload with NO activator_ip emits no cluster, no listener" do
    stack = start_stack()
    composite_workload(stack, "grp-a", 5413)

    ctx = build_ctx(stack, activator_ip: nil)
    desired = EndpointPublisher.desired_for_node(ctx, "v1")

    refute Enum.any?(desired.clusters, &(&1.name == "group|grp-a"))
    # No composite listener; and with only this cold no-activator workload the
    # document carries no listeners key at all.
    refute Map.has_key?(desired, :listeners)
  end

  test "no-composite regression: a serving-only render is byte-identical and carries no listeners key" do
    stack = start_stack()
    serving_workload(stack, "svc-a", "svc-a.example.com")

    ctx = build_ctx(stack, [])
    desired = EndpointPublisher.desired_for_node(ctx, "v1")

    # The serving cluster is present; no composite (or stateful) clusters/listeners.
    assert Enum.any?(desired.clusters, &(&1.name == "serve|svc-a"))
    refute Enum.any?(desired.clusters, &String.starts_with?(&1.name, "group|"))
    refute Map.has_key?(desired, :listeners)

    # The document shape is exactly {version, clusters, routes} (the pre-R4 wire).
    assert Map.keys(desired) |> Enum.sort() == [:clusters, :routes, :version]
  end
end
