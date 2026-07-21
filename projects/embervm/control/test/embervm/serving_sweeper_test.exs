defmodule Embervm.ServingSweeperTest do
  @moduledoc """
  Exercises Embervm.ServingSweeper (the R3 Task 9 lifecycle-economics loop) against a
  real ServingStore + op-log, an injected stats-scrape seam (scripted rq_total
  readings), an injected StopServing seam, and an injected timer seam so the
  drainSeconds wait is fired deterministically WITHOUT a real sleep. Covers:

    * idle detection from a scripted stats sequence (a non-zero delta keeps an
      instance warm; zero delta across idleBankSeconds marks it idle);
    * the minInstances floor is respected (an idle workload never banks below it);
    * drain-before-bank ordering (serving_unpublished precedes serving_banked in the
      op-log, and the activator is installed atomically with the LAST unpublish, so
      scale-to-zero is never a 503 window);
    * the drain-for-bank drain_reason stamp (:bank) so a health sweep cannot republish;
    * max-lifetime expiry (drain + destroy), banked-TTL GC, and wake-time TTL;
    * the forced-roll DELETE path (drain + destroy live, evict banked);
    * the per-node concurrent-bank cap.

  Every clock/timer is injected; no test sleeps.
  """
  # async: false: several tests poll for a state transition driven by a spawned
  # bank worker (fire_drain -> {:bank_drained} -> StopServing worker -> {:bank_done}
  # -> abort/republish), which is contention-sensitive under a large async suite.
  # Serial execution removes the contention and keeps the wait deterministic.
  use ExUnit.Case, async: false

  alias Embervm.{NodeCapacity, ServingStore, ServingSweeper, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.StopServingResponse

  # A publisher that RECORDS each publish/1 cast so a test can assert the fan-out was
  # re-derived (and, combined with a store read, that the activator swap happened
  # atomically with the last unpublish).
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

  # A mutable clock an Agent backs, so a test advances wall-time between sweeps
  # deterministically.
  defp clock(agent), do: fn -> Agent.get(agent, & &1) end
  defp advance(agent, ms), do: Agent.update(agent, &(&1 + ms))

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"swcap_#{suffix}"
    cat_table = :"swcat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_servingsweeper_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, clock_agent} = Agent.start_link(fn -> 10_000 end)
    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = ServingStore.start_link(name: nil, op_log: op_log, clock: clock(clock_agent))
    {:ok, pub} = FakePublisher.start_link()

    # The scrape seam: an Agent holds the CURRENT reading the next scrape returns, so
    # a test scripts a stats sequence by updating it between sweeps. A reading is
    # %{cluster_name => rq_total} or {:error, reason} to simulate a scrape failure.
    {:ok, scrape_agent} = Agent.start_link(fn -> {:ok, %{}} end)

    # The StopServing seam: records each call and returns a bank snapshot (or a
    # scripted failure via the fail agent).
    {:ok, stop_calls} = Agent.start_link(fn -> [] end)
    {:ok, bank_fail} = Agent.start_link(fn -> false end)

    stop_serving_fun = fn _ch, req ->
      Agent.update(stop_calls, &[req | &1])

      cond do
        req.mode == :STOP_SERVING_MODE_BANK and Agent.get(bank_fail, & &1) ->
          {:error, :bank_boom}

        req.mode == :STOP_SERVING_MODE_BANK ->
          {:ok, %StopServingResponse{snapshot_ref: "snap-#{req.vm_id}", size_bytes: 4_096}}

        true ->
          {:ok, %StopServingResponse{}}
      end
    end

    # The timer seam: records {msg, delay} and, unless a test wants to hold the timer,
    # does NOT auto-fire (the test sends {:bank_drained, id} itself to control
    # ordering). Held timers let a test assert the drain window shape.
    {:ok, timers} = Agent.start_link(fn -> [] end)
    timer_fun = fn msg, delay -> Agent.update(timers, &[{msg, delay} | &1]) end

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
        channel_fun: Keyword.get(opts, :channel_fun, fn _node -> {:ok, :ch} end),
        stop_serving_fun: stop_serving_fun,
        timer_fun: timer_fun,
        bank_concurrency: Keyword.get(opts, :bank_concurrency, 1),
        sweep_interval_ms: 0
      ]

    # The status.serving writer seam (Task 10): records every {namespace, name,
    # status_map} the sweep patches, so a test asserts the debounce (one write per
    # changed workload per sweep, none when unchanged).
    {:ok, status_calls} = Agent.start_link(fn -> [] end)

    status_writer = fn namespace, name, status_map ->
      Agent.update(status_calls, &[{namespace, name, status_map} | &1])
      :ok
    end

    sweeper_opts = Keyword.put(sweeper_opts, :status_writer, status_writer)

    {:ok, sweeper} = ServingSweeper.start_link(sweeper_opts)

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
      timers: timers,
      status_calls: status_calls
    }
  end

  defp set_scrape(ctx, reading), do: Agent.update(ctx.scrape_agent, fn _ -> reading end)
  defp stop_calls(ctx), do: Agent.get(ctx.stop_calls, &Enum.reverse(&1))
  defp timers(ctx), do: Agent.get(ctx.timers, &Enum.reverse(&1))

  defp serving_workload(ctx, name, cfg \\ %{}) do
    base = %{
      host: "#{name}.example",
      port: 8080,
      health_path: "/healthz",
      min_instances: 0,
      max_instances: 4,
      idle_bank_seconds: 60,
      drain_seconds: 5,
      max_lifetime_seconds: 86_400,
      banked_ttl_seconds: 3_600
    }

    WorkloadCatalog.upsert(ctx.cat_table, name, %{
      class: "serving",
      namespace: "embervm",
      serving: Map.merge(base, cfg)
    })
  end

  defp status_writes(ctx), do: Agent.get(ctx.status_calls, &Enum.reverse(&1))

  defp serving_node(ctx, node_id) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.99.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      serving_vms: [],
      serving_snapshots: []
    })
  end

  # Create a published instance directly (the activator's cold-create + publish),
  # returning its id.
  defp published_instance(ctx, id, workload, vm_id, ip) do
    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "system:serving:#{workload}",
        workload: workload,
        node_id: "node-4",
        vm_id: vm_id,
        ip: ip,
        port: 8080
      })

    {:ok, _} = ServingStore.publish(ctx.store, id, ip, 8080, :started)
    id
  end

  # Drive the drain timer for an instance by hand (the seam records but does not fire).
  defp fire_drain(ctx, id), do: send(ctx.sweeper, {:bank_drained, id})

  # Flush the sweeper's mailbox: a plain GenServer call is processed AFTER every
  # message already queued, so anything sent before this returns has been handled.
  # Use :sys.get_state rather than sweep/1 so flushing does not re-run a full sweep
  # (which would re-scrape and could re-arm decisions mid-assertion).
  defp flush(ctx), do: :sys.get_state(ctx.sweeper)

  # Poll the store until `fun` returns a truthy value or a bounded number of flushes
  # elapse. The bank worker reports {:bank_done} asynchronously, so a test that fired
  # a drain waits here for the durable transition to land. No real sleep: each
  # iteration just flushes the mailbox (processing any pending {:bank_done}).
  defp wait_until(ctx, fun, tries \\ 50) do
    flush(ctx)

    cond do
      fun.() -> :ok
      tries <= 0 -> flunk("wait_until: condition never held")
      true -> wait_until(ctx, fun, tries - 1)
    end
  end

  # -- idle detection from a scripted stats sequence -------------------------

  test "a non-zero rq delta keeps an instance warm; zero delta across the window banks it" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{min_instances: 0, idle_bank_seconds: 60, drain_seconds: 5})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    # Tick 1: establish the baseline reading (10 requests so far). No prior reading =>
    # nothing active, nothing banked (baseline only).
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    # Tick 2 (30s later): the counter rose (traffic) => the instance is marked active,
    # a serving_stats op is appended, and it is NOT banked.
    advance(ctx.clock_agent, 30_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 25}})
    ServingSweeper.sweep(ctx.sweeper)

    {:ok, warm} = ServingStore.get(ctx.store, "srv-1")
    assert warm.state == :published
    assert warm.last_active_at != nil

    # A serving_stats op recorded the request-count delta (25 - 10 = 15).
    stats_ops = load_ops(ctx, "serving_stats")
    assert Enum.any?(stats_ops, &(&1.payload["rq_delta"] == 15))

    # Ticks 3+ (past idleBankSeconds with NO further traffic): the counter is flat, so
    # the delta is zero and the instance idles. Advance past the idle window measured
    # from the last-active tick.
    advance(ctx.clock_agent, 70_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 25}})
    ServingSweeper.sweep(ctx.sweeper)

    # It is now draining for a bank (unpublished, drain timer armed), NOT yet banked.
    {:ok, draining} = ServingStore.get(ctx.store, "srv-1")
    assert draining.state == :draining
    assert draining.drain_reason == :bank
    assert Enum.any?(timers(ctx), fn {msg, delay} -> msg == {:bank_drained, "srv-1"} and delay == 5_000 end)
  end

  test "a scrape failure fails open: no idle-bank decision runs against stale stats" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{idle_bank_seconds: 60})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    # Baseline, then a long idle gap, but the scrape FAILS: the instance must NOT be
    # banked (never bank on stale/missing stats).
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 300_000)
    set_scrape(ctx, {:error, :timeout})
    ServingSweeper.sweep(ctx.sweeper)

    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :published, "a failed scrape must not bank a warm instance"
  end

  test "the first tick establishes a baseline and banks nothing (no prior reading)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{idle_bank_seconds: 1})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    # Even with idle_bank_seconds=1 and an old created_at, the FIRST successful scrape
    # only baselines: no delta is computable, so nothing banks this tick.
    advance(ctx.clock_agent, 10_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 5}})
    ServingSweeper.sweep(ctx.sweeper)

    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :published
  end

  # -- minInstances floor ----------------------------------------------------

  test "the minInstances floor is respected: an idle workload never banks below it" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{min_instances: 1, idle_bank_seconds: 60})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")
    published_instance(ctx, "srv-2", "wl-a", "vm-2", "10.99.0.6")

    # Baseline then a long idle gap: both instances are idle, but minInstances=1 means
    # exactly ONE may bank; the other stays live.
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 120_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    states = ServingStore.list(ctx.store, "wl-a") |> Enum.map(& &1.state) |> Enum.sort()
    # One draining-for-bank, one still published (the floor).
    assert states == [:draining, :published]
  end

  test "minInstances 0 allows scale-to-zero (the sole instance banks)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{min_instances: 0, idle_bank_seconds: 60})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)
    advance(ctx.clock_agent, 120_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :draining
  end

  # -- drain-before-bank ordering + atomic activator -------------------------

  test "drain ordering: serving_unpublished precedes serving_banked in the op-log" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{min_instances: 0, idle_bank_seconds: 60, drain_seconds: 5})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    # Idle it to the drain step.
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)
    advance(ctx.clock_agent, 120_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    # Unpublished, no bank yet: the endpoint is already out of the fan-out.
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []
    assert stop_calls(ctx) == []

    # Fire the drain deadline: the bank runs (StopServing BANK), then serving_banked.
    fire_drain(ctx, "srv-1")
    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, ServingStore.get(ctx.store, "srv-1")) end)

    {:ok, banked} = ServingStore.get(ctx.store, "srv-1")
    assert banked.state == :banked
    assert banked.snapshot_ref == "snap-vm-1"

    # The StopServing was a BANK.
    assert [%{mode: :STOP_SERVING_MODE_BANK, vm_id: "vm-1"}] = stop_calls(ctx)

    # Op-log ordering: the serving_unpublished op strictly precedes serving_banked.
    seqs = op_seqs(ctx, ["serving_unpublished", "serving_banked"])
    assert seqs["serving_unpublished"] < seqs["serving_banked"]
  end

  test "scale-to-zero installs the activator atomically with the last unpublish (publisher asked before the bank)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{min_instances: 0, idle_bank_seconds: 60})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)
    before = FakePublisher.count(ctx.pub)

    advance(ctx.clock_agent, 120_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    # The unpublish (which empties the cluster) asked the publisher to re-derive, so
    # the activator fallback is installed in the SAME EDS update that removed the
    # endpoint -- BEFORE the bank even runs. No 503 window.
    assert FakePublisher.count(ctx.pub) == before + 1
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []
  end

  # -- max-lifetime + TTL GC -------------------------------------------------

  test "max-lifetime expiry drains and destroys a live instance" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{max_lifetime_seconds: 100})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    # Past the lifetime (created at clock=10_000, lifetime 100s): destroy.
    advance(ctx.clock_agent, 200_000)
    # A benign scrape so the tick runs its lifetime pass.
    set_scrape(ctx, {:ok, %{}})
    ServingSweeper.sweep(ctx.sweeper)

    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :destroyed
    assert instance.terminal_reason == "lifetime"
    # The VM was torn down with DESTROY (no snapshot).
    assert [%{mode: :STOP_SERVING_MODE_DESTROY, vm_id: "vm-1"}] = stop_calls(ctx)
  end

  test "banked-TTL GC evicts a banked snapshot untouched past bankedTtlSeconds (also the wake-time TTL guard)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{banked_ttl_seconds: 100, max_lifetime_seconds: 1_000_000})
    serving_node(ctx, "node-4")
    id = published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    # Bank it (drain + fire + finish).
    {:ok, _} = ServingStore.unpublish(ctx.store, id, :bank)
    {:ok, _} = ServingStore.mark(ctx.store, id, :bank)

    {:ok, _} =
      ServingStore.transition(ctx.store, id, :bank_ready, :serving_banked,
        %{snapshot_ref: "snap-1", size_bytes: 10, generation: 1},
        %{snapshot_ref: "snap-1", snapshot_size_bytes: 10, generation: 1, vm_id: nil}
      )

    {:ok, banked} = ServingStore.get(ctx.store, id)
    assert banked.state == :banked

    # Past the banked TTL (banked at ~10_000, ttl 100s): the GC evicts it. Because the
    # instance is now terminal (evicted), a later relight would see it gone -- the
    # wake-time TTL guard falls out of the same terminal state.
    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, {:ok, %{}})
    ServingSweeper.sweep(ctx.sweeper)

    {:ok, instance} = ServingStore.get(ctx.store, id)
    assert instance.state == :evicted
    assert instance.terminal_reason == "idle_ttl"
  end

  # -- stale-base lineage GC (D-R3.11.3 follow-up) ---------------------------

  # A node that reports a CURRENT serving_image_ref for the workload (a runtime roll
  # rebuilt the base to a new key).
  defp serving_node_with_image(ctx, node_id, workload, serving_image_ref) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.99.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{
        workload => %{base_state: :BASE_BUILD_STATE_READY, serving_image_ref: serving_image_ref}
      },
      serving_vms: [],
      serving_snapshots: []
    })
  end

  # A banked instance born from base_snapshot_ref (its lineage) with snapshot_ref.
  defp banked_instance(ctx, id, workload, base_snapshot_ref, snapshot_ref) do
    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "system:serving:#{workload}",
        workload: workload,
        node_id: "node-4",
        vm_id: "vm-#{id}",
        ip: "10.99.0.5",
        port: 8080,
        base_snapshot_ref: base_snapshot_ref
      })

    {:ok, _} = ServingStore.publish(ctx.store, id, "10.99.0.5", 8080, :started)
    {:ok, _} = ServingStore.unpublish(ctx.store, id, :bank)
    {:ok, _} = ServingStore.mark(ctx.store, id, :bank)

    {:ok, _} =
      ServingStore.transition(ctx.store, id, :bank_ready, :serving_banked,
        %{snapshot_ref: snapshot_ref, size_bytes: 10, generation: 1},
        %{snapshot_ref: snapshot_ref, snapshot_size_bytes: 10, generation: 1, vm_id: nil}
      )

    id
  end

  test "stale-lineage GC evicts a banked snapshot whose base is superseded, keeps the current one" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{banked_ttl_seconds: 1_000_000})
    # The node now reports the CURRENT base "base:new" (a runtime roll rebuilt it).
    serving_node_with_image(ctx, "node-4", "wl-a", "base:new")

    banked_instance(ctx, "srv-old", "wl-a", "base:old", "snap-old")
    banked_instance(ctx, "srv-new", "wl-a", "base:new", "snap-new")

    set_scrape(ctx, {:ok, %{}})
    ServingSweeper.sweep(ctx.sweeper)

    # The stale-lineage snapshot is evicted with reason stale_base.
    {:ok, stale} = ServingStore.get(ctx.store, "srv-old")
    assert stale.state == :evicted
    assert stale.terminal_reason == "stale_base"

    # The current-lineage snapshot survives (not evicted, still relightable).
    {:ok, current} = ServingStore.get(ctx.store, "srv-new")
    assert current.state == :banked
  end

  test "stale-lineage GC fails open when the node reports no current serving_image_ref yet" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{banked_ttl_seconds: 1_000_000})
    # The node reports NO serving_image_ref for the workload (new base not built yet).
    serving_node(ctx, "node-4")

    banked_instance(ctx, "srv-1", "wl-a", "base:old", "snap-1")

    set_scrape(ctx, {:ok, %{}})
    ServingSweeper.sweep(ctx.sweeper)

    # Kept, not evicted: with no current ref to compare against, warmth wins.
    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :banked
  end

  # -- forced roll -----------------------------------------------------------

  test "forced roll drains + destroys live instances and evicts banked ones" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{max_lifetime_seconds: 1_000_000})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")
    published_instance(ctx, "srv-2", "wl-a", "vm-2", "10.99.0.6")

    # Bank srv-2 so the roll has one banked victim too.
    {:ok, _} = ServingStore.unpublish(ctx.store, "srv-2", :bank)
    {:ok, _} = ServingStore.mark(ctx.store, "srv-2", :bank)

    {:ok, _} =
      ServingStore.transition(ctx.store, "srv-2", :bank_ready, :serving_banked,
        %{snapshot_ref: "snap-2", size_bytes: 10, generation: 1},
        %{snapshot_ref: "snap-2", snapshot_size_bytes: 10, generation: 1, vm_id: nil}
      )

    result = ServingSweeper.force_roll(ctx.sweeper, "wl-a")
    assert result == %{destroyed: 1, evicted: 1}

    {:ok, i1} = ServingStore.get(ctx.store, "srv-1")
    {:ok, i2} = ServingStore.get(ctx.store, "srv-2")
    assert i1.state == :destroyed
    assert i1.terminal_reason == "forced_roll"
    assert i2.state == :evicted

    # srv-1 (live) was torn down DESTROY; srv-2 (banked) was not (evict is a durable
    # op + node GC, no StopServing).
    assert [%{mode: :STOP_SERVING_MODE_DESTROY, vm_id: "vm-1"}] = stop_calls(ctx)
    # The fan-out is empty (both gone).
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []
  end

  test "forced roll on a workload with no instances is a no-op 200 (zero counts)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    assert ServingSweeper.force_roll(ctx.sweeper, "wl-a") == %{destroyed: 0, evicted: 0}
  end

  # -- concurrent-bank cap ---------------------------------------------------

  test "the per-node concurrent-bank cap defers a second bank until a slot frees" do
    ctx = start_stack(bank_concurrency: 1)
    serving_workload(ctx, "wl-a", %{min_instances: 0, idle_bank_seconds: 60})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")
    published_instance(ctx, "srv-2", "wl-a", "vm-2", "10.99.0.6")

    # Idle both to the drain step.
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)
    advance(ctx.clock_agent, 120_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    # Fire BOTH drain deadlines. With cap=1, only the first admits a bank; the second
    # is deferred (a retry timer re-armed), so exactly ONE StopServing(BANK) has run.
    fire_drain(ctx, "srv-1")
    fire_drain(ctx, "srv-2")
    # Wait until exactly one bank has landed (one instance banked), then assert the
    # cap held: the second is still draining, not banked.
    wait_until(ctx, fn ->
      ServingStore.list(ctx.store, "wl-a") |> Enum.count(&(&1.state == :banked)) == 1
    end)

    bank_calls = Enum.filter(stop_calls(ctx), &(&1.mode == :STOP_SERVING_MODE_BANK))
    assert length(bank_calls) == 1, "cap=1 must serialize banks"

    # The deferred instance re-armed a retry timer.
    assert Enum.any?(timers(ctx), fn {msg, _delay} ->
             msg in [{:bank_drained, "srv-1"}, {:bank_drained, "srv-2"}]
           end)
  end

  test "a failed bank returns the instance to the fan-out (abort, republish)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a", %{min_instances: 0, idle_bank_seconds: 60})
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    Agent.update(ctx.bank_fail, fn _ -> true end)

    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)
    advance(ctx.clock_agent, 120_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 10}})
    ServingSweeper.sweep(ctx.sweeper)

    fire_drain(ctx, "srv-1")
    wait_until(ctx, fn -> match?({:ok, %{state: :published}}, ServingStore.get(ctx.store, "srv-1")) end)

    # The bank failed with the VM still alive: it is republished, back in the fan-out.
    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :published
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == [%{ip: "10.99.0.5", port: 8080}]
  end

  # -- raw stats-body parse path ---------------------------------------------

  test "parse_stats extracts only serve| cluster rq totals from a realistic Envoy body" do
    # The REAL parse path (default_scrape's parser), fed a raw Envoy ?format=json
    # body, not a pre-parsed map: this is the coverage the sweeper/health tests miss
    # (they all inject readings AFTER parsing). It proves the cluster.<name>.
    # upstream_rq_total prefix/suffix stripping yields the exact serve|<workload> key
    # (a mis-strip would drop every serving workload silently) AND that non-serving
    # clusters (admin_loopback, xds_cluster) and non-rq_total cluster gauges are
    # ignored. The workload name itself carries a dot and a pipe, so a naive dot-split
    # or a character-set trim would corrupt it.
    body =
      Jason.encode!(%{
        "stats" => [
          %{"name" => "cluster.serve|wl-a.upstream_rq_total", "value" => 42},
          %{"name" => "cluster.serve|team.svc-b.upstream_rq_total", "value" => 7},
          # Noise that must be ignored: the node's own control clusters, and a
          # non-rq_total gauge on a serving cluster.
          %{"name" => "cluster.admin_loopback.upstream_rq_total", "value" => 999},
          %{"name" => "cluster.xds_cluster.upstream_rq_total", "value" => 5},
          %{"name" => "cluster.serve|wl-a.upstream_rq_active", "value" => 3},
          %{"name" => "server.uptime", "value" => 1_234}
        ]
      })

    assert {:ok, reading} = ServingSweeper.parse_stats(body)

    assert reading == %{
             "serve|wl-a" => 42,
             "serve|team.svc-b" => 7,
             "admin_loopback" => 999,
             "xds_cluster" => 5
           }

    # The stripped cluster names round-trip through the workload extractor: only the
    # serve| clusters map to a workload; the control clusters map to nil (ignored by
    # record_deltas). This is the end-to-end guarantee that the raw body reaches the
    # right workloads.
    assert reading |> Map.keys() |> Enum.map(&workload_of/1) |> Enum.sort() ==
             [nil, nil, "team.svc-b", "wl-a"]
  end

  test "parse_stats rejects an unparseable or non-stats body (fail-open upstream)" do
    assert {:error, _} = ServingSweeper.parse_stats("not json")
    assert {:error, :unparseable_stats} = ServingSweeper.parse_stats(Jason.encode!(%{"nope" => 1}))
  end

  # Mirror the sweeper's private workload_of_cluster/1 for the round-trip assertion
  # (serve|<workload> -> <workload>, anything else -> nil).
  defp workload_of("serve|" <> workload) when workload != "", do: workload
  defp workload_of(_other), do: nil

  # -- helpers ---------------------------------------------------------------

  defp load_ops(ctx, kind) do
    atom = String.to_existing_atom(kind)
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    Enum.filter(ops, &(&1.kind == atom))
  end

  # The op sequence numbers for the given kind strings (first occurrence), for
  # ordering assertions. Kinds are given as strings and matched against the atom
  # op.kind.
  defp op_seqs(ctx, kinds) do
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)

    for kind <- kinds, into: %{} do
      atom = String.to_existing_atom(kind)
      seq = ops |> Enum.find(&(&1.kind == atom)) |> Map.get(:seq)
      {kind, seq}
    end
  end

  # -- status.serving {live,banked,published} writer (Task 10) ----------------

  test "the sweep writes status.serving {live,banked,published} + servingSummary" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")
    # One published instance => live=1 (published is a live state), banked=0,
    # published=1 (the healthy-published set).
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    # A no-op scrape (no prior reading) so the sweep runs without banking anything.
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 0}})
    ServingSweeper.sweep(ctx.sweeper)

    assert [{"embervm", "wl-a", status_map}] = status_writes(ctx)
    assert status_map["serving"] == %{"live" => 1, "banked" => 0, "published" => 1}
    assert status_map["servingSummary"] == "1/0/1"
  end

  test "the status write is DEBOUNCED: no re-write when the counts are unchanged" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 0}})

    # First sweep writes; two further sweeps with identical counts write nothing more.
    ServingSweeper.sweep(ctx.sweeper)
    ServingSweeper.sweep(ctx.sweeper)
    ServingSweeper.sweep(ctx.sweeper)

    assert length(status_writes(ctx)) == 1
  end

  test "a counts CHANGE re-writes status.serving" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 0}})

    ServingSweeper.sweep(ctx.sweeper)

    # Add a second published instance: published goes 1 -> 2, so the next sweep writes.
    published_instance(ctx, "srv-2", "wl-a", "vm-2", "10.99.0.6")
    ServingSweeper.sweep(ctx.sweeper)

    writes = status_writes(ctx)
    assert length(writes) == 2
    assert List.last(writes) |> elem(2) |> Map.get("serving") == %{"live" => 2, "banked" => 0, "published" => 2}
  end

  test "a status-writer error never crashes the sweep (visibility-only)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")
    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 0}})

    # Point the writer at a raising function for THIS sweeper via a fresh stack:
    # simplest is to assert the sweep still returns :ok even though the writer raises.
    :sys.replace_state(ctx.sweeper, fn s -> %{s | status_writer: fn _ns, _n, _m -> raise "boom" end} end)

    assert :ok = ServingSweeper.sweep(ctx.sweeper)
  end

  # -- instance-key unification (PR-B0b) ---------------------------------------

  # A per-instance capacity fact (keyed by {node, pod_uid}) carrying the per-instance
  # live serving-VM inventory the sweeper's dial resolution reads.
  defp put_serving_brick(ctx, node_id, pod_uid, opts) do
    NodeCapacity.put(ctx.cap_table, {node_id, pod_uid}, %{
      configured_id: node_id,
      node_id: node_id,
      pod_uid: pod_uid,
      instance_id: "#{node_id}/#{pod_uid}",
      serving_subnet_cidr: "10.99.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      serving_vms: Keyword.get(opts, :serving_vms, []),
      serving_snapshots: Keyword.get(opts, :serving_snapshots, [])
    })
  end

  test "the serving bank dials the OWNER instance_id even when the node-name alias points at a sibling" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    capture_channel = fn key ->
      Agent.update(dialed, &[key | &1])
      {:ok, :ch}
    end

    ctx = start_stack(channel_fun: capture_channel)
    serving_workload(ctx, "wl-a", %{min_instances: 0, idle_bank_seconds: 60, drain_seconds: 5})

    # Two co-located instances on node-4. pod-owner RUNS vm-1; pod-sibling does not (it
    # is the last registrant the node-name alias would collapse to). The bank must dial
    # pod-owner, never the alias.
    put_serving_brick(ctx, "node-4", "pod-sibling", serving_vms: [])

    put_serving_brick(ctx, "node-4", "pod-owner",
      serving_vms: [%{vm_id: "vm-1", workload: "wl-a", ip: "10.99.0.5", port: 8080, healthy: true}]
    )

    published_instance(ctx, "srv-1", "wl-a", "vm-1", "10.99.0.5")

    set_scrape(ctx, {:ok, %{"serve|wl-a" => 0}})
    ServingSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 70_000)
    set_scrape(ctx, {:ok, %{"serve|wl-a" => 0}})
    ServingSweeper.sweep(ctx.sweeper)

    fire_drain(ctx, "srv-1")
    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, ServingStore.get(ctx.store, "srv-1")) end)

    assert "node-4/pod-owner" in Agent.get(dialed, & &1)
    refute "node-4" in Agent.get(dialed, & &1)
  end
end
