defmodule Embervm.DispatcherTest do
  @moduledoc """
  Task 11 acceptance for `Embervm.Dispatcher`: warm + miss dispatch drive a task
  to terminal, fail-closed enforcement denies with distinct kinds, the caps bound
  in-flight and enforce per-principal fairness, and 1k tasks drain with zero lost.

  A FAKE DAEMON is injected via `channel_fun`/`assign_fun`/`prime_fun` (the same
  seam idiom the NodeRegistry/BaseBuilder tests use), a real (unnamed) op-log +
  TaskStore give the real FSM, and unique ETS table names + an unnamed store keep
  each test isolated from the application's own supervised dispatcher.
  """
  use ExUnit.Case, async: true

  alias Embervm.{Dispatcher, NodeCapacity, TaskStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.{AssignRequest, AssignResponse, GuestResponse, PrimeResponse, Trace, UsageStats}

  # -- harness ---------------------------------------------------------------

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"cap_#{suffix}"
    cat_table = :"cat_#{suffix}"
    depth_table = :"depth_#{suffix}"
    disp_name = :"disp_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_dispatcher_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = TaskStore.start_link(name: nil, op_log: op_log, on_queued: fn t -> Dispatcher.enqueue(disp_name, t) end)

    disp_opts =
      [
        name: disp_name,
        task_store: store,
        capacity_table: cap_table,
        catalog_table: cat_table,
        depth_table: depth_table,
        clock: fn -> 1_000_000 end,
        channel_fun: fn _node -> {:ok, :ch} end,
        assign_fun: Keyword.get(opts, :assign_fun, fn _ch, _req -> {:ok, success_resp()} end),
        prime_fun: Keyword.get(opts, :prime_fun, fn _ch, _req -> {:ok, %PrimeResponse{vm_id: "vm-#{System.unique_integer([:positive])}"}} end),
        start_sweep: false
      ] ++ Keyword.take(opts, [:queue_depth_cap, :share_fraction, :stale_after_ms])

    {:ok, disp} = Dispatcher.start_link(disp_opts)

    %{disp: disp, name: disp_name, store: store, cap_table: cap_table, cat_table: cat_table, depth_table: depth_table}
  end

  defp success_resp(code \\ 200, body \\ "ok") do
    %AssignResponse{
      response: %GuestResponse{status_code: code, headers: %{}, body: body},
      usage: %UsageStats{cpu_ms: 1, peak_rss_mib: 1, wall_ms: 1}
    }
  end

  defp put_facts(ctx, wl, opts \\ []) do
    node = Keyword.get(opts, :node, "node-4")

    NodeCapacity.put(ctx.cap_table, node, %{
      node_id: node,
      configured_id: node,
      workloads: %{
        wl => %{
          free_primed_slots: Keyword.get(opts, :free, 0),
          snapshot_ref: Keyword.get(opts, :snapshot_ref, "snap-#{wl}"),
          base_state: Keyword.get(opts, :base_state, :BASE_BUILD_STATE_READY)
        }
      },
      mem_headroom_mib: 4096,
      cpu_headroom_millicores: 4000,
      live_vms: Keyword.get(opts, :live, 0),
      max_live_vms: Keyword.get(opts, :max, 8),
      draining: Keyword.get(opts, :draining, false),
      updated_at: Keyword.get(opts, :updated_at, 1_000_000)
    })
  end

  defp put_catalog(ctx, wl, opts \\ []) do
    WorkloadCatalog.upsert(ctx.cat_table, wl, %{
      name: wl,
      namespace: "embervm",
      cap: Keyword.get(opts, :cap, 10),
      floor: Keyword.get(opts, :floor, 0),
      invoke_path: "/",
      timeout_ms: 5_000,
      result_ttl_ms: 60_000,
      result_max_bytes: Keyword.get(opts, :result_max_bytes, 1_048_576),
      retry: Keyword.get(opts, :retry, %{max_attempts: 3, backoff_ms: 1, backoff_cap_ms: 1, retry_on: [:transport, :timeout, :guest5xx]}),
      triggers: []
    })
  end

  defp submit(ctx, wl, principal) do
    {:ok, :created, tid} =
      TaskStore.submit(ctx.store, %{
        tenant: "t",
        principal: principal,
        workload: wl,
        request: %{path: "/", headers: %{}, body_b64: Base.encode64("")}
      })

    tid
  end

  defp state_of(ctx, tid) do
    case TaskStore.get(ctx.store, tid) do
      {:ok, %{state: s}} -> s
      _ -> :missing
    end
  end

  defp eventually(fun, timeout \\ 5_000, interval \\ 10) do
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

  # -- warm + miss -----------------------------------------------------------

  test "warm dispatch drives a queued task to succeeded and stores the result" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", free: 1)
    Dispatcher.deposit(ctx.name, "node-4", "wl-a", "vm-1")

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    assert {:ok, %{status_code: 200, body: "ok"}} = TaskStore.get_result(ctx.store, tid)
    assert Dispatcher.stats(ctx.name).warm_hits >= 1
  end

  test "miss path primes then assigns, counted as a miss" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", free: 0, live: 0, max: 8)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :succeeded end)
    stats = Dispatcher.stats(ctx.name)
    assert stats.misses >= 1
    assert stats.warm_hits == 0
  end

  # -- fail-closed -----------------------------------------------------------

  test "no capacity facts: task stays queued, denial recorded as :no_capacity" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    # No facts at all.

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> Dispatcher.stats(ctx.name).denials.no_capacity > 0 end)
    Process.sleep(50)
    assert state_of(ctx, tid) == :queued
  end

  test "stale capacity (> 15s since stamp): denial recorded as :stale_capacity" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    # Base ready, but stamped 20s before the dispatcher's clock (1_000_000).
    put_facts(ctx, "wl-a", free: 1, updated_at: 1_000_000 - 20_000)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> Dispatcher.stats(ctx.name).denials.stale_capacity > 0 end)
    assert state_of(ctx, tid) == :queued
  end

  test "draining node is not dispatchable (fail-closed)" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", free: 1, draining: true)

    tid = submit(ctx, "wl-a", "p1")
    assert eventually(fn -> Dispatcher.stats(ctx.name).denials.no_capacity > 0 end)
    assert state_of(ctx, tid) == :queued
  end

  # -- caps ------------------------------------------------------------------

  test "per-workload cap bounds in-flight tasks" do
    gate = new_gate()
    ctx = start_stack(assign_fun: gated_assign(gate))
    put_catalog(ctx, "wl-a", cap: 2)
    put_facts(ctx, "wl-a", live: 0, max: 8)

    tids = for _ <- 1..5, do: submit(ctx, "wl-a", "p1")

    # Exactly cap (2) go in-flight; the rest wait queued.
    assert eventually(fn -> Map.get(Dispatcher.stats(ctx.name).inflight_wl, "wl-a") == 2 end)
    assert Dispatcher.stats(ctx.name).queued == 3
    assert Dispatcher.stats(ctx.name).denials.cap > 0

    open_gate(gate)
    assert eventually(fn -> Enum.all?(tids, &(state_of(ctx, &1) == :succeeded)) end)
  end

  test "per-principal share cap splits a workload's cap across principals" do
    gate = new_gate()
    ctx = start_stack(assign_fun: gated_assign(gate))
    # cap 4, two active principals -> share = 2 each.
    put_catalog(ctx, "wl-a", cap: 4)
    put_facts(ctx, "wl-a", live: 0, max: 16)

    # A has 5 tasks, B has 1. With share 2, A is held to 2 in-flight even though 4
    # slots exist; B takes its 1. So the in-flight split is A:2, B:1 (not A:4).
    for _ <- 1..5, do: submit(ctx, "wl-a", "A")
    submit(ctx, "wl-a", "B")

    assert eventually(fn ->
             ifp = Dispatcher.stats(ctx.name).inflight_pr
             Map.get(ifp, {"wl-a", "A"}) == 2 and Map.get(ifp, {"wl-a", "B"}) == 1
           end)

    open_gate(gate)
    assert eventually(fn -> Dispatcher.stats(ctx.name).queued == 0 end)
  end

  # -- fairness (round-robin across principals, FIFO within) -----------------

  test "dispatch order is round-robin across principals, FIFO within a principal" do
    {:ok, order} = Agent.start_link(fn -> [] end)

    record_assign = fn _ch, %AssignRequest{trace: %Trace{task_id: tid}} ->
      Agent.update(order, &(&1 ++ [tid]))
      {:ok, success_resp()}
    end

    # cap 1 serializes dispatch so the recorded call order IS the dispatch order.
    ctx = start_stack(assign_fun: record_assign)
    put_catalog(ctx, "wl-a", cap: 1)

    # Submit with NO capacity first so all six queue before any dispatch, giving a
    # clean steady-state (both principals always have work) rather than a startup
    # artifact where one principal is served while alone.
    a = for _ <- 1..3, do: submit(ctx, "wl-a", "A")
    b = for _ <- 1..3, do: submit(ctx, "wl-a", "B")
    principal = Map.new(Enum.map(a, &{&1, "A"}) ++ Enum.map(b, &{&1, "B"}))

    # Wait until all six enqueue casts (in submit order) are processed, so the
    # fair-queue order is the submit order and not a random-task-id backlog sort.
    assert eventually(fn -> Dispatcher.stats(ctx.name).queued == 6 end)

    # Now grant capacity and kick a drain (the tasks are already queued, so the
    # sweep only triggers the drain; do_enqueue dedupes them).
    put_facts(ctx, "wl-a", live: 0, max: 8)
    Dispatcher.sweep(ctx.name)

    assert eventually(fn -> length(Agent.get(order, & &1)) == 6 end)

    principals = order |> Agent.get(& &1) |> Enum.map(&Map.get(principal, &1))
    assert principals == ["A", "B", "A", "B", "A", "B"]

    # And FIFO within a principal: A's three tasks ran in submit order.
    a_order = order |> Agent.get(& &1) |> Enum.filter(&(Map.get(principal, &1) == "A"))
    assert a_order == a
  end

  # -- drain integrity -------------------------------------------------------

  test "1000 tasks drain with zero lost, all terminal" do
    ctx = start_stack()
    put_catalog(ctx, "wl-a", cap: 200)
    put_facts(ctx, "wl-a", live: 0, max: 1_000_000)

    tids = for _ <- 1..1000, do: submit(ctx, "wl-a", "p1")

    assert eventually(
             fn ->
               s = Dispatcher.stats(ctx.name)
               s.queued == 0 and s.workers == 0 and map_size(s.inflight_wl) == 0
             end,
             30_000
           )

    succeeded = Enum.count(tids, &(state_of(ctx, &1) == :succeeded))
    assert succeeded == 1000
    assert {:ok, []} = TaskStore.list_backlog(ctx.store)
  end

  # -- failure classification ------------------------------------------------

  test "guest 5xx retries through Retry until attempts exhaust, then dead-letters" do
    {:ok, attempts} = Agent.start_link(fn -> 0 end)

    fail_5xx = fn _ch, _req ->
      Agent.update(attempts, &(&1 + 1))
      {:ok, success_resp(503, "boom")}
    end

    ctx = start_stack(assign_fun: fail_5xx)
    put_catalog(ctx, "wl-a", cap: 10, retry: %{max_attempts: 3, backoff_ms: 1, backoff_cap_ms: 1, retry_on: [:guest5xx]})
    put_facts(ctx, "wl-a", live: 0, max: 8)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :dead_lettered end)
    # Three attempts (1..max_attempts), each classified retryable until the last.
    assert Agent.get(attempts, & &1) == 3
  end

  test "guest 4xx is permanent (never retried)" do
    {:ok, attempts} = Agent.start_link(fn -> 0 end)

    fail_4xx = fn _ch, _req ->
      Agent.update(attempts, &(&1 + 1))
      {:ok, success_resp(400, "bad")}
    end

    ctx = start_stack(assign_fun: fail_4xx)
    put_catalog(ctx, "wl-a", cap: 10)
    put_facts(ctx, "wl-a", live: 0, max: 8)

    tid = submit(ctx, "wl-a", "p1")

    assert eventually(fn -> state_of(ctx, tid) == :dead_lettered end)
    assert Agent.get(attempts, & &1) == 1
  end

  # -- admit? 429 gate -------------------------------------------------------

  test "admit? denies once the per-principal queue-depth cap is reached" do
    ctx = start_stack(queue_depth_cap: 2)
    put_catalog(ctx, "wl-a", cap: 10)
    # No capacity, so submitted tasks stay queued and keep charging depth.

    assert Dispatcher.admit?(ctx.depth_table, "wl-a", "p1")
    submit(ctx, "wl-a", "p1")
    submit(ctx, "wl-a", "p1")

    assert eventually(fn -> not Dispatcher.admit?(ctx.depth_table, "wl-a", "p1") end)
    # A different principal is unaffected.
    assert Dispatcher.admit?(ctx.depth_table, "wl-a", "p2")
  end

  # -- gate helpers ----------------------------------------------------------

  defp new_gate do
    {:ok, agent} = Agent.start_link(fn -> false end)
    agent
  end

  defp open_gate(agent), do: Agent.update(agent, fn _ -> true end)

  # An assign_fun that blocks (holding the task in-flight) until the gate opens,
  # so a test can observe steady-state in-flight counts before completion.
  defp gated_assign(gate) do
    fn _ch, _req ->
      wait_open(gate)
      {:ok, success_resp()}
    end
  end

  defp wait_open(gate) do
    if Agent.get(gate, & &1) do
      :ok
    else
      Process.sleep(5)
      wait_open(gate)
    end
  end
end
