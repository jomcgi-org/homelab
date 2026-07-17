defmodule Embervm.StatefulSweeperTest do
  @moduledoc """
  Exercises Embervm.StatefulSweeper (the R4 Task 9 lifecycle-economics loop)
  against a real StatefulStore + op-log, an injected stats-scrape seam
  (scripted downstream_cx_active/total readings), injected StopStateful/
  EvictSnapshot seams, and an injected clock so no test sleeps. Covers:

    * idle detection from a scripted stats sequence (cx_active must be zero AND
      the cx_total delta must be zero across idleBankSeconds to bank; a
      never-zero cx_active NEVER banks; a nonzero cx_total delta resets the
      idle clock);
    * the decision-7 recheck: a connection racing in between the idle decision
      and the bank aborts it (no stateful_banked op, republished);
    * max-lifetime expiry (an idle over-lifetime instance destroys
      immediately; an active one waits out the drain patience window then
      destroys anyway);
    * banked-TTL GC (evicts the bundle, reason "ttl"; the volume row survives);
    * eager broken-pair eviction on the sweep cadence.

  Every clock/timer is injected; no test sleeps.
  """
  use ExUnit.Case, async: false

  alias Embervm.{NodeCapacity, StatefulStore, StatefulSweeper, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.{EvictSnapshotResponse, StopStatefulResponse}

  # A publisher that RECORDS each publish/1 cast, mirroring ServingSweeperTest's
  # FakePublisher.
  defmodule FakePublisher do
    use GenServer
    def start_link, do: GenServer.start_link(__MODULE__, 0)
    def count(pid), do: GenServer.call(pid, :count)
    @impl true
    def init(n), do: {:ok, n}
    @impl true
    def handle_cast(:publish, n), do: {:noreply, n + 1}
    @impl true
    def handle_call(:count, _from, n), do: {:reply, n, n}
  end

  defp clock(agent), do: fn -> Agent.get(agent, & &1) end
  defp advance(agent, ms), do: Agent.update(agent, &(&1 + ms))

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"sfswcap_#{suffix}"
    cat_table = :"sfswcat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_statefulsweeper_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, clock_agent} = Agent.start_link(fn -> 10_000 end)
    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = StatefulStore.start_link(name: nil, op_log: op_log, clock: clock(clock_agent))
    {:ok, pub} = FakePublisher.start_link()

    # The scrape seam: an Agent holds the CURRENT reading the next scrape
    # returns. A reading is %{stat_prefix => %{active: n, total: n}} or
    # {:error, reason}.
    {:ok, scrape_agent} = Agent.start_link(fn -> {:ok, %{}} end)

    {:ok, stop_calls} = Agent.start_link(fn -> [] end)
    {:ok, bank_fail} = Agent.start_link(fn -> false end)
    {:ok, evict_calls} = Agent.start_link(fn -> [] end)

    stop_stateful_fun = fn _ch, req ->
      Agent.update(stop_calls, &[req | &1])

      cond do
        req.mode == :STOP_STATEFUL_MODE_BANK and Agent.get(bank_fail, & &1) ->
          {:error, :bank_boom}

        req.mode == :STOP_STATEFUL_MODE_BANK ->
          {:ok, %StopStatefulResponse{snapshot_ref: "snap-#{req.vm_id}", size_bytes: 4_096, generation: 1}}

        true ->
          {:ok, %StopStatefulResponse{}}
      end
    end

    evict_snapshot_fun = fn _ch, req ->
      Agent.update(evict_calls, &[req | &1])
      {:ok, %EvictSnapshotResponse{}}
    end

    sweeper_opts =
      [
        name: nil,
        store: store,
        publisher: pub,
        capacity_table: cap_table,
        catalog_table: cat_table,
        op_log: op_log,
        clock: clock(clock_agent),
        scrape_fun: fn _url -> Agent.get(scrape_agent, & &1) end,
        stats_base: Keyword.get(opts, :stats_base, "http://serving:9902"),
        channel_fun: fn _node -> {:ok, :ch} end,
        stop_stateful_fun: stop_stateful_fun,
        evict_snapshot_fun: evict_snapshot_fun,
        bank_concurrency: Keyword.get(opts, :bank_concurrency, 1),
        lifetime_drain_max_ms: Keyword.get(opts, :lifetime_drain_max_ms, 3_600_000),
        sweep_interval_ms: 0
      ]

    {:ok, sweeper} = StatefulSweeper.start_link(sweeper_opts)

    %{
      sweeper: sweeper,
      store: store,
      op_log: op_log,
      pub: pub,
      cap_table: cap_table,
      cat_table: cat_table,
      clock_agent: clock_agent,
      scrape_agent: scrape_agent,
      stop_calls: stop_calls,
      bank_fail: bank_fail,
      evict_calls: evict_calls
    }
  end

  defp set_scrape(ctx, reading), do: Agent.update(ctx.scrape_agent, fn _ -> reading end)
  defp stop_calls(ctx), do: Agent.get(ctx.stop_calls, &Enum.reverse(&1))
  defp evict_calls(ctx), do: Agent.get(ctx.evict_calls, &Enum.reverse(&1))

  # Flush the sweeper's mailbox: a plain :sys call is processed AFTER every
  # message already queued, so anything sent (e.g. a spawned bank worker's
  # {:bank_done}) before this returns has been handled. StopStateful(BANK) runs
  # in a spawned worker (mirroring ServingSweeper), so a test asserting the
  # OUTCOME of a bank attempt must wait for it rather than reading the store
  # immediately after sweep/1 returns.
  defp flush(ctx), do: :sys.get_state(ctx.sweeper)

  defp wait_until(ctx, fun, tries \\ 50) do
    flush(ctx)

    cond do
      fun.() -> :ok
      tries <= 0 -> flunk("wait_until: condition never held")
      true -> wait_until(ctx, fun, tries - 1)
    end
  end

  defp stateful_workload(ctx, name, listen_port, cfg \\ %{}) do
    base = %{
      port: 5432,
      listen_port: listen_port,
      volume_size_gib: 1,
      volume_mount_path: "/data",
      idle_bank_seconds: 60,
      max_lifetime_seconds: 86_400,
      banked_ttl_seconds: 3_600,
      wake_timeout_seconds: 60
    }

    WorkloadCatalog.upsert(ctx.cat_table, name, %{
      class: "stateful",
      namespace: "embervm",
      stateful: Map.merge(base, cfg)
    })
  end

  defp stateful_node(ctx, node_id) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.98.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      stateful_vms: [],
      stateful_bundles: [],
      volumes: []
    })
  end

  # Create a serving (published) instance directly.
  defp serving_instance(ctx, id, workload, vm_id, ip, port \\ 5432) do
    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "system:stateful:#{workload}",
        workload: workload,
        node_id: "node-4",
        vm_id: vm_id,
        generation: 0
      })

    {:ok, _} = StatefulStore.publish(ctx.store, id, ip, port, :started)
    id
  end

  # Drive an instance from serving to banked directly (bypassing the sweeper),
  # for tests that need a pre-existing banked bundle.
  defp banked_instance(ctx, id, workload, vm_id, snapshot_generation) do
    _ = serving_instance(ctx, id, workload, vm_id, "10.98.0.5")
    {:ok, _} = StatefulStore.unpublish(ctx.store, id, :bank)

    {:ok, banked} =
      StatefulStore.transition(
        ctx.store,
        id,
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: "snap-#{id}", size_bytes: 2_000, generation: snapshot_generation},
        %{snapshot_ref: "snap-#{id}", snapshot_size_bytes: 2_000, snapshot_generation: snapshot_generation, vm_id: nil}
      )

    banked
  end

  defp reading(prefix, active, total), do: {:ok, %{prefix => %{active: active, total: total}}}

  # -- idle detection: cx_active must be zero AND cx_total delta zero ---------

  test "cx_active==0 and a flat cx_total delta across idleBankSeconds banks the instance" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    # Tick 1: baseline (no prior reading, banks nothing).
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, still_serving} = StatefulStore.get(ctx.store, "sf-1")
    assert still_serving.state == :serving

    # Tick 2: idle confirmed this tick (delta 0 against tick 1), so idle_since
    # is set to THIS tick's clock. The idle-bank decision measures the window
    # from idle_since, so a bank cannot fire on the same tick idle_since is
    # first established; the instance is still serving after this tick.
    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, still_serving2} = StatefulStore.get(ctx.store, "sf-1")
    assert still_serving2.state == :serving

    # Tick 3 (65s after idle_since was set, past idleBankSeconds=60): cx_active
    # still 0, cx_total unchanged (delta 0, re-confirming idle without
    # resetting idle_since). The instance banks (StopStateful BANK ran; the row
    # is now banked, since the sweep is synchronous within one sweep/1 call).
    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # StopStateful(BANK) runs in a spawned worker; wait for {:bank_done}.
    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, banked} = StatefulStore.get(ctx.store, "sf-1")
    assert banked.state == :banked
    assert banked.snapshot_ref == "snap-vm-1"
    assert banked.snapshot_generation == 1

    assert [%{mode: :STOP_STATEFUL_MODE_BANK, vm_id: "vm-1"}] = stop_calls(ctx)

    # Op-log ordering: stateful_banked landed (stateful_unpublished is NOT
    # appended per the store's ETS-only unpublish decision).
    ops = load_ops(ctx, "stateful_banked")
    assert length(ops) == 1
  end

  test "cx_active never zero: the instance NEVER banks even well past idleBankSeconds" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    set_scrape(ctx, reading("state-5400", 1, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # Well past 2x idleBankSeconds, but cx_active stays 1 (an open connection)
    # every tick: never bankable per decision 7.
    advance(ctx.clock_agent, 130_000)
    set_scrape(ctx, reading("state-5400", 1, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 130_000)
    set_scrape(ctx, reading("state-5400", 1, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving
    assert stop_calls(ctx) == []
  end

  test "a nonzero cx_total delta resets the idle clock" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    # Baseline.
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # 50s later: still under idleBankSeconds, but a NEW connection arrived
    # (total 3 -> 5). This resets the idle clock (touches active).
    advance(ctx.clock_agent, 50_000)
    set_scrape(ctx, reading("state-5400", 0, 5))
    StatefulSweeper.sweep(ctx.sweeper)

    # The next tick with a flat delta (still total=5) is when idle_since is
    # FIRST set (idleness is confirmed against the tick-2 reading); the
    # idle-bank decision measures the window from THIS tick, so it cannot bank
    # on the same tick idle_since is established.
    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 5))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, not_yet} = StatefulStore.get(ctx.store, "sf-1")
    assert not_yet.state == :serving

    # 55s after idle_since was set: still short of idleBankSeconds=60 measured
    # from the reset point (proving it needed the full window from the RESET,
    # not from the original baseline, which is already well past 60s ago).
    advance(ctx.clock_agent, 55_000)
    set_scrape(ctx, reading("state-5400", 0, 5))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, still_not_yet} = StatefulStore.get(ctx.store, "sf-1")
    assert still_not_yet.state == :serving

    # ...then bank once the FULL window elapses from the reset.
    advance(ctx.clock_agent, 10_000)
    set_scrape(ctx, reading("state-5400", 0, 5))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, banked} = StatefulStore.get(ctx.store, "sf-1")
    assert banked.state == :banked

    stats_ops = load_ops(ctx, "stateful_stats")
    assert Enum.any?(stats_ops, &(&1.payload["cx_delta"] == 2))
  end

  test "a scrape failure fails open: no idle-bank decision runs against stale stats" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 300_000)
    set_scrape(ctx, {:error, :timeout})
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving, "a failed scrape must not bank a warm instance"
  end

  # -- decision-7 recheck: abort-on-race ---------------------------------------

  test "a connection racing in between the idle decision and the bank ABORTS it (no stateful_banked, republished)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # Tick 2: confirm idle (idle_since gets set this tick, so the window has
    # NOT yet elapsed).
    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, still_serving} = StatefulStore.get(ctx.store, "sf-1")
    assert still_serving.state == :serving

    # Tick 3 (65s after idle_since was set, past idleBankSeconds=60): the
    # scrape seam is scripted to answer the TICK-START scan with cx_active==0
    # (idle re-confirmed, the bank begins) but answer the RECHECK's fresh
    # re-scrape (a SECOND call within the same sweep, after unpublish) with
    # cx_active==1: a connection opened in the gap decision 7 exists to catch.
    # An Agent-backed call counter distinguishes the two scrape calls
    # deterministically (no timing dependency).
    {:ok, call_count} = Agent.start_link(fn -> 0 end)

    scrape_fun = fn _url ->
      n = Agent.get_and_update(call_count, fn n -> {n, n + 1} end)

      if n == 0 do
        reading("state-5400", 0, 3)
      else
        reading("state-5400", 1, 3)
      end
    end

    :sys.replace_state(ctx.sweeper, fn s -> %{s | scrape_fun: scrape_fun} end)

    advance(ctx.clock_agent, 65_000)
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving
    assert instance.ip == "10.98.0.5"
    assert instance.port == 5432

    # No bank RPC was ever issued, and no stateful_banked op landed.
    assert stop_calls(ctx) == []
    assert load_ops(ctx, "stateful_banked") == []
  end

  # -- max-lifetime expiry ------------------------------------------------------

  test "an idle over-lifetime instance destroys immediately" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{max_lifetime_seconds: 100})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :destroyed
    assert instance.terminal_reason == "lifetime"
    assert [%{mode: :STOP_STATEFUL_MODE_DESTROY, vm_id: "vm-1"}] = stop_calls(ctx)
  end

  test "an active over-lifetime instance waits the drain patience window, then destroys anyway" do
    ctx = start_stack(lifetime_drain_max_ms: 100_000)
    stateful_workload(ctx, "wl-a", 5400, %{max_lifetime_seconds: 100})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    # Over lifetime AND actively connected: must NOT destroy yet.
    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("state-5400", 1, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, still_alive} = StatefulStore.get(ctx.store, "sf-1")
    assert still_alive.state == :serving

    # Still active, short of the patience window (100s): still alive.
    advance(ctx.clock_agent, 50_000)
    set_scrape(ctx, reading("state-5400", 1, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, still_alive2} = StatefulStore.get(ctx.store, "sf-1")
    assert still_alive2.state == :serving

    # Past the patience window (measured from when it was FIRST seen
    # over-lifetime-and-active): destroyed anyway, even though still active.
    advance(ctx.clock_agent, 60_000)
    set_scrape(ctx, reading("state-5400", 1, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, destroyed} = StatefulStore.get(ctx.store, "sf-1")
    assert destroyed.state == :destroyed
    assert destroyed.terminal_reason == "lifetime"
  end

  test "a banked over-lifetime bundle is evicted immediately (no VM to drain)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{max_lifetime_seconds: 100})
    stateful_node(ctx, "node-4")
    banked_instance(ctx, "sf-1", "wl-a", "vm-1", 1)

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :evicted
    assert instance.terminal_reason == "lifetime"
  end

  # -- banked-TTL GC ------------------------------------------------------------

  test "a banked bundle untouched past bankedTtlSeconds is evicted (reason ttl); the volume survives" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{banked_ttl_seconds: 100, max_lifetime_seconds: 1_000_000})
    stateful_node(ctx, "node-4")

    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 1,
        size_bytes: 1_073_741_824,
        allocated_bytes: 100
      })

    banked_instance(ctx, "sf-1", "wl-a", "vm-1", 1)

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :evicted
    assert instance.terminal_reason == "ttl"

    # The EvictSnapshot RPC ran, reclaiming the bundle's disk.
    assert [%{snapshot_ref: "snap-sf-1"}] = evict_calls(ctx)

    # The VOLUME is untouched: still present with its generation intact.
    volume = StatefulStore.get_volume(ctx.store, "wl-a")
    assert volume != nil
    assert volume.generation == 1
  end

  test "a banked bundle still within bankedTtlSeconds is kept" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{banked_ttl_seconds: 1_000_000, max_lifetime_seconds: 1_000_000})
    stateful_node(ctx, "node-4")

    # A matching volume (generation 1 == the bundle's stamped generation) so the
    # pair is VALID and the per-tick eager-evict does not remove the bundle; only
    # the TTL/lifetime sweeps could, and both are far in the future here.
    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 1,
        size_bytes: 1_073_741_824,
        allocated_bytes: 100
      })

    banked_instance(ctx, "sf-1", "wl-a", "vm-1", 1)

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :banked
  end

  # -- eager broken-pair eviction -----------------------------------------------

  test "a banked instance whose volume generation moved is evicted on the sweep (broken pair)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{banked_ttl_seconds: 1_000_000, max_lifetime_seconds: 1_000_000})
    stateful_node(ctx, "node-4")

    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 1,
        size_bytes: 1_073_741_824,
        allocated_bytes: 100
      })

    # Banked with snapshot_generation=1, matching the volume: pair valid so far.
    banked_instance(ctx, "sf-1", "wl-a", "vm-1", 1)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    # The volume's generation moves (e.g. a concurrent cold-boot attach bumped
    # it), breaking the pair.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{generation: 2})
    refute StatefulStore.pair_valid?(ctx.store, "wl-a")

    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :evicted
    assert instance.terminal_reason == "pair_broken"

    # The volume itself survives, generation as bumped.
    volume = StatefulStore.get_volume(ctx.store, "wl-a")
    assert volume.generation == 2
  end

  # -- per-node concurrent-bank cap ---------------------------------------------

  test "the per-node concurrent-bank cap defers a second idle instance's bank to a later tick" do
    ctx = start_stack(bank_concurrency: 1)
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_workload(ctx, "wl-b", 5401, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5", 5432)
    serving_instance(ctx, "sf-2", "wl-b", "vm-2", "10.98.0.6", 5432)

    scrape = {:ok, %{"state-5400" => %{active: 0, total: 0}, "state-5401" => %{active: 0, total: 0}}}

    set_scrape(ctx, scrape)
    StatefulSweeper.sweep(ctx.sweeper)

    # Idle confirmed this tick for both workloads (idle_since set); cannot bank
    # on the same tick idle_since is established.
    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, scrape)
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, scrape)
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn ->
      [StatefulStore.get(ctx.store, "sf-1"), StatefulStore.get(ctx.store, "sf-2")]
      |> Enum.map(fn {:ok, i} -> i.state end)
      |> Enum.any?(&(&1 == :banked))
    end)

    states =
      [StatefulStore.get(ctx.store, "sf-1"), StatefulStore.get(ctx.store, "sf-2")]
      |> Enum.map(fn {:ok, i} -> i.state end)
      |> Enum.sort()

    # Both instances live on the SAME node (node-4), so the cap=1 serializes:
    # exactly one banked this tick, the other stays serving (aborted back after
    # losing the cap race).
    assert Enum.count(states, &(&1 == :banked)) == 1
    assert Enum.count(states, &(&1 == :serving)) == 1

    bank_calls = Enum.filter(stop_calls(ctx), &(&1.mode == :STOP_STATEFUL_MODE_BANK))
    assert length(bank_calls) == 1, "cap=1 must serialize banks"
  end

  test "a failed bank returns the instance to the fan-out (abort, endpoint restored)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    Agent.update(ctx.bank_fail, fn _ -> true end)

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # Idle confirmed this tick (idle_since set); a bank cannot fire on the same
    # tick idle_since is established (see the off-by-one note in the idle
    # detection tests above).
    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, still_serving} = StatefulStore.get(ctx.store, "sf-1")
    assert still_serving.state == :serving

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # The bank RPC failed (bank_fail): the worker reports {:bank_done, ...,
    # {:error, ...}}, which finish_bank_active resolves by aborting back to
    # `serving` and restoring the endpoint. Wait for that async completion.
    wait_until(ctx, fn -> match?({:ok, %{state: :serving}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving
    assert instance.ip == "10.98.0.5"
    assert instance.port == 5432

    assert [%{mode: :STOP_STATEFUL_MODE_BANK}] = stop_calls(ctx)
    assert load_ops(ctx, "stateful_banked") == []
  end

  # -- raw stats-body parse path -------------------------------------------------

  test "parse_stats extracts only tcp.<prefix>.downstream_cx_{active,total} from a realistic Envoy body" do
    body =
      Jason.encode!(%{
        "stats" => [
          %{"name" => "tcp.state-5400.downstream_cx_active", "value" => 2},
          %{"name" => "tcp.state-5400.downstream_cx_total", "value" => 42},
          %{"name" => "tcp.state-5401.downstream_cx_active", "value" => 0},
          %{"name" => "tcp.state-5401.downstream_cx_total", "value" => 7},
          # Noise that must be ignored: cluster gauges and non-cx_active/total
          # tcp gauges.
          %{"name" => "cluster.serve|wl-c.upstream_rq_total", "value" => 999},
          %{"name" => "tcp.state-5400.downstream_cx_rx_bytes_total", "value" => 12_345},
          %{"name" => "server.uptime", "value" => 1_234}
        ]
      })

    assert {:ok, reading} = StatefulSweeper.parse_stats(body)

    assert reading == %{
             "state-5400" => %{active: 2, total: 42},
             "state-5401" => %{active: 0, total: 7}
           }
  end

  test "parse_stats rejects an unparseable or non-stats body (fail-open upstream)" do
    assert {:error, _} = StatefulSweeper.parse_stats("not json")
    assert {:error, :unparseable_stats} = StatefulSweeper.parse_stats(Jason.encode!(%{"nope" => 1}))
  end

  # -- helpers --------------------------------------------------------------

  defp load_ops(ctx, kind) do
    atom = String.to_existing_atom(kind)
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    Enum.filter(ops, &(&1.kind == atom))
  end
end
