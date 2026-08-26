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

    # A REGISTERED writer, because SpecTrace.emit/3 resolves it via
    # Process.whereis/1. An unnamed writer means every emission silently finds
    # nothing, the scenario records zero events, and any conclusion drawn about
    # what the checker can see is a conclusion about an empty store.
    {:ok, trace_store} = TraceSQLite.start_link(name: nil, path: ":memory:")

    # A SCOPED writer name, so this scenario's trace contains only this
    # scenario's emissions.
    #
    # The writer used to be one global name resolved by Process.whereis, so
    # whichever test happened to have started a writer captured emissions from
    # every other test in the BEAM. The store was per-test; the trace was not.
    # This scenario caught it by asserting on its own trace and finding
    # health_monotonic failing for a node it never created, and the mixing cuts
    # both ways: a foreign record can just as easily supply the precondition an
    # invariant needs, so a green run was not evidence either. #4833.
    writer_name = :"wedge_trace_writer_#{System.unique_integer([:positive])}"

    trace_writer =
      start_supervised!(
        {SpecTrace.Writer,
         name: writer_name, store_mod: TraceSQLite, store: trace_store, batch_size: 1, flush_ms: 5}
      )

    SpecTrace.scope_writer(writer_name)
    on_exit(fn -> SpecTrace.scope_writer(nil) end)

    context = [trace_store: trace_store, trace_writer: trace_writer]

    case System.get_env("EMBERVM_FAKE_NODE_BIN") do
      nil ->
        {:ok, [skip: "EMBERVM_FAKE_NODE_BIN is not staged", fake: nil] ++ context}

      bin ->
        {:ok, fake_ctx} = start_fake_node(bin)
        {:ok, Keyword.merge(context, Enum.to_list(fake_ctx))}
    end
  end

  test "dispatch restart wedge starves adoption and the trace records it", ctx do
    # NOT a silent skip. mix_test.sh now stages the fake noded (bazel/erlang/BUILD
    # plus the EMBERVM_FAKE_NODE_SRC block), so an absent binary means that
    # staging broke, not that the environment is legitimately without it.
    #
    # The previous revision printed "skipped" and passed, so this scenario never
    # ran in CI once, while the suite reported green. A harness proof that can
    # excuse itself is the exact defect class this harness exists to catch.
    refute ctx[:skip],
           "the fake noded is not staged: #{ctx[:skip]}. mix_test.sh is supposed to " <>
             "export EMBERVM_FAKE_NODE_BIN from EMBERVM_FAKE_NODE_SRC. This scenario " <>
             "must not silently skip."

    run_wedge_scenario(ctx.fake, ctx.trace_store, ctx.trace_writer)
  end

  defp run_wedge_scenario(fake, trace_store, trace_writer) do
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
    assert {:ok, %{task_id: ^task_id, state: :succeeded}} = TaskStore.get(store, task_id)

    assert control(fake.control_port, :post, "/fault/suppress-primed") == 204
    {:ok, stream} = NodeService.Stub.watch_node(fake.channel, %Embervm.Node.V1.WatchNodeRequest{node_id: "node-4"})
    assert Enum.all?(stream, fn {:ok, status} -> status.workloads == [] end)
    crash_dispatcher(dispatcher_name)
    put_capacity(cap_table, [])
    start_dispatcher(dispatcher_name, store, cap_table, catalog_table, depth_table, dispatched)

    _wedge_task_id = submit(store)
    refute eventually(fn -> Agent.get(dispatched, & &1) == 2 end, 5_000)

    # Force enough sweeps to FORM a liveness window. `eventually_dispatched` is
    # bounded at K=2, so it needs K+1 checkpoints before it can say anything, and
    # a checkpoint is emitted once per sweep. The level predicate may still find
    # no queued window if the task leaves queued before these sweeps, but it needs
    # the checkpoints to distinguish that from a trace too short to scan.
    #
    # The fix belongs here rather than in K. Shrinking the bound so a thin
    # scenario passes is how a liveness gate ratchets itself into never firing,
    # which is #4759's override-rate kill point arriving by increments.
    for _ <- 1..(2 + 1), do: assert(:ok = Dispatcher.sweep(dispatcher_name))

    # The checker runs over the WEDGE'S OWN trace, captured by the writer started
    # in setup, never over a store this test fabricates. An earlier revision
    # created a fresh store, wrote one preamble record, ran the checker over that
    # and concluded "the checker cannot see the wedge". It could not see
    # anything: there was nothing there. Testing the wrong artifact produces a
    # finding about the wrong artifact.
    :ok = SpecTrace.drain(trace_writer)
    {:ok, records} = TraceSQLite.read_window(trace_store, spec: "adoption")

    assert records != [],
           "the wedge produced NO trace records, so any conclusion about what the " <>
             "checker can or cannot see is unfounded. Check the writer is running and " <>
             "the gate is on."

    checkpoints = Enum.filter(records, &(&1["action"] == "checkpoint"))

    assert checkpoints != [],
           "no checkpoint records. Checkpoint is emitted every sweep EVEN WHEN NOTHING " <>
             "ELSE FIRES precisely so that non-progress is observable, and a wedge is " <>
             "non-progress. Without it this scenario cannot be distinguished from a quiet " <>
             "healthy run by any means."

    verdicts = Checker.run(TraceSQLite, trace_store)

    # The liveness invariant should identify why the suppress-primed wedge task
    # cannot be judged from control-plane testimony.
    # Assert on the invariants that could plausibly see a wedge, NOT on the whole
    # verdict list.
    #
    # A blanket `refute any fail` caught health_monotonic failing with
    # "node node-4/uid-210 has age_to_down without preceding age_to_unknown",
    # for a node this scenario never created. `SpecTrace.Writer` is registered
    # under a GLOBAL name and `emit/3` resolves it with `Process.whereis/1`, so
    # this test's writer captures emissions from every other test's NodeRegistry
    # running in the same BEAM. The trace is shared even though the store is not.
    #
    # That ambient churn is a real limitation of the hermetic lane and is filed
    # separately. It does not affect the finding here: the wedge is about
    # adoption and dispatch, so those are what this asserts on.
    for invariant <- [:dispatch_provenance, :prime_before_checkpoint, :adopt_idempotent] do
      verdict = Enum.find(verdicts, &(&1.invariant == invariant))

      assert verdict.verdict in [:pass, :vacuous],
             "#{invariant} returned #{inspect(verdict.verdict)} on the wedge. If an " <>
               "invariant has started detecting non-progress, the comment above is " <>
               "stale and this scenario should assert the detection instead: " <>
               "#{inspect(verdict)}"
    end

    wedge_verdict = Enum.find(verdicts, &(&1.invariant == :eventually_dispatched))
    assert wedge_verdict, "eventually_dispatched returned no verdict on wedge scenario"

    # A suppress-primed wedge means noded stops reporting its VMs, so the control
    # plane's warm inventory genuinely EMPTIES. Capacity still advertises a ready
    # base, so this scenario takes the cold-miss path. Its test prime_fun returns
    # :not_expected and max_attempts is one, which removes the task from queued
    # before it occupies K+1 checkpoints. The healthy pre-wedge task also drains
    # between sweeps. With no persistent queued level, the invariant is vacuous
    # for insufficient window coverage, not for inventory. `oracle: :trace_only`
    # is exactly this limitation, the control plane testifying about itself.
    #
    # Distinguishing "the node hid its VMs" from "there are no VMs" requires
    # asking the NODE, which is the tier-3 reconciliation against /v1/nodes that
    # #4812 records as unsatisfied. No amount of liveness checking over the CP's
    # own trace closes it.
    #
    # What eventually_dispatched DOES catch is the other wedge class: inventory
    # the control plane can SEE and never dispatches. The reachability manifest's
    # fail scenario proves that direction, and it is the one a dispatcher bug
    # produces.
    assert wedge_verdict.verdict == :vacuous,
           "expected vacuous because no task stayed queued for K+1 checkpoints, " <>
             "got #{inspect(wedge_verdict.verdict)}: #{inspect(wedge_verdict.detail)}"

    assert wedge_verdict.coverage == 0

    assert wedge_verdict.detail =~ "3 consecutive checkpoints"
    refute wedge_verdict.detail =~ "inventory"

    pre_wedge_checkpoints = Enum.filter(records, &(&1["action"] == "checkpoint")) |> Enum.take(2)
    pre_wedge_dispatches = Enum.filter(records, &(&1["action"] in ["dispatch_warm", "dispatch_miss"])) |> Enum.take(1)

    assert length(pre_wedge_dispatches) >= 1, "pre-wedge phase should have had a dispatch"
    assert length(pre_wedge_checkpoints) >= 1, "pre-wedge phase should have had checkpoints"
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

  # A CRASH, and it must be observed to have happened before restarting.
  #
  # `Process.exit(pid, :normal)` from another process is IGNORED unless the
  # target traps exits, so the dispatcher survived and the restart failed with
  # {:error, {:already_started, pid}}. It also modelled the wrong thing:
  # adoption.tla's CrashCP is a crash, not an orderly shutdown, and the whole
  # point of this scenario is what the control plane does after dying.
  #
  # Monitoring and waiting for the :DOWN is what makes the restart deterministic
  # rather than a race against process teardown.
  defp crash_dispatcher(name) do
    pid = Process.whereis(name)

    # UNLINK first. The dispatcher is started with start_link, so it is linked to
    # this test process, and a :kill exit propagates down the link and takes the
    # test with it (** (EXIT from #PID<...>) killed). Monitoring is what we want
    # here instead: observe the death without sharing it.
    Process.unlink(pid)
    ref = Process.monitor(pid)
    Process.exit(pid, :kill)

    receive do
      {:DOWN, ^ref, :process, ^pid, _reason} -> :ok
    after
      5_000 -> flunk("dispatcher #{name} did not die within 5s")
    end
  end

  # :httpc wants a FOUR-tuple for methods that carry a body and a two-tuple for
  # those that do not. Passing the two-tuple form for :post returns
  # {:error, :invalid_request} before anything reaches the network, which reads
  # like the control port rejecting the lever rather than the client refusing to
  # send it.
  defp control(port, method, path) do
    url = ~c"http://127.0.0.1:#{port}#{path}"

    request =
      case method do
        :post -> {url, [], ~c"application/json", ~c""}
        _ -> {url, []}
      end

    {:ok, {{_, status, _}, _headers, _body}} = :httpc.request(method, request, [], [])
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
