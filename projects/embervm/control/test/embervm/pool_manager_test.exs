defmodule Embervm.PoolManagerTest do
  @moduledoc """
  Task 11 acceptance for `Embervm.PoolManager`: floor-first refill, the
  floor-isolation property (one workload's burst does not drain another's floor),
  proportional surplus by queue depth, and primedFloorSatisfied status.

  The daemon is faked through `prime_fun` (records which workload each prime is
  for) and `deposit_fun` (no-op); capacity + catalog + queue-depth are seeded
  directly in unique ETS tables. Refill is driven synchronously via `refill/1`.
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, PoolManager, WorkloadCatalog}
  alias Embervm.Scheduler.Inflight
  alias Embervm.Node.V1.{PrimeRequest, PrimeResponse, Trace}

  defmodule OpLogRecorder do
    def append(agent, op) do
      Agent.update(agent, &[op | &1])
      :ok
    end
  end

  defp start_pool(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"pcap_#{suffix}"
    cat_table = :"pcat_#{suffix}"
    depth_table = :"pdepth_#{suffix}"
    # A private in-flight ledger: this module is async, and a shared one would
    # let one test's held claims debit another test's identically named bricks.
    inflight_table = Inflight.create(:"pinfl_#{suffix}")

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)
    :ets.new(depth_table, [:set, :public, :named_table])

    {:ok, primes} = Agent.start_link(fn -> [] end)
    {:ok, status} = Agent.start_link(fn -> [] end)

    default_prime_fun = fn _ch, %PrimeRequest{trace: %Trace{workload: wl}} ->
      Agent.update(primes, &[wl | &1])
      {:ok, %PrimeResponse{vm_id: "vm-#{System.unique_integer([:positive])}"}}
    end

    prime_fun = Keyword.get(opts, :prime_fun, default_prime_fun)
    invalidate_fun = Keyword.get(opts, :invalidate_fun, fn _node, _chan -> :ok end)
    channel_fun = Keyword.get(opts, :channel_fun, fn _ -> {:ok, :ch} end)

    status_writer = fn _ns, name, map ->
      Agent.update(status, &[{name, map} | &1])
      :ok
    end

    {:ok, pool} =
      PoolManager.start_link(
        [
          name: nil,
          capacity_table: cap_table,
          catalog_table: cat_table,
          depth_table: depth_table,
          inflight_table: inflight_table,
          clock: fn -> 1_000_000 end,
          channel_fun: channel_fun,
          invalidate_fun: invalidate_fun,
          prime_fun: prime_fun,
          deposit_fun: fn _srv, _node, _wl, _vm -> :ok end,
          reservation_target_fun:
            Keyword.get(opts, :reservation_target_fun, fn _instance, _workload, _count, _mem_mib -> :ok end),
          op_log: Keyword.get(opts, :op_log),
          op_log_mod: Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite),
          tenant: Keyword.get(opts, :tenant, "homelab"),
          monotonic_clock: Keyword.get(opts, :monotonic_clock, fn -> 2_000_000 end),
          status_writer: status_writer,
          max_concurrent_primes: Keyword.get(opts, :max_concurrent_primes, 100),
          start_refill: false
        ]
      )

    %{
      pool: pool,
      cap_table: cap_table,
      cat_table: cat_table,
      depth_table: depth_table,
      inflight_table: inflight_table,
      primes: primes,
      status: status
    }
  end

  defp put_catalog(ctx, wl, floor, opts \\ []) do
    resources = Keyword.get(opts, :resources, %{})

    WorkloadCatalog.upsert(ctx.cat_table, wl, %{
      name: wl,
      namespace: "embervm",
      floor: floor,
      cap: 100,
      invoke_path: "/",
      resources: resources,
      # Flattened exactly as Embervm.WorkloadWatcher's catalog_entry does
      # (`mem_mib: Map.get(resources, "memMib")`), so these fixtures carry the same
      # entry shape the pool actually reads in production. Keeping only the nested
      # `resources` map here would have let the memory gate silently read nil and
      # pass everything, which is the bug these tests exist to catch.
      mem_mib: Map.get(resources, "memMib")
    })
  end

  defp put_facts(ctx, workloads, opts) do
    put_instance_facts(ctx, "node-4", workloads, opts)
  end

  defp put_instance_facts(ctx, instance_id, workloads, opts) do
    wl_map =
      for {wl, free} <- workloads, into: %{} do
        ready = Keyword.get(opts, :ready, true)
        state = if ready, do: :BASE_BUILD_STATE_READY, else: :BASE_BUILD_STATE_BUILDING
        {wl, %{free_primed_slots: free, snapshot_ref: "snap-#{wl}", base_state: state}}
      end

    NodeCapacity.put(ctx.cap_table, {instance_id, instance_id}, %{
      node_id: instance_id,
      instance_id: instance_id,
      configured_id: instance_id,
      workloads: wl_map,
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8192),
      cpu_headroom_millicores: 8000,
      live_vms: Keyword.get(opts, :live, 0),
      max_live_vms: Keyword.fetch!(opts, :max),
      size_class: Keyword.get(opts, :size_class, ""),
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 0),
      draining: false,
      updated_at: 1_000_000
    }
    |> Map.merge(Keyword.get(opts, :extra, %{})))
  end

  defp set_depth(ctx, wl, n), do: :ets.insert(ctx.depth_table, {{wl, "p"}, n})

  defp prime_counts(ctx) do
    Process.sleep(50)
    ctx.primes |> Agent.get(& &1) |> Enum.frequencies()
  end

  defp eventually(fun, timeout \\ 1_000) do
    deadline = System.monotonic_time(:millisecond) + timeout
    do_eventually(fun, deadline)
  end

  defp do_eventually(fun, deadline) do
    if fun.() do
      true
    else
      if System.monotonic_time(:millisecond) >= deadline do
        false
      else
        Process.sleep(10)
        do_eventually(fun, deadline)
      end
    end
  end

  test "a transport-dead Prime invalidates the channel so the next tick re-dials" do
    # Regression for the noded-pod-restart wedge: a Prime failing because the channel's
    # transport is dead (wrapped by the Mint adapter as an RPCError) must tear the
    # cached channel down; otherwise every refill reuses the dead channel forever.
    test_pid = self()
    dead = %GRPC.RPCError{status: 2, message: "error occurred while receiving data: the connection is closed"}

    ctx =
      start_pool(
        prime_fun: fn _ch, _req -> {:error, dead} end,
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end
      )

    put_catalog(ctx, "wl", 1)
    put_facts(ctx, [{"wl", 0}], max: 10)
    :ok = PoolManager.refill(ctx.pool)

    assert_receive {:invalidated, "node-4", :ch}, 1_000
  end

  test "records primed op on successful refill prime" do
    {:ok, op_log} = Agent.start_link(fn -> [] end)
    ctx = start_pool(op_log: op_log, op_log_mod: OpLogRecorder)
    put_catalog(ctx, "wl-prime", 1)
    put_facts(ctx, [{"wl-prime", 0}], max: 10)

    :ok = PoolManager.refill(ctx.pool)

    assert eventually(fn -> Agent.get(op_log, &length/1) == 1 end)
    assert [%{kind: :primed, workload: "wl-prime", tenant: "homelab", payload: payload}] = Agent.get(op_log, & &1)
    assert payload.lane == :task
    assert payload.workload == "wl-prime"
    assert payload.node_id == "node-4"
    assert String.starts_with?(payload.vm_id, "vm-")
  end

  test "a server-status Prime failure leaves the channel up (no needless invalidate)" do
    test_pid = self()
    server_err = %GRPC.RPCError{status: 9, message: "snapshot lost"}

    ctx =
      start_pool(
        prime_fun: fn _ch, _req -> {:error, server_err} end,
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end
      )

    put_catalog(ctx, "wl", 1)
    put_facts(ctx, [{"wl", 0}], max: 10)
    :ok = PoolManager.refill(ctx.pool)

    refute_receive {:invalidated, _, _}, 300
  end

  test "floor-first: primes each workload up to its floor" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)
    put_catalog(ctx, "wl-b", 3)
    put_facts(ctx, %{"wl-a" => 0, "wl-b" => 0}, max: 20)

    :ok = PoolManager.refill(ctx.pool)

    counts = prime_counts(ctx)
    assert counts["wl-a"] == 2
    assert counts["wl-b"] == 3
  end

  test "floor isolation: a burst workload does not drain another workload's floor" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)
    put_catalog(ctx, "wl-b", 2)
    # Budget exactly funds both floors (2 + 2). wl-a has a huge queue-depth burst.
    put_facts(ctx, %{"wl-a" => 0, "wl-b" => 0}, max: 4)
    set_depth(ctx, "wl-a", 500)
    set_depth(ctx, "wl-b", 0)

    :ok = PoolManager.refill(ctx.pool)

    counts = prime_counts(ctx)
    # wl-b keeps its full floor of 2; wl-a's burst does NOT push it past its floor
    # into wl-b's budget (surplus runs only after every floor is funded).
    assert counts["wl-a"] == 2
    assert counts["wl-b"] == 2
  end

  test "surplus is proportional to queue depth, after floors" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)
    put_catalog(ctx, "wl-b", 2)
    # Budget 6 = both floors (4) + 2 surplus. Only wl-a has queue depth.
    put_facts(ctx, %{"wl-a" => 0, "wl-b" => 0}, max: 6)
    set_depth(ctx, "wl-a", 50)
    set_depth(ctx, "wl-b", 0)

    :ok = PoolManager.refill(ctx.pool)

    counts = prime_counts(ctx)
    # Floors first (a:2, b:2), then both surplus VMs go to wl-a (the only depth).
    assert counts["wl-a"] == 4
    assert counts["wl-b"] == 2
  end

  test "does not prime when the floor is already satisfied" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)
    put_facts(ctx, %{"wl-a" => 2}, max: 10)

    :ok = PoolManager.refill(ctx.pool)

    assert prime_counts(ctx) == %{}
  end

  test "publishes pool targets and zeros them when the workload retires" do
    parent = self()

    ctx =
      start_pool(
        reservation_target_fun: fn instance, workload, count, mem_mib ->
          send(parent, {:pool_target, instance, workload, count, mem_mib})
        end
      )

    put_catalog(ctx, "wl-a", 1, resources: %{"memMib" => 512})
    put_facts(ctx, [{"wl-a", 0}], max: 4)

    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:pool_target, "node-4", "wl-a", 1, 512}

    WorkloadCatalog.drop(ctx.cat_table, "wl-a")
    put_facts(ctx, [], max: 4)
    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:pool_target, "node-4", "wl-a", 0, 512}
  end

  test "zeros pool targets when a brick leaves capacity" do
    parent = self()

    ctx =
      start_pool(
        reservation_target_fun: fn instance, workload, count, mem_mib ->
          send(parent, {:pool_target, instance, workload, count, mem_mib})
        end
      )

    put_catalog(ctx, "wl-a", 1, resources: %{"memMib" => 256})
    put_facts(ctx, [{"wl-a", 1}], max: 4)
    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:pool_target, "node-4", "wl-a", 1, 256}

    NodeCapacity.drop(ctx.cap_table, {"node-4", "node-4"})
    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:pool_target, "node-4", "wl-a", 0, 256}
  end

  test "writes primedFloorSatisfied, only on a flip" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)
    put_facts(ctx, %{"wl-a" => 2}, max: 10)

    :ok = PoolManager.refill(ctx.pool)
    Process.sleep(20)
    writes1 = Agent.get(ctx.status, & &1)
    assert Enum.any?(writes1, fn {name, map} -> name == "wl-a" and map["primedFloorSatisfied"] == true end)

    # A second refill with the same (satisfied) state writes nothing new.
    :ok = PoolManager.refill(ctx.pool)
    Process.sleep(20)
    assert Agent.get(ctx.status, & &1) == writes1
  end

  test "primedFloorSatisfied resets from catalog truth when a wiped base disappears" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 1)
    put_facts(ctx, [{"wl-a", 1}], max: 4)

    PoolManager.refill(ctx.pool)
    assert eventually(fn ->
             Enum.any?(Agent.get(ctx.status, & &1), fn
               {"wl-a", %{"primedFloorSatisfied" => true}} -> true
               _ -> false
             end)
           end)

    put_facts(ctx, [], max: 4)
    PoolManager.refill(ctx.pool)

    assert eventually(fn ->
             Enum.any?(Agent.get(ctx.status, & &1), fn
               {"wl-a", %{"primedFloorSatisfied" => false}} -> true
               _ -> false
             end)
           end)
  end

  test "fleet-wide floor spreads four total across two equal wildcard instances" do
    parent = self()
    ctx = start_pool(channel_fun: fn node -> {:ok, node} end,
      prime_fun: fn node, %PrimeRequest{trace: %Trace{workload: wl}} ->
        send(parent, {:prime, node, wl})
        {:ok, %PrimeResponse{vm_id: "vm"}}
      end)
    put_catalog(ctx, "wl-a", 4)
    put_instance_facts(ctx, "i-1", [{"wl-a", 0}], max: 5)
    put_instance_facts(ctx, "i-2", [{"wl-a", 0}], max: 5)

    :ok = PoolManager.refill(ctx.pool)
    primes = for _ <- 1..4, do: (receive do {:prime, node, "wl-a"} -> node end)
    assert Enum.frequencies(primes) == %{"i-1" => 2, "i-2" => 2}
  end

  test "packs primes onto the fuller instance until its memory makes it ineligible" do
    parent = self()
    ctx = start_pool(channel_fun: fn node -> {:ok, node} end,
      prime_fun: fn node, %PrimeRequest{trace: %Trace{workload: wl}} ->
        send(parent, {:prime, node, wl})
        {:ok, %PrimeResponse{vm_id: "vm-#{System.unique_integer([:positive])}"}}
      end)

    put_catalog(ctx, "wl-a", 3, resources: %{"memMib" => 2_048})
    put_instance_facts(ctx, "full", [{"wl-a", 0}], max: 5, live: 1, mem_budget_mib: 8_192, mem_headroom_mib: 2_048, size_class: "8gi")
    put_instance_facts(ctx, "empty", [{"wl-a", 0}], max: 5, live: 0, mem_budget_mib: 8_192, mem_headroom_mib: 8_192, size_class: "8gi")

    :ok = PoolManager.refill(ctx.pool)
    primes = for _ <- 1..3, do: (receive do {:prime, node, "wl-a"} -> node end)
    assert Enum.frequencies(primes) == %{"full" => 1, "empty" => 2}
  end

  test "a third instance does not multiply an existing fleet floor" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 4)
    put_instance_facts(ctx, "i-1", [{"wl-a", 2}], max: 5)
    put_instance_facts(ctx, "i-2", [{"wl-a", 2}], max: 5)
    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{}

    put_instance_facts(ctx, "i-3", [{"wl-a", 0}], max: 5)
    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{}
  end

  test "fleet allocation respects each instance budget" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 4)
    put_instance_facts(ctx, "i-1", [{"wl-a", 0}], max: 1)
    put_instance_facts(ctx, "i-2", [{"wl-a", 0}], max: 5)

    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{"wl-a" => 4}
  end

  test "not-base-ready instances are excluded from fleet totals and placement" do
    parent = self()
    ctx = start_pool(channel_fun: fn node -> {:ok, node} end,
      prime_fun: fn node, %PrimeRequest{trace: %Trace{workload: wl}} ->
        send(parent, {:prime, node, wl})
        {:ok, %PrimeResponse{vm_id: "vm"}}
      end)
    put_catalog(ctx, "wl-a", 4)
    put_instance_facts(ctx, "ready", [{"wl-a", 0}], max: 5)
    put_instance_facts(ctx, "building", [{"wl-a", 5}], max: 5, ready: false)

    :ok = PoolManager.refill(ctx.pool)
    primes = for _ <- 1..4, do: (receive do {:prime, node, "wl-a"} -> node end)
    assert primes == ["ready", "ready", "ready", "ready"]
  end

  test "primedFloorSatisfied uses the fleet total" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 4)
    put_instance_facts(ctx, "i-1", [{"wl-a", 2}], max: 5)
    put_instance_facts(ctx, "i-2", [{"wl-a", 2}], max: 5)

    :ok = PoolManager.refill(ctx.pool)
    assert Enum.any?(Agent.get(ctx.status, & &1), fn {name, map} ->
             name == "wl-a" and map["primedFloorSatisfied"] == true
           end)
  end

  test "fleet-wide floor isolation protects every workload floor" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)
    put_catalog(ctx, "wl-b", 2)
    put_instance_facts(ctx, "i-1", [{"wl-a", 0}, {"wl-b", 0}], max: 2)
    put_instance_facts(ctx, "i-2", [{"wl-a", 0}, {"wl-b", 0}], max: 2)
    set_depth(ctx, "wl-a", 500)
    set_depth(ctx, "wl-b", 0)

    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{"wl-a" => 2, "wl-b" => 2}
  end

  test "stale or draining capacity is not refilled (fail-closed)" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)

    NodeCapacity.put(ctx.cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: %{"wl-a" => %{free_primed_slots: 0, snapshot_ref: "snap", base_state: :BASE_BUILD_STATE_READY}},
      mem_headroom_mib: 8192,
      cpu_headroom_millicores: 8000,
      live_vms: 0,
      max_live_vms: 10,
      draining: false,
      updated_at: 1_000_000 - 30_000
    })

    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{}
  end

  test "does not prime a workload onto a classed instance too small for its memory need" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2, resources: %{"memMib" => 1024})
    put_instance_facts(ctx, "small", [{"wl-a", 0}], max: 10,
      size_class: "2gi", mem_budget_mib: 2048, mem_headroom_mib: 512)

    :ok = PoolManager.refill(ctx.pool)

    assert prime_counts(ctx) == %{}
  end

  test "primes a workload onto a larger instance when a smaller one has insufficient memory" do
    parent = self()
    ctx = start_pool(channel_fun: fn node -> {:ok, node} end,
      prime_fun: fn node, %PrimeRequest{trace: %Trace{workload: wl}} ->
        send(parent, {:prime, node, wl})
        {:ok, %PrimeResponse{vm_id: "vm"}}
      end)
    put_catalog(ctx, "wl-a", 2, resources: %{"memMib" => 1024})
    put_instance_facts(ctx, "small", [{"wl-a", 0}], max: 10,
      size_class: "2gi", mem_budget_mib: 2048, mem_headroom_mib: 512)
    put_instance_facts(ctx, "large", [{"wl-a", 0}], max: 20,
      size_class: "16gi", mem_budget_mib: 16_384, mem_headroom_mib: 4096)

    :ok = PoolManager.refill(ctx.pool)

    primes = for _ <- 1..2, do: (receive do {:prime, node, "wl-a"} -> node end)
    assert Enum.frequencies(primes) == %{"large" => 2}
  end

  test "wildcard brick (size_class empty, mem_budget_mib zero) is always memory-eligible" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2, resources: %{"memMib" => 4096})
    put_instance_facts(ctx, "wildcard", [{"wl-a", 0}], max: 10,
      size_class: "", mem_budget_mib: 0, mem_headroom_mib: 1)

    :ok = PoolManager.refill(ctx.pool)

    assert prime_counts(ctx) == %{"wl-a" => 2}
  end

  test "workload with no memMib in catalog is still primed (no regression)" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2)
    put_instance_facts(ctx, "small", [{"wl-a", 0}], max: 10,
      size_class: "2gi", mem_budget_mib: 2048, mem_headroom_mib: 512)

    :ok = PoolManager.refill(ctx.pool)

    assert prime_counts(ctx) == %{"wl-a" => 2}
  end

  test "fleet-wide floor isolation holds with memory gate" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2, resources: %{"memMib" => 512})
    put_catalog(ctx, "wl-b", 2, resources: %{"memMib" => 2048})
    put_instance_facts(ctx, "small", [{"wl-a", 0}, {"wl-b", 0}], max: 4,
      size_class: "2gi", mem_budget_mib: 2048, mem_headroom_mib: 1024)
    # large must hold BOTH floors at once, not just wl-b's: the memory gate is
    # cumulative within a pass, so headroom has to cover wl-b's 2 x 2048 plus
    # whatever wl-a lands here. At exactly 4096 the second wl-b prime is squeezed
    # out by a single 512 MiB wl-a, which would test the arithmetic rather than the
    # floor-isolation property this case is about.
    put_instance_facts(ctx, "large", [{"wl-a", 0}, {"wl-b", 0}], max: 8,
      size_class: "16gi", mem_budget_mib: 16_384, mem_headroom_mib: 8192)
    set_depth(ctx, "wl-a", 500)

    :ok = PoolManager.refill(ctx.pool)

    counts = prime_counts(ctx)
    assert counts["wl-b"] == 2
    assert counts["wl-a"] >= 2
  end

  # -- in-flight memory (the shared Scheduler.Inflight ledger) ---------------

  # A prime_fun that reports each Prime and parks until released, so a prime
  # stays IN FLIGHT across refill ticks.
  defp blocking_prime(parent, result \\ {:ok, %PrimeResponse{vm_id: "vm-held"}}) do
    fn node, %PrimeRequest{trace: %Trace{workload: wl}} ->
      send(parent, {:prime, node, wl, self()})

      receive do
        :release -> result
      end
    end
  end

  test "an in-flight prime's memory is charged on the next refill" do
    # The #4121 shape across TICKS: one 1024 MiB prime fits a brick reporting
    # 1933 MiB, and the fact does not move while it restores. Without the ledger
    # the second tick would see 1933 again and place a second prime.
    parent = self()
    ctx = start_pool(channel_fun: fn node -> {:ok, node} end, prime_fun: blocking_prime(parent))
    put_catalog(ctx, "wl-a", 2, resources: %{"memMib" => 1024})
    put_instance_facts(ctx, "b", [{"wl-a", 0}], max: 10, size_class: "4gi", mem_budget_mib: 3_840, mem_headroom_mib: 1_933)

    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:prime, "b", "wl-a", worker}
    assert Inflight.pending_mib("b", table: ctx.inflight_table) == 1_024

    :ok = PoolManager.refill(ctx.pool)
    refute_receive {:prime, _, _, _}, 200
    assert PoolManager.stats(ctx.pool).inflight_primes == %{{"b", "wl-a"} => 1}

    # Success settles: the debit is HELD until the daemon's next report counts
    # the VM, so a refill in that window still places nothing new here.
    send(worker, :release)
    assert eventually(fn -> PoolManager.stats(ctx.pool).inflight_primes == %{} end)
    assert Inflight.pending_mib("b", table: ctx.inflight_table) == 1_024
    :ok = PoolManager.refill(ctx.pool)
    refute_receive {:prime, _, _, _}, 200
  end

  test "an in-flight prime spreads the next tick onto a sibling brick" do
    parent = self()
    ctx = start_pool(channel_fun: fn node -> {:ok, node} end, prime_fun: blocking_prime(parent))
    put_catalog(ctx, "wl-a", 2, resources: %{"memMib" => 1024})
    put_instance_facts(ctx, "a", [{"wl-a", 0}], max: 10, size_class: "4gi", mem_budget_mib: 3_840, mem_headroom_mib: 1_933)
    put_instance_facts(ctx, "b", [{"wl-a", 0}], max: 10, size_class: "4gi", mem_budget_mib: 3_840, mem_headroom_mib: 1_933)

    # One pass places one prime per brick (each fits exactly one).
    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:prime, first, "wl-a", w1}
    assert_receive {:prime, second, "wl-a", w2}
    assert Enum.sort([first, second]) == ["a", "b"]

    :ok = PoolManager.refill(ctx.pool)
    refute_receive {:prime, _, _, _}, 200
    send(w1, :release)
    send(w2, :release)
  end

  test "a failed prime releases its claim at once" do
    parent = self()
    refusal = {:error, %GRPC.RPCError{status: 8, message: "noded: pressure:mem"}}

    ctx =
      start_pool(channel_fun: fn node -> {:ok, node} end, prime_fun: blocking_prime(parent, refusal))

    put_catalog(ctx, "wl-a", 1, resources: %{"memMib" => 1024})
    put_instance_facts(ctx, "b", [{"wl-a", 0}], max: 10, size_class: "4gi", mem_budget_mib: 3_840, mem_headroom_mib: 1_933)

    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:prime, "b", "wl-a", worker}
    assert Inflight.pending_mib("b", table: ctx.inflight_table) == 1_024

    send(worker, :release)
    assert eventually(fn -> Inflight.pending_mib("b", table: ctx.inflight_table) == 0 end)
    assert eventually(fn -> PoolManager.stats(ctx.pool).inflight_primes == %{} end)

    # Placeable again on the next tick.
    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:prime, "b", "wl-a", again}
    send(again, :release)
  end

  test "a prime worker that dies releases its claim" do
    parent = self()

    ctx =
      start_pool(
        channel_fun: fn node -> {:ok, node} end,
        prime_fun: fn node, _req ->
          send(parent, {:prime, node, self()})

          receive do
            :die -> exit(:boom)
          end
        end
      )

    put_catalog(ctx, "wl-a", 1, resources: %{"memMib" => 1024})
    put_instance_facts(ctx, "b", [{"wl-a", 0}], max: 10, size_class: "4gi", mem_budget_mib: 3_840, mem_headroom_mib: 1_933)

    :ok = PoolManager.refill(ctx.pool)
    assert_receive {:prime, "b", worker}
    assert Inflight.pending_mib("b", table: ctx.inflight_table) == 1_024

    # The worker catches the prime_fun exit and reports it as a failed result;
    # either way (result or DOWN) the claim must not outlive the prime.
    send(worker, :die)
    assert eventually(fn -> Inflight.pending_mib("b", table: ctx.inflight_table) == 0 end)
  end

  test "another placer's claim on a brick lowers what the pool sees there" do
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 1, resources: %{"memMib" => 1024})
    put_instance_facts(ctx, "b", [{"wl-a", 0}], max: 10, size_class: "4gi", mem_budget_mib: 3_840, mem_headroom_mib: 3_000)

    # A session create committed 2048 MiB here that the brick has not reported.
    Inflight.claim("b", {:create, 1}, 2_048, table: ctx.inflight_table)

    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{}

    Inflight.release("b", {:create, 1}, table: ctx.inflight_table)
    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{"wl-a" => 1}
  end

  test "the in-pass gate stays cumulative on a reserved brick" do
    # Reserved: 7936 budget - 3072 declared = 4864 admittable, so ONE more 3072
    # MiB guest fits, however much observed headroom the brick reports.
    reserved = %{admits_on_reservation: true, mem_reserved_mib: 3_072}
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 3, resources: %{"memMib" => 3072})

    put_instance_facts(ctx, "r", [{"wl-a", 0}],
      max: 16,
      size_class: "8gi",
      mem_budget_mib: 7_936,
      mem_headroom_mib: 7_300,
      extra: reserved
    )

    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{"wl-a" => 1}

    # Control: the same brick in the observed model admits on headroom and
    # takes two (7300 -> 4228 -> 1156).
    ctx2 = start_pool()
    put_catalog(ctx2, "wl-a", 3, resources: %{"memMib" => 3072})

    put_instance_facts(ctx2, "o", [{"wl-a", 0}],
      max: 16,
      size_class: "8gi",
      mem_budget_mib: 7_936,
      mem_headroom_mib: 7_300,
      extra: %{reserved | admits_on_reservation: false}
    )

    :ok = PoolManager.refill(ctx2.pool)
    assert prime_counts(ctx2) == %{"wl-a" => 2}
  end

  test "the in-pass gate stays cumulative on a brick that reports its own admission" do
    reported = %{admission_reported: true, admits_on_reservation: true, mem_admittable_mib: 5_000, vm_overhead_mib: 100}
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 3, resources: %{"memMib" => 2048})

    put_instance_facts(ctx, "d", [{"wl-a", 0}],
      max: 16,
      size_class: "8gi",
      mem_budget_mib: 7_936,
      mem_headroom_mib: 7_900,
      extra: reported
    )

    # 5000 -> 2852 (2048 + 100 overhead) -> 704: two primes.
    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{"wl-a" => 2}
  end

  test "an observed brick's reported overhead is not charged in the pass" do
    # noded reports vm_overhead_mib under observed but admits on the need alone:
    # 4100 -> 2052 -> 4, two primes. Charging the 100 MiB overhead would stop
    # at one (4100 -> 1952).
    observed = %{admission_reported: true, admits_on_reservation: false, mem_admittable_mib: 4_100, vm_overhead_mib: 100}
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 3, resources: %{"memMib" => 2048})

    put_instance_facts(ctx, "d", [{"wl-a", 0}],
      max: 16,
      size_class: "8gi",
      mem_budget_mib: 7_936,
      mem_headroom_mib: 7_900,
      extra: observed
    )

    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{"wl-a" => 2}
  end

  test "a classed brick reporting no budget is blind and never primed onto" do
    # Deliberately fail-closed: this shape used to read as the unlimited wildcard.
    ctx = start_pool()
    put_catalog(ctx, "wl-a", 2, resources: %{"memMib" => 512})
    put_instance_facts(ctx, "blind", [{"wl-a", 0}], max: 10, size_class: "4gi", mem_budget_mib: 0, mem_headroom_mib: 0)

    :ok = PoolManager.refill(ctx.pool)
    assert prime_counts(ctx) == %{}
  end
end
