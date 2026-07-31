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
  alias Embervm.Node.V1.{EvictArtifactResponse, EvictSnapshotResponse, ResolveStatefulResponse, StopStatefulResponse}

  # Consecutive broken sweeps required before an eager eviction fires (the
  # StatefulStore @broken_evict_threshold hysteresis). One sweep = one
  # eager_evict_broken_pairs observation. Defined here (before its first use) so
  # both the generation-guard test and the broken-pair test can reference it.
  @broken_evict_sweeps 3

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

  # A stand-in StatefulManager that records the {:checkpoint_resolved, workload,
  # outcome} casts the sweeper sends on resolve, so a test asserts the manager was
  # notified. Also answers parked?/2 falsely (unused here; the sweeper's
  # parked_fun is injected directly off an Agent).
  defmodule FakeManager do
    use GenServer
    def start_link(notes_agent), do: GenServer.start_link(__MODULE__, notes_agent)
    @impl true
    def init(notes_agent), do: {:ok, notes_agent}
    @impl true
    def handle_cast({:checkpoint_resolved, workload, outcome}, notes) do
      Agent.update(notes, &[{workload, outcome} | &1])
      {:noreply, notes}
    end

    @impl true
    def handle_call({:parked?, _workload}, _from, notes), do: {:reply, false, notes}
  end

  # An Embervm.OpLog backend that delegates every callback to the real SQLite
  # backend EXCEPT append/2 for a :generation_blessed op, which it fails
  # unconditionally. Used to exercise plan_resolve_blessing/3's
  # bless_generation-fails-so-force-COMMIT branch (StatefulSweeper.ex's
  # `defp plan_resolve_blessing(state, workload, :abort)`): the sweeper's own
  # StatefulStore is a real GenServer whose op_log_mod is dispatched per-call
  # (see StatefulStore's moduledoc), so swapping ONLY this module in for the
  # workload's StatefulStore reproduces a genuine op-log append failure exactly
  # as StatefulStore.bless_generation/3 would see it in production, rather than
  # forcing the {:error, _} branch artificially from outside the real call path.
  defmodule FailingBlessOpLog do
    @behaviour Embervm.OpLog
    alias Embervm.OpLog.SQLite

    @impl true
    def append(server, %Embervm.OpLog.Op{kind: :generation_blessed} = _op) do
      _ = server
      {:error, :bless_write_boom}
    end

    def append(server, op), do: SQLite.append(server, op)

    @impl true
    def read_from(server, seq), do: SQLite.read_from(server, seq)
    @impl true
    def load_tasks(server), do: SQLite.load_tasks(server)
    @impl true
    def load_sessions(server), do: SQLite.load_sessions(server)
    @impl true
    def load_serving_instances(server), do: SQLite.load_serving_instances(server)
    @impl true
    def load_stateful_instances(server), do: SQLite.load_stateful_instances(server)
    @impl true
    def load_volumes(server), do: SQLite.load_volumes(server)
    @impl true
    def load_volume_blessing(server), do: SQLite.load_volume_blessing(server)
    @impl true
    def load_blessing_leases(_), do: {:ok, []}
    @impl true
    def load_checkpoint_dispatches(server), do: SQLite.load_checkpoint_dispatches(server)
    @impl true
    def load_group_instances(server), do: SQLite.load_group_instances(server)
    @impl true
    def load_group_members(server), do: SQLite.load_group_members(server)
    @impl true
    def load_result(server, task_id), do: SQLite.load_result(server, task_id)
    @impl true
    def load_request(server, task_id), do: SQLite.load_request(server, task_id)
    @impl true
    def list_usage(server, opts), do: SQLite.list_usage(server, opts)
    @impl true
    def compact(server, now_ms), do: SQLite.compact(server, now_ms)
    @impl true
    def compacted_through(server), do: SQLite.compacted_through(server)
    @impl true
    def evict_task(server, task_id), do: SQLite.evict_task(server, task_id)
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
    # store_op_log_mod: the Embervm.OpLog backend the STORE's own op_log_mod
    # dispatches to (default the real SQLite). A test that needs a genuine
    # bless_generation append failure (rather than the idempotent
    # at-or-below-watermark no-op, which is NOT an error) injects
    # FailingBlessOpLog here, exercising StatefulSweeper's
    # plan_resolve_blessing/3 force-commit branch through the real call path.
    store_op_log_mod = Keyword.get(opts, :store_op_log_mod, SQLite)

    {:ok, store} =
      StatefulStore.start_link(name: nil, op_log: op_log, op_log_mod: store_op_log_mod, clock: clock(clock_agent))

    {:ok, pub} = FakePublisher.start_link()

    # The scrape seam: an Agent holds the CURRENT reading the next scrape
    # returns. A reading is %{stat_prefix => %{active: n, total: n}} or
    # {:error, reason}.
    {:ok, scrape_agent} = Agent.start_link(fn -> {:ok, %{}} end)

    {:ok, stop_calls} = Agent.start_link(fn -> [] end)
    {:ok, bank_fail} = Agent.start_link(fn -> false end)
    {:ok, evict_calls} = Agent.start_link(fn -> [] end)
    {:ok, resolve_calls} = Agent.start_link(fn -> [] end)
    {:ok, resolve_fail} = Agent.start_link(fn -> false end)
    # Whether a connection is "parked" for the checkpoint fork (commit vs abort).
    # A test flips this before the resolve decision runs.
    {:ok, parked} = Agent.start_link(fn -> false end)
    {:ok, resolved_notes} = Agent.start_link(fn -> [] end)

    stop_stateful_fun = fn _ch, req ->
      Agent.update(stop_calls, &[req | &1])

      cond do
        req.mode == :STOP_STATEFUL_MODE_BANK and Agent.get(bank_fail, & &1) ->
          {:error, :bank_boom}

        req.mode == :STOP_STATEFUL_MODE_BANK ->
          {:ok, %StopStatefulResponse{snapshot_ref: "snap-#{req.vm_id}", size_bytes: 4_096, generation: 1}}

        req.mode == :STOP_STATEFUL_MODE_CHECKPOINT ->
          {:ok, %StopStatefulResponse{checkpoint_token: "ckpt-#{req.vm_id}", generation: 1}}

        true ->
          {:ok, %StopStatefulResponse{}}
      end
    end

    resolve_stateful_fun = fn _ch, req ->
      Agent.update(resolve_calls, &[req | &1])

      if Agent.get(resolve_fail, & &1) do
        {:error, :resolve_boom}
      else
        case req.mode do
          :RESOLVE_MODE_COMMIT ->
            {:ok, %ResolveStatefulResponse{snapshot_ref: "snap-#{req.vm_id}", generation: 2, size_bytes: 8_192}}

          :RESOLVE_MODE_ABORT ->
            {:ok, %ResolveStatefulResponse{}}
        end
      end
    end

    # A fake manager (a GenServer that records {:checkpoint_resolved, ...} casts),
    # so the sweeper's notify + the parked_fun both have a real ref. parked_fun is
    # injected directly off the `parked` Agent so the commit/abort fork is
    # deterministic without wiring a real StatefulManager.
    {:ok, fake_mgr} = FakeManager.start_link(resolved_notes)

    evict_snapshot_fun = fn _ch, req ->
      Agent.update(evict_calls, &[req | &1])
      {:ok, %EvictSnapshotResponse{}}
    end

    {:ok, evict_artifact_calls} = Agent.start_link(fn -> [] end)

    evict_artifact_fun = fn _ch, req ->
      Agent.update(evict_artifact_calls, &[req | &1])
      {:ok, %EvictArtifactResponse{bytes_freed: 4096}}
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
        channel_fun: Keyword.get(opts, :channel_fun, fn _node -> {:ok, :ch} end),
        invalidate_fun: Keyword.get(opts, :invalidate_fun, fn _n, _c -> :ok end),
        stop_stateful_fun: stop_stateful_fun,
        resolve_stateful_fun: resolve_stateful_fun,
        evict_snapshot_fun: evict_snapshot_fun,
        evict_artifact_fun: evict_artifact_fun,
        manager: fake_mgr,
        parked_fun: fn _wl -> Agent.get(parked, & &1) end,
        flap_abort_threshold: Keyword.get(opts, :flap_abort_threshold, 20),
        # Instant tests: never actually sleep on the propagation settle.
        propagation_settle_ms: Keyword.get(opts, :propagation_settle_ms, 0),
        bank_concurrency: Keyword.get(opts, :bank_concurrency, 1),
        lifetime_drain_max_ms: Keyword.get(opts, :lifetime_drain_max_ms, 3_600_000),
        bank_backoff_base_ms: Keyword.get(opts, :bank_backoff_base_ms, 1_000),
        bank_backoff_cap_ms: Keyword.get(opts, :bank_backoff_cap_ms, 30_000),
        sweep_interval_ms: 0
      ]

    # The status.stateful writer seam (Task 10): records every {namespace, name,
    # status_map} the sweep patches, so a test asserts the debounce (one write
    # per changed workload per sweep, none when unchanged). Mirrors
    # ServingSweeperTest's status_writer seam.
    {:ok, status_calls} = Agent.start_link(fn -> [] end)

    status_writer = fn namespace, name, status_map ->
      Agent.update(status_calls, &[{namespace, name, status_map} | &1])
      :ok
    end

    sweeper_opts = Keyword.put(sweeper_opts, :status_writer, status_writer)

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
      evict_calls: evict_calls,
      evict_artifact_calls: evict_artifact_calls,
      status_calls: status_calls,
      resolve_calls: resolve_calls,
      resolve_fail: resolve_fail,
      parked: parked,
      fake_mgr: fake_mgr,
      resolved_notes: resolved_notes
    }
  end

  defp set_scrape(ctx, reading), do: Agent.update(ctx.scrape_agent, fn _ -> reading end)
  defp stop_calls(ctx), do: Agent.get(ctx.stop_calls, &Enum.reverse(&1))
  defp evict_calls(ctx), do: Agent.get(ctx.evict_calls, &Enum.reverse(&1))
  defp evict_artifact_calls(ctx), do: Agent.get(ctx.evict_artifact_calls, &Enum.reverse(&1))
  defp status_writes(ctx), do: Agent.get(ctx.status_calls, &Enum.reverse(&1))
  defp resolve_calls(ctx), do: Agent.get(ctx.resolve_calls, &Enum.reverse(&1))
  defp resolved_notes(ctx), do: Agent.get(ctx.resolved_notes, &Enum.reverse(&1))
  defp set_parked(ctx, v), do: Agent.update(ctx.parked, fn _ -> v end)

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
      true ->
        # Yield a scheduler slot between tries. flush only drains the sweeper's
        # own mailbox; a spawned bank worker (posts {:bank_done}) is a separate
        # process that must be scheduled before the store reaches :banked.
        # Without this backoff the loop spins through all tries in a couple of ms
        # and starves the worker under the 8-way parallel CI runner (the historic
        # "condition never held" idle-bank flake).
        Process.sleep(10)
        wait_until(ctx, fun, tries - 1)
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

  # -- remote retention / GC (R6, Task 9) -------------------------------------

  test "a banked-TTL eviction ALSO issues EvictArtifact(remote) for the bundle; the volume is never remote-evicted" do
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

    # The remote store copy of the bundle is dropped on the SAME trigger: an
    # EvictArtifact(remote=true) for the STATEFUL bundle. NEVER a VOLUME evict (a
    # bundle eviction never strands a volume generation, standing decision 8).
    assert [req] = evict_artifact_calls(ctx)
    assert req.remote == true
    assert req.artifact.kind == :ARTIFACT_KIND_STATEFUL
    assert req.artifact.ref == "snap-sf-1"
    assert req.artifact.workload == "wl-a"
    refute Enum.any?(evict_artifact_calls(ctx), &(&1.artifact.kind == :ARTIFACT_KIND_VOLUME))
  end

  test "the generation guard: a superseded-generation (broken pair) eviction remote-evicts the BUNDLE but never the volume" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{banked_ttl_seconds: 1_000_000, max_lifetime_seconds: 1_000_000})
    stateful_node(ctx, "node-4")

    # A volume that has moved ON to generation 5, and a bundle stamped at generation
    # 2: a broken pair (superseded generation). The eager sweep evicts the stale
    # bundle. The store must keep the volume (its generation 5 is one a future bundle
    # will pair with), so no VOLUME remote-evict is ever issued.
    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 5,
        size_bytes: 1_073_741_824,
        allocated_bytes: 100
      })

    banked_instance(ctx, "sf-stale", "wl-a", "vm-1", 2)
    refute StatefulStore.pair_valid?(ctx.store, "wl-a")

    set_scrape(ctx, reading("state-5400", 0, 0))

    # Hysteresis: a genuinely broken pair is evicted only after @broken_evict_sweeps
    # consecutive broken sweeps (see the eager broken-pair eviction section below).
    for _ <- 1..(@broken_evict_sweeps - 1) do
      StatefulSweeper.sweep(ctx.sweeper)
      assert {:ok, %{state: :banked}} = StatefulStore.get(ctx.store, "sf-stale")
    end

    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-stale")
    assert instance.state == :evicted

    # The bundle's remote copy is dropped; the volume's is NOT (generation guard).
    assert Enum.any?(evict_artifact_calls(ctx), &(&1.artifact.kind == :ARTIFACT_KIND_STATEFUL and &1.artifact.ref == "snap-sf-stale"))
    refute Enum.any?(evict_artifact_calls(ctx), &(&1.artifact.kind == :ARTIFACT_KIND_VOLUME))
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

  test "a banked instance whose volume generation moved is evicted after the grace window of sweeps (broken pair)" do
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

    # The volume's generation moves FORWARD (e.g. a concurrent cold-boot attach
    # bumped it), genuinely breaking the pair.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{generation: 2})
    refute StatefulStore.pair_valid?(ctx.store, "wl-a")

    set_scrape(ctx, reading("state-5400", 0, 0))

    # Hysteresis: the pair is not evicted until it has been observed broken on
    # @broken_evict_sweeps consecutive sweeps (a single blip must not drop it).
    for _ <- 1..(@broken_evict_sweeps - 1) do
      StatefulSweeper.sweep(ctx.sweeper)
      assert {:ok, %{state: :banked}} = StatefulStore.get(ctx.store, "sf-1")
    end

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

  # -- interruptible bank (ADR embervm/008) -------------------------------------

  # Drive an interruptible workload's serving instance to the point a bank fires,
  # returning after the first two priming ticks (baseline + idle_since set).
  defp prime_idle_interruptible(ctx, workload, listen_port, id, vm_id) do
    stateful_workload(ctx, workload, listen_port, %{idle_bank_seconds: 60, interruptible_bank: true})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, id, workload, vm_id, "10.98.0.5")

    prefix = "state-#{listen_port}"
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    prefix
  end

  test "interruptible workload NOT parked -> CHECKPOINT then COMMIT: instance banked, bundle recorded, resolve COMMIT" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, false)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, banked} = StatefulStore.get(ctx.store, "sf-1")
    assert banked.state == :banked
    assert banked.snapshot_ref == "snap-vm-1"
    assert banked.snapshot_generation == 2

    # A CHECKPOINT (not an atomic BANK) was issued, then a COMMIT resolve.
    modes = Enum.map(stop_calls(ctx), & &1.mode)
    assert :STOP_STATEFUL_MODE_CHECKPOINT in modes
    refute :STOP_STATEFUL_MODE_BANK in modes

    assert [%{mode: :RESOLVE_MODE_COMMIT, vm_id: "vm-1", checkpoint_token: "ckpt-vm-1"}] = resolve_calls(ctx)

    # The manager was notified of the commit (drain the fake manager's mailbox first).
    _ = :sys.get_state(ctx.fake_mgr)
    assert {"wl-i", :commit} in resolved_notes(ctx)

    ops = load_ops(ctx, "stateful_banked")
    assert length(ops) == 1
  end

  test "interruptible workload PARKED -> CHECKPOINT then ABORT: instance back to serving, no bundle, resolve ABORT, aborts incremented" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, true)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :serving}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving
    assert instance.ip == "10.98.0.5"
    assert instance.port == 5432

    assert [%{mode: :RESOLVE_MODE_ABORT, vm_id: "vm-1"}] = resolve_calls(ctx)

    # No bundle was committed.
    assert load_ops(ctx, "stateful_banked") == []

    # The consecutive-abort counter was incremented for the workload.
    aborts = :sys.get_state(ctx.sweeper).checkpoint_aborts
    assert Map.get(aborts, "wl-i") == 1

    _ = :sys.get_state(ctx.fake_mgr)
    assert {"wl-i", :abort} in resolved_notes(ctx)
  end

  # -- R7 abort-blessing (ADR embervm/011, standing decision 4) ---------------

  test "an ABORT resolve blesses next_blessed_generation BEFORE dispatch and threads it into the ResolveStatefulRequest" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, true)

    # Nothing blessed yet for this never-blessed workload.
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-i") == 1

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :serving}}, StatefulStore.get(ctx.store, "sf-1")) end)

    # The captured ResolveStateful request carries the blessed generation (1, the
    # first blessing for a never-blessed workload), not 0.
    assert [%{mode: :RESOLVE_MODE_ABORT, vm_id: "vm-1", blessed_generation: 1}] = resolve_calls(ctx)

    # The store's blessing watermark advanced to what was dispatched, and a
    # SECOND abort cycle blesses the NEXT generation past it (2), proving the
    # watermark is durable across cycles, not just threaded once.
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-i") == 2

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)
    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> length(resolve_calls(ctx)) >= 2 end)

    assert [_first, %{mode: :RESOLVE_MODE_ABORT, blessed_generation: 2}] = resolve_calls(ctx)
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-i") == 3
  end

  test "a COMMIT resolve does NOT bless: the watermark is unchanged and the request carries blessed_generation 0" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, false)

    assert StatefulStore.next_blessed_generation(ctx.store, "wl-i") == 1

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    assert [%{mode: :RESOLVE_MODE_COMMIT, blessed_generation: 0}] = resolve_calls(ctx)

    # A COMMIT never appends a generation_blessed op, so the watermark is exactly
    # as unblessed as before the cycle ran.
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-i") == 1
  end

  # -- R7 checkpoint-abort auto-heal record (ADR embervm/017) -----------------

  test "a CHECKPOINT records a checkpoint_dispatched op, and a COMMIT resolve clears it (checkpoint_resolved)" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, false)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    # The dispatch was recorded when the VM paused, and the CP-driven COMMIT cleared
    # it so a resolved checkpoint can never auto-heal a later unrelated +1.
    assert length(load_ops(ctx, "checkpoint_dispatched")) == 1
    assert length(load_ops(ctx, "checkpoint_resolved")) == 1
  end

  test "a CHECKPOINT then ABORT records the dispatch and clears it on the CP-driven abort" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, true)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :serving}}, StatefulStore.get(ctx.store, "sf-1")) end)

    assert length(load_ops(ctx, "checkpoint_dispatched")) == 1
    assert length(load_ops(ctx, "checkpoint_resolved")) == 1
  end

  test "a bless_generation failure forces the resolve to COMMIT instead of dispatching an unblessed abort" do
    # A stale/lower-generation collision against StatefulStore.bless_generation/3
    # is NOT an error (the store's monotonicity guard treats it as an idempotent
    # no-op returning {:ok, fact}; see StatefulStore's handle_call({:bless_generation,
    # ...}) clause), so it cannot be used to trigger plan_resolve_blessing/3's
    # {:error, reason} branch. The only way bless_generation/3 genuinely returns
    # {:error, _} is an op-log append failure. FailingBlessOpLog fails exactly
    # (only) the :generation_blessed append, through the real StatefulStore call
    # path (state.store here is a real StatefulStore GenServer, just backed by a
    # failing op-log for this one op kind), so this reproduces the real failure
    # StatefulSweeper.plan_resolve_blessing/3 must recover from, not an artificial
    # short-circuit.
    ctx = start_stack(store_op_log_mod: FailingBlessOpLog)
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    # A parked connection would normally decide ABORT; the bless failure must
    # override that decision to COMMIT regardless.
    set_parked(ctx, true)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    # Forced COMMIT despite parked==true: no generation was blessed to abort with,
    # so plan_resolve_blessing/3 refuses to dispatch an unblessed abort and falls
    # back to the always-ledger-safe COMMIT.
    assert [%{mode: :RESOLVE_MODE_COMMIT, blessed_generation: 0}] = resolve_calls(ctx)

    {:ok, banked} = StatefulStore.get(ctx.store, "sf-1")
    assert banked.state == :banked

    # The watermark never advanced (the append failed): still 1 for the very
    # first bless attempt of this never-blessed workload.
    assert StatefulStore.next_blessed_generation(ctx.store, "wl-i") == 1
  end

  test "flap guard: at the abort threshold the next cycle FORCES COMMIT even though parked, and resets the counter" do
    ctx = start_stack(flap_abort_threshold: 3)
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    # Pre-seed the counter AT the threshold so this cycle force-commits.
    :sys.replace_state(ctx.sweeper, fn s -> %{s | checkpoint_aborts: %{"wl-i" => 3}} end)
    set_parked(ctx, true)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    # Forced commit despite parked==true.
    assert [%{mode: :RESOLVE_MODE_COMMIT}] = resolve_calls(ctx)

    # Counter reset (settled to banked).
    aborts = :sys.get_state(ctx.sweeper).checkpoint_aborts
    assert Map.get(aborts, "wl-i") == 0
  end

  test "a resolve RPC error FAILS the instance (checkpointed -> failed)" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, false)
    Agent.update(ctx.resolve_fail, fn _ -> true end)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :failed}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, failed} = StatefulStore.get(ctx.store, "sf-1")
    assert failed.state == :failed
    assert load_ops(ctx, "stateful_failed") != []
  end

  test "a resolve rejected FAILED_PRECONDITION (noded auto-aborted first) reconciles to serving, not failed (ADR 008)" do
    ctx = start_stack()
    prefix = prime_idle_interruptible(ctx, "wl-i", 5400, "sf-1", "vm-1")

    set_parked(ctx, false)
    # noded's resolve-timeout auto-abort won the single-resolve race: our late
    # resolve is rejected FAILED_PRECONDITION (gRPC status 9). noded's auto-abort
    # RESUMES the VM hot (never tears it down), so the instance must reconcile to
    # :serving rather than :failed (which would orphan a healthy live VM).
    :sys.replace_state(ctx.sweeper, fn s ->
      %{
        s
        | resolve_stateful_fun: fn _ch, _req ->
            {:error, %GRPC.RPCError{status: 9, message: "already resolved"}}
          end
      }
    end)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading(prefix, 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :serving}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving
    # No bundle committed and NOT failed: the VM is live and serving on the node.
    assert load_ops(ctx, "stateful_banked") == []
    assert load_ops(ctx, "stateful_failed") == []
    # Parked callers were told :abort (served hot).
    _ = :sys.get_state(ctx.fake_mgr)
    assert {"wl-i", :abort} in resolved_notes(ctx)
  end

  test "non-interruptible workload is unchanged: atomic BANK, never CHECKPOINT/ResolveStateful" do
    ctx = start_stack()
    # Default cfg: interruptible_bank absent (false).
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    modes = Enum.map(stop_calls(ctx), & &1.mode)
    assert :STOP_STATEFUL_MODE_BANK in modes
    refute :STOP_STATEFUL_MODE_CHECKPOINT in modes
    assert resolve_calls(ctx) == []
  end

  test "fail-closed recheck: an interruptible workload does NOT checkpoint when the recheck scrape fails" do
    ctx = start_stack()

    stateful_workload(ctx, "wl-i", 5400, %{idle_bank_seconds: 60, interruptible_bank: true})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-i", "vm-1", "10.98.0.5")

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # Tick 3: the tick-start scan reads idle (call 0), but the recheck re-scrape
    # (call 1) FAILS. For an interruptible workload this fails CLOSED: no
    # checkpoint issued, the instance stays serving.
    {:ok, call_count} = Agent.start_link(fn -> 0 end)

    scrape_fun = fn _url ->
      n = Agent.get_and_update(call_count, fn n -> {n, n + 1} end)
      if n == 0, do: reading("state-5400", 0, 3), else: {:error, :timeout}
    end

    :sys.replace_state(ctx.sweeper, fn s -> %{s | scrape_fun: scrape_fun} end)

    advance(ctx.clock_agent, 65_000)
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving
    assert stop_calls(ctx) == []
    assert resolve_calls(ctx) == []
  end

  test "fail-open recheck unchanged for a non-interruptible workload when the recheck scrape fails" do
    ctx = start_stack()

    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    {:ok, call_count} = Agent.start_link(fn -> 0 end)

    scrape_fun = fn _url ->
      n = Agent.get_and_update(call_count, fn n -> {n, n + 1} end)
      if n == 0, do: reading("state-5400", 0, 3), else: {:error, :timeout}
    end

    :sys.replace_state(ctx.sweeper, fn s -> %{s | scrape_fun: scrape_fun} end)

    advance(ctx.clock_agent, 65_000)
    StatefulSweeper.sweep(ctx.sweeper)

    # Fail-open: the atomic bank still proceeds despite the failed recheck scrape.
    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    modes = Enum.map(stop_calls(ctx), & &1.mode)
    assert :STOP_STATEFUL_MODE_BANK in modes
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

  # -- status.stateful {state,generation,bundleGeneration,volumeBytes} writer (Task 10) --

  test "the sweep writes status.stateful {state,generation,bundleGeneration,volumeBytes} + statefulSummary" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400)
    stateful_node(ctx, "node-4")

    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 3,
        size_bytes: 1_073_741_824,
        allocated_bytes: 555_000
      })

    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    # A no-op scrape (no prior reading) so the sweep runs without banking anything.
    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    assert [{"embervm", "wl-a", status_map}] = status_writes(ctx)

    assert status_map["stateful"] == %{
             "state" => "serving",
             "generation" => 0,
             # No banked bundle exists yet: bundleGeneration reads 0 (never nil).
             "bundleGeneration" => 0,
             "volumeBytes" => 555_000
           }

    assert status_map["statefulSummary"] == "serving gen=0/bundle=0"
  end

  test "status.stateful reports the banked bundle's snapshot_generation as bundleGeneration" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400)
    stateful_node(ctx, "node-4")

    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 2,
        size_bytes: 1_073_741_824,
        allocated_bytes: 42
      })

    banked_instance(ctx, "sf-1", "wl-a", "vm-1", 2)

    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    assert [{"embervm", "wl-a", status_map}] = status_writes(ctx)

    assert status_map["stateful"] == %{
             # No live (non-terminal) instance: state reads "" (never nil).
             "state" => "",
             "generation" => 0,
             "bundleGeneration" => 2,
             "volumeBytes" => 42
           }
  end

  test "the status write is DEBOUNCED: no re-write when the quad is unchanged" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400)
    stateful_node(ctx, "node-4")

    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 1,
        size_bytes: 1_073_741_824,
        allocated_bytes: 10
      })

    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")
    set_scrape(ctx, reading("state-5400", 0, 0))

    # First sweep writes; two further sweeps with identical values write nothing more.
    StatefulSweeper.sweep(ctx.sweeper)
    StatefulSweeper.sweep(ctx.sweeper)
    StatefulSweeper.sweep(ctx.sweeper)

    assert length(status_writes(ctx)) == 1
  end

  test "a quad CHANGE re-writes status.stateful" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400)
    stateful_node(ctx, "node-4")

    {:ok, _} =
      StatefulStore.create_volume(ctx.store, "wl-a", %{
        node_id: "node-4",
        generation: 1,
        size_bytes: 1_073_741_824,
        allocated_bytes: 10
      })

    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")
    set_scrape(ctx, reading("state-5400", 0, 0))
    StatefulSweeper.sweep(ctx.sweeper)

    # The volume's allocated_bytes grows (the daemon's next NodeStatus report):
    # volumeBytes changes, so the next sweep writes again.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{allocated_bytes: 20})
    StatefulSweeper.sweep(ctx.sweeper)

    writes = status_writes(ctx)
    assert length(writes) == 2
    assert List.last(writes) |> elem(2) |> get_in(["stateful", "volumeBytes"]) == 20
  end

  test "a status-writer error never crashes the sweep (visibility-only)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400)
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")
    set_scrape(ctx, reading("state-5400", 0, 0))

    :sys.replace_state(ctx.sweeper, fn s -> %{s | status_writer: fn _ns, _n, _m -> raise "boom" end} end)

    assert :ok = StatefulSweeper.sweep(ctx.sweeper)
  end

  # -- helpers --------------------------------------------------------------

  defp load_ops(ctx, kind) do
    atom = String.to_existing_atom(kind)
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    Enum.filter(ops, &(&1.kind == atom))
  end

  # -- instance-key unification (PR-B0a) ---------------------------------------

  # Put one per-instance capacity fact (keyed by {node, pod_uid}) carrying the
  # per-instance live-VM / bundle inventory the sweeper's dial resolution reads.
  defp put_brick(ctx, node_id, pod_uid, opts) do
    NodeCapacity.put(ctx.cap_table, {node_id, pod_uid}, %{
      node_id: node_id,
      configured_id: node_id,
      pod_uid: pod_uid,
      instance_id: "#{node_id}/#{pod_uid}",
      serving_subnet_cidr: "10.98.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      stateful_vms: Keyword.get(opts, :stateful_vms, []),
      stateful_bundles: Keyword.get(opts, :stateful_bundles, []),
      volumes: []
    })
  end

  test "the bank dials the OWNER instance_id even when the node-name alias points at a sibling" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    capture_channel = fn key ->
      Agent.update(dialed, &[key | &1])
      {:ok, :ch}
    end

    ctx = start_stack(channel_fun: capture_channel)
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})

    # Two co-located instances on node-4. pod-owner RUNS vm-1; pod-sibling does not
    # (it is the last registrant the node-name alias would collapse to). The bank
    # must dial pod-owner, never the alias.
    put_brick(ctx, "node-4", "pod-sibling", stateful_vms: [])
    put_brick(ctx, "node-4", "pod-owner",
      stateful_vms: [%{vm_id: "vm-1", workload: "wl-a", ip: "10.98.0.5", port: 5432, healthy: true, generation: 0}]
    )

    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "sf-1")) end)

    # The StopStateful(BANK) dialled the owning instance, not the node-name alias.
    assert "node-4/pod-owner" in Agent.get(dialed, & &1)
    refute "node-4" in Agent.get(dialed, & &1)
  end

  test "an owner-resolved unknown-vm FAILED_PRECONDITION terminalizes the instance (-> :failed), not loop/republish" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    # An OWNER-RESOLVED dial: a per-instance fact reports the live vm_id, so the
    # unknown-vm reply is authoritative (we reached the exact owner). Only then does
    # the terminalize fire (task #12).
    put_brick(ctx, "node-4", "pod-owner",
      stateful_vms: [%{vm_id: "vm-1", workload: "wl-a", ip: "10.98.0.5", port: 5432, healthy: true, generation: 0}]
    )

    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    # The daemon rejects the bank FAILED_PRECONDITION (status 9): it does not own
    # this VM ("not bankable / unknown vm"). The instance must FAIL, not abort back
    # to serving and re-drive next tick.
    :sys.replace_state(ctx.sweeper, fn s ->
      %{
        s
        | stop_stateful_fun: fn _ch, req ->
            if req.mode == :STOP_STATEFUL_MODE_BANK do
              {:error, %GRPC.RPCError{status: 9, message: "not bankable (unknown vm)"}}
            else
              {:ok, %StopStatefulResponse{}}
            end
          end
      }
    end)

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :failed}}, StatefulStore.get(ctx.store, "sf-1")) end)

    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :failed
    assert load_ops(ctx, "stateful_banked") == []
    # A durable stateful_failed was recorded (the wake path cold-boots next).
    assert length(load_ops(ctx, "stateful_failed")) == 1
  end

  test "a FAIL-OPEN unknown-vm FAILED_PRECONDITION does NOT terminalize (backs off instead)" do
    # No per-instance fact reports vm-1, so the dial FALLS OPEN to the bare node name
    # (owner_resolved false). Under co-location the alias could be the wrong sibling
    # (a stale-fact race), so an unknown-vm reply here is NOT authoritative: task #12
    # requires we back off (abort + arm backoff), NOT fail the instance (which would
    # risk cold-booting a second VM on the same volume). The instance stays serving.
    ctx = start_stack(bank_backoff_base_ms: 10_000, bank_backoff_cap_ms: 30_000)
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    :sys.replace_state(ctx.sweeper, fn s ->
      %{
        s
        | stop_stateful_fun: fn _ch, req ->
            if req.mode == :STOP_STATEFUL_MODE_BANK do
              {:error, %GRPC.RPCError{status: 9, message: "not bankable (unknown vm)"}}
            else
              {:ok, %StopStatefulResponse{}}
            end
          end
      }
    end)

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # It aborted back to serving (NOT failed), and recorded no durable stateful_failed.
    wait_until(ctx, fn -> match?({:ok, %{state: :serving}}, StatefulStore.get(ctx.store, "sf-1")) end)
    {:ok, instance} = StatefulStore.get(ctx.store, "sf-1")
    assert instance.state == :serving
    assert load_ops(ctx, "stateful_failed") == []

    # And it armed the backoff: a sweep 1 s later (inside the 10 s window) does not
    # re-drive the bank.
    bank_calls = fn -> Enum.count(stop_calls(ctx), &(&1.mode == :STOP_STATEFUL_MODE_BANK)) end
    n = bank_calls.()

    advance(ctx.clock_agent, 1_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)
    flush(ctx)
    assert bank_calls.() == n
  end

  test "a NON-terminal bank error backs off (does not re-drive at sweep frequency)" do
    ctx = start_stack(bank_backoff_base_ms: 10_000, bank_backoff_cap_ms: 30_000)
    stateful_workload(ctx, "wl-a", 5400, %{idle_bank_seconds: 60})
    stateful_node(ctx, "node-4")
    serving_instance(ctx, "sf-1", "wl-a", "vm-1", "10.98.0.5")

    # A transient (non-FAILED_PRECONDITION) bank failure: abort back to serving,
    # arm the backoff.
    Agent.update(ctx.bank_fail, fn _ -> true end)

    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    # This tick crosses idle: the first bank fires and fails, arming the backoff.
    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)

    wait_until(ctx, fn -> match?({:ok, %{state: :serving}}, StatefulStore.get(ctx.store, "sf-1")) end)

    bank_calls = fn -> Enum.count(stop_calls(ctx), &(&1.mode == :STOP_STATEFUL_MODE_BANK)) end
    assert bank_calls.() == 1

    # A sweep only 1 s later (well inside the 10 s backoff) must NOT re-drive the
    # bank: this is the loop the backoff kills.
    advance(ctx.clock_agent, 1_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)
    flush(ctx)
    assert bank_calls.() == 1

    # Past the backoff window, the bank is re-driven (still failing, still one more
    # attempt: the point is it backed OFF, not that it gave up).
    advance(ctx.clock_agent, 11_000)
    set_scrape(ctx, reading("state-5400", 0, 3))
    StatefulSweeper.sweep(ctx.sweeper)
    wait_until(ctx, fn -> bank_calls.() == 2 end)
    assert bank_calls.() == 2
  end

  describe "drain_node/2 (R6 force-bank)" do
    test "banks a live instance even though it is NOT idle (bypasses the idle predicate)" do
      ctx = start_stack()
      stateful_workload(ctx, "scratch-postgres", 5433)
      stateful_node(ctx, "node-4")
      serving_instance(ctx, "i1", "scratch-postgres", "vm-1", "10.98.0.5")

      # An ACTIVE connection: the idle pass would never bank this. Drain must.
      set_scrape(ctx, reading("state-5433", 1, 100))

      assert StatefulSweeper.drain_node(ctx.sweeper, "node-4") == 1

      wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "i1")) end)
      assert [%{mode: :STOP_STATEFUL_MODE_BANK}] = stop_calls(ctx)
    end

    test "an interruptible workload COMMITs on drain even with a parked connection" do
      ctx = start_stack()
      stateful_workload(ctx, "scratch-postgres", 5433, %{interruptible_bank: true})
      stateful_node(ctx, "node-4")
      serving_instance(ctx, "i1", "scratch-postgres", "vm-1", "10.98.0.5")

      # A parked caller wants it hot NOW: without drain, decide_resolve ABORTs
      # (resumes). Under drain, spot semantics force COMMIT (the caller re-wakes).
      set_parked(ctx, true)

      assert StatefulSweeper.drain_node(ctx.sweeper, "node-4") == 1

      wait_until(ctx, fn -> length(resolve_calls(ctx)) >= 1 end)
      assert [%{mode: :RESOLVE_MODE_COMMIT}] = resolve_calls(ctx)

      wait_until(ctx, fn -> match?({:ok, %{state: :banked}}, StatefulStore.get(ctx.store, "i1")) end)

      # The drain mark self-clears once the resolve completes (no leak into a later
      # non-drain checkpoint of the same workload).
      assert not MapSet.member?(:sys.get_state(ctx.sweeper).draining_workloads, "scratch-postgres")
    end

    test "instances on OTHER nodes are untouched" do
      ctx = start_stack()
      stateful_workload(ctx, "scratch-postgres", 5433)
      stateful_node(ctx, "node-4")
      serving_instance(ctx, "i1", "scratch-postgres", "vm-1", "10.98.0.5")

      # Drain a different node: nothing on node-4 banks.
      assert StatefulSweeper.drain_node(ctx.sweeper, "node-9") == 0
      flush(ctx)
      assert {:ok, %{state: :serving}} = StatefulStore.get(ctx.store, "i1")
      assert stop_calls(ctx) == []
    end
  end
end
