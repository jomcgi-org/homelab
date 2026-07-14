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
  alias Embervm.Node.V1.{PrimeRequest, PrimeResponse, Trace}

  defp start_pool(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"pcap_#{suffix}"
    cat_table = :"pcat_#{suffix}"
    depth_table = :"pdepth_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)
    :ets.new(depth_table, [:set, :public, :named_table])

    {:ok, primes} = Agent.start_link(fn -> [] end)
    {:ok, status} = Agent.start_link(fn -> [] end)

    prime_fun = fn _ch, %PrimeRequest{trace: %Trace{workload: wl}} ->
      Agent.update(primes, &[wl | &1])
      {:ok, %PrimeResponse{vm_id: "vm-#{System.unique_integer([:positive])}"}}
    end

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
          clock: fn -> 1_000_000 end,
          channel_fun: fn _ -> {:ok, :ch} end,
          prime_fun: prime_fun,
          deposit_fun: fn _srv, _node, _wl, _vm -> :ok end,
          status_writer: status_writer,
          max_concurrent_primes: Keyword.get(opts, :max_concurrent_primes, 100),
          start_refill: false
        ]
      )

    %{pool: pool, cap_table: cap_table, cat_table: cat_table, depth_table: depth_table, primes: primes, status: status}
  end

  defp put_catalog(ctx, wl, floor) do
    WorkloadCatalog.upsert(ctx.cat_table, wl, %{name: wl, namespace: "embervm", floor: floor, cap: 100, invoke_path: "/"})
  end

  defp put_facts(ctx, workloads, opts) do
    wl_map =
      for {wl, free} <- workloads, into: %{} do
        {wl, %{free_primed_slots: free, snapshot_ref: "snap-#{wl}", base_state: :BASE_BUILD_STATE_READY}}
      end

    NodeCapacity.put(ctx.cap_table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      workloads: wl_map,
      mem_headroom_mib: 8192,
      cpu_headroom_millicores: 8000,
      live_vms: Keyword.get(opts, :live, 0),
      max_live_vms: Keyword.fetch!(opts, :max),
      draining: false,
      updated_at: 1_000_000
    })
  end

  defp set_depth(ctx, wl, n), do: :ets.insert(ctx.depth_table, {{wl, "p"}, n})

  defp prime_counts(ctx) do
    Process.sleep(50)
    ctx.primes |> Agent.get(& &1) |> Enum.frequencies()
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
end
