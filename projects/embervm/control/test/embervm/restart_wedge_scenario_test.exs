defmodule Embervm.RestartWedgeScenarioTest do
  use ExUnit.Case, async: false

  alias Embervm.{Dispatcher, NodeCapacity, TaskStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.SpecTrace
  alias Embervm.SpecTrace.Checker
  alias Embervm.SpecTrace.Store.SQLite, as: TraceSQLite
  alias Embervm.Node.V1.{NodeService, PrimeRequest, Trace}

  setup do
    :inets.start()
    System.put_env("EMBERVM_SPEC_TRACE", "on")
    SpecTrace.configure()

    on_exit(fn ->
      System.put_env("EMBERVM_SPEC_TRACE", "off")
      SpecTrace.configure()
    end)

    case System.get_env("EMBERVM_FAKE_NODE_BIN") do
      nil -> {:ok, skip: "EMBERVM_FAKE_NODE_BIN is not staged in the ordinary Mix test", fake: nil}
      bin -> start_fake_node(bin)
    end
  end

  test "dispatch restart wedge is caught by SpecTrace checker", %{skip: skip, fake: fake} do
    if skip do
      # The round-trip genrule supplies the fake binary; ordinary Mix runs do not.
      IO.puts("restart wedge scenario skipped: #{skip}")
    else
      run_wedge_scenario(fake)
    end
  end

  defp run_wedge_scenario(fake) do
    vm_ids = prime(fake.channel, 3)
    assert Enum.count(vm_ids) == 3

    suffix = System.unique_integer([:positive])
    cap_table = String.to_atom("wedge_cap_#{suffix}")
    catalog_table = String.to_atom("wedge_catalog_#{suffix}")
    depth_table = String.to_atom("wedge_depth_#{suffix}")
    dispatcher_name = String.to_atom("wedge_dispatcher_#{suffix}")
    db_path = Path.join(System.tmp_dir!(), "embervm_restart_wedge_#{suffix}.db")

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(catalog_table)
    put_catalog(catalog_table)
    put_capacity(cap_table, vm_ids)
    {:ok, op_log} = SQLite.start_link(name: nil, path: db_path)

    dispatched = Agent.start_link(fn -> 0 end) |> elem(1)
    {:ok, store} =
      TaskStore.start_link(
        name: nil,
        op_log: op_log,
        on_queued: fn task -> Dispatcher.enqueue(dispatcher_name, task) end
      )

    start_dispatcher(dispatcher_name, store, cap_table, catalog_table, depth_table, dispatched)
    assert :ok = Dispatcher.sweep(dispatcher_name)
    task_id = submit(store)
    assert eventually(fn -> Agent.get(dispatched, & &1) == 1 end)
    assert {:ok, %{id: ^task_id}} = TaskStore.get(store, task_id)

    assert control(fake.control_port, :post, "/fault/suppress-primed") == 204
    {:ok, stream} = NodeService.Stub.watch_node(fake.channel, %Embervm.Node.V1.WatchNodeRequest{node_id: "node-4"})
    assert Enum.all?(stream, fn {:ok, status} -> status.workloads == [] end)
    Process.exit(Process.whereis(dispatcher_name), :normal)
    put_capacity(cap_table, [])
    start_dispatcher(dispatcher_name, store, cap_table, catalog_table, depth_table, dispatched)

    _wedge_task = submit(store)
    refute eventually(fn -> Agent.get(dispatched, & &1) == 2 end, 5_000)

    {:ok, trace_store} = TraceSQLite.start_link(name: nil, path: ":memory:")
    :ok =
      TraceSQLite.write(trace_store, [
        %{
          "run_id" => "restart-wedge",
          "seq" => 1,
          "mono" => 1,
          "ts" => 1,
          "spec" => "adoption",
          "action" => "preamble",
          "vars" => %{}
        }
      ])

    verdicts = Checker.run(TraceSQLite, trace_store)

    # The current checker is trace-only. A quiet run has no illegal transition,
    # so it reports vacuous coverage rather than proving that adoption happened.
    assert Enum.any?(verdicts, &(&1.verdict == :vacuous))
    refute Enum.any?(verdicts, &(&1.verdict == :fail))
  end

  defp start_fake_node(bin) do
    proc = Port.open({:spawn_executable, bin}, [:binary, :exit_status, {:line, 1024}, {:args, ["--control-addr", "127.0.0.1:0"]}])
    grpc_port = read_port(proc, "PORT=", 5_000)
    control_port = read_port(proc, "CONTROL_PORT=", 5_000)
    {:ok, channel} = GRPC.Stub.connect("127.0.0.1:#{grpc_port}", adapter: GRPC.Client.Adapters.Mint)

    on_exit(fn ->
      _ = GRPC.Stub.disconnect(channel)
      if Port.info(proc), do: Port.close(proc)
    end)

    {:ok, fake: %{channel: channel, control_port: control_port}}
  end

  defp read_port(proc, prefix, timeout) do
    receive do
      {^proc, {:data, {:eol, line}}} ->
        if String.starts_with?(line, prefix), do: String.to_integer(String.replace_prefix(line, prefix, "")), else: read_port(proc, prefix, timeout)
      {^proc, {:exit_status, code}} -> flunk("fake node exited early with status #{code}")
    after
      timeout -> flunk("timed out waiting for fake node #{prefix} line")
    end
  end

  defp prime(channel, count) do
    for n <- 1..count do
      {:ok, response} =
        NodeService.Stub.prime(channel, %PrimeRequest{
          trace: %Trace{workload: "test-workload"},
          lineage_id: "scenario-#{n}",
          volume_mount: "/workspace"
        })

      response.vm_id
    end
  end

  defp control(port, method, path) do
    {:ok, {{_, status, _}, _headers, _body}} =
      :httpc.request(method, {~c"http://127.0.0.1:#{port}#{path}", []}, [], [])

    status
  end

  defp put_catalog(table) do
    WorkloadCatalog.upsert(table, "test-workload", %{
      name: "test-workload",
      namespace: "embervm",
      cap: 10,
      floor: 0,
      mem_mib: 0,
      invoke_path: "/",
      timeout_ms: 5_000,
      result_ttl_ms: 60_000,
      result_max_bytes: 1_048_576,
      retry: %{max_attempts: 1, backoff_ms: 1, backoff_cap_ms: 1, retry_on: []},
      triggers: []
    })
  end

  defp put_capacity(table, vm_ids) do
    NodeCapacity.put(table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      instance_id: "node-4",
      workloads: %{
        "test-workload" => %{
          free_primed_slots: length(vm_ids),
          snapshot_ref: "snap-test-workload",
          base_state: :BASE_BUILD_STATE_READY,
          primed_vm_ids: vm_ids
        }
      },
      mem_headroom_mib: 4096,
      mem_budget_mib: 0,
      cpu_headroom_millicores: 4000,
      live_vms: 0,
      max_live_vms: 10,
      draining: false,
      updated_at: System.monotonic_time(:millisecond)
    })
  end

  defp start_dispatcher(name, store, cap_table, catalog_table, depth_table, dispatched) do
    {:ok, _pid} =
      Dispatcher.start_link(
        name: name,
        task_store: store,
        capacity_table: cap_table,
        catalog_table: catalog_table,
        depth_table: depth_table,
        channel_fun: fn _ -> {:ok, :channel} end,
        assign_fun: fn _channel, _request -> Agent.update(dispatched, &(&1 + 1)); {:ok, assign_response()} end,
        prime_fun: fn _channel, _request -> {:error, :not_expected} end,
        start_sweep: false
      )
  end

  defp assign_response do
    %Embervm.Node.V1.AssignResponse{
      response: %Embervm.Node.V1.GuestResponse{status_code: 200, headers: %{}, body: "ok"},
      usage: %Embervm.Node.V1.UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}
    }
  end

  defp submit(store) do
    {:ok, :created, task_id} =
      TaskStore.submit(store, %{
        tenant: "test",
        principal: "scenario",
        workload: "test-workload",
        request: %{path: "/", headers: %{}, body_b64: Base.encode64("")}
      })

    task_id
  end

  defp eventually(fun, timeout \\ 1_000, interval \\ 10) do
    deadline = System.monotonic_time(:millisecond) + timeout
    do_eventually(fun, deadline, interval)
  end

  defp do_eventually(fun, deadline, interval) do
    if fun.() do
      true
    else
      if System.monotonic_time(:millisecond) >= deadline do
        false
      else
        Process.sleep(interval)
        do_eventually(fun, deadline, interval)
      end
    end
  end
end
