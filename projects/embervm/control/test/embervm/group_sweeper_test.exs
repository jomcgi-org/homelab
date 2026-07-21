defmodule Embervm.GroupSweeperTest do
  @moduledoc """
  Exercises Embervm.GroupSweeper (the R5 Task 8 lifecycle-economics loop) against a
  real GroupStore + op-log, an injected stats-scrape seam (scripted
  downstream_cx_active/total for the group entry listener), an injected bank drive +
  daemon seams, an isolated ActivatorSplices counter, and an injected clock so no
  test sleeps. Covers:

    * idle detection with the DEGRADED exclusion (a degraded group is NEVER banked),
      the zero-activator-splices clause (a live splice blocks banking even at
      cx_active==0), abort-on-recheck (a connection racing in aborts the bank);
    * max-lifetime TTL patience (an active group waits the drain window then
      destroys, tearing down members + the network);
    * banked-TTL terminal (an expired set DESTROYS the instance, warmth-only);
    * forced roll (destroys members + evicts the set + keeps the definition, so the
      next connection fresh-boots);
    * scrape-fails-open (never banks on stale stats).

  Fake processes use start_supervised! for order-independent teardown; synchronous
  assertions where possible (the bank drive is an injected synchronous fun, so no
  wait_until flake).
  """
  use ExUnit.Case, async: false

  alias Embervm.{ActivatorSplices, GroupState, GroupStore, NodeCapacity, WorkloadCatalog}
  alias Embervm.GroupSweeper
  alias Embervm.OpLog.SQLite

  defmodule FakePublisher do
    use GenServer
    def start_link(_ \\ []), do: GenServer.start_link(__MODULE__, 0)
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
    cap_table = :"grswcap_#{suffix}"
    cat_table = :"grswcat_#{suffix}"
    splices = :"grswsplices_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_groupsweeper_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, clock_agent} = Agent.start_link(fn -> 10_000 end)
    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = GroupStore.start_link(name: nil, op_log: op_log, clock: clock(clock_agent))
    pub = start_supervised!({FakePublisher, []})
    _ = start_supervised!({ActivatorSplices, [table: splices]})

    {:ok, scrape_agent} = Agent.start_link(fn -> {:ok, %{}} end)
    {:ok, bank_calls} = Agent.start_link(fn -> [] end)
    {:ok, bank_result} = Agent.start_link(fn -> :ok end)
    {:ok, stop_calls} = Agent.start_link(fn -> [] end)
    {:ok, delete_net_calls} = Agent.start_link(fn -> [] end)
    {:ok, evict_calls} = Agent.start_link(fn -> [] end)

    # The bank drive seam: records each (workload, instance_id) and, on :ok, actually
    # banks the instance in the store (mirroring what GroupManager.bank_group does) so
    # the sweeper's post-bank state matches production. The sweeper has ALREADY moved
    # the instance running -> banking before calling (the unpublish-then-bank order),
    # so this drives from :banking (bank_ready is the banking -> banked edge). On a
    # scripted {:error, _} it leaves the instance in :banking (an aborting bank returns
    # it to running via the sweeper's own bank_abort path, not here).
    bank_fun = fn workload, instance_id ->
      Agent.update(bank_calls, &[{workload, instance_id} | &1])

      case Agent.get(bank_result, & &1) do
        :ok ->
          members = GroupStore.members(store, instance_id)
          set_members = for m <- members, do: %{name: m.member_name, snapshot_ref: "snap-#{m.member_name}"}
          {:ok, _} = GroupStore.bank_ready(store, instance_id, "set-#{instance_id}", set_members)
          {:ok, %{set_id: "set-#{instance_id}", banked: length(set_members), pause_spread_ms: 5}}

        {:error, _} = err ->
          # A failed member bank: the manager would have marked banking -> running
          # (bank_abort). Mirror that so the sweeper's re-publish sees a running group.
          {:ok, _} = GroupStore.mark(store, instance_id, :bank_abort)
          err
      end
    end

    stop_group_member_fun = fn _ch, req ->
      Agent.update(stop_calls, &[req | &1])
      {:ok, %Embervm.Node.V1.StopGroupMemberResponse{}}
    end

    delete_group_network_fun = fn _ch, req ->
      Agent.update(delete_net_calls, &[req | &1])
      {:ok, %Embervm.Node.V1.DeleteGroupNetworkResponse{}}
    end

    evict_snapshot_fun = fn _ch, req ->
      Agent.update(evict_calls, &[req | &1])
      {:ok, %Embervm.Node.V1.EvictSnapshotResponse{}}
    end

    # The status.group writer seam (Task 9): records every {namespace, name,
    # status_map} the sweep patches, so a test asserts the debounce (one write per
    # changed workload per sweep, none when unchanged). Mirrors StatefulSweeperTest.
    {:ok, status_calls} = Agent.start_link(fn -> [] end)

    status_writer = fn namespace, name, status_map ->
      Agent.update(status_calls, &[{namespace, name, status_map} | &1])
      :ok
    end

    sweeper_opts = [
      name: nil,
      store: store,
      publisher: pub,
      capacity_table: cap_table,
      catalog_table: cat_table,
      op_log: op_log,
      clock: clock(clock_agent),
      scrape_fun: fn _url -> Agent.get(scrape_agent, & &1) end,
      stats_base: Keyword.get(opts, :stats_base, "http://serving:9902"),
      splices_table: splices,
      bank_fun: bank_fun,
      channel_fun: Keyword.get(opts, :channel_fun, fn _node -> {:ok, :ch} end),
      stop_group_member_fun: stop_group_member_fun,
      delete_group_network_fun: delete_group_network_fun,
      evict_snapshot_fun: evict_snapshot_fun,
      status_writer: status_writer,
      lifetime_drain_max_ms: Keyword.get(opts, :lifetime_drain_max_ms, 3_600_000),
      sweep_interval_ms: 0
    ]

    {:ok, sweeper} = GroupSweeper.start_link(sweeper_opts)

    %{
      sweeper: sweeper,
      store: store,
      op_log: op_log,
      pub: pub,
      cap_table: cap_table,
      cat_table: cat_table,
      splices: splices,
      clock_agent: clock_agent,
      scrape_agent: scrape_agent,
      bank_calls: bank_calls,
      bank_result: bank_result,
      stop_calls: stop_calls,
      delete_net_calls: delete_net_calls,
      evict_calls: evict_calls,
      status_calls: status_calls
    }
  end

  defp set_scrape(ctx, reading), do: Agent.update(ctx.scrape_agent, fn _ -> reading end)
  defp status_writes(ctx), do: Agent.get(ctx.status_calls, &Enum.reverse(&1))
  defp bank_calls(ctx), do: Agent.get(ctx.bank_calls, &Enum.reverse(&1))
  defp stop_calls(ctx), do: Agent.get(ctx.stop_calls, &Enum.reverse(&1))
  defp delete_net_calls(ctx), do: Agent.get(ctx.delete_net_calls, &Enum.reverse(&1))
  defp evict_calls(ctx), do: Agent.get(ctx.evict_calls, &Enum.reverse(&1))

  defp reading(prefix, active, total), do: {:ok, %{prefix => %{active: active, total: total}}}

  defp group_workload(ctx, name, listen_port, cfg \\ %{}) do
    group = %{
      members: [%{name: "a", start_order: 0}, %{name: "b", start_order: 1}],
      entry: %{member: "a", port: 8080, listen_port: listen_port},
      secret_ref: nil,
      idle_bank_seconds: Map.get(cfg, :idle_bank_seconds, 60),
      max_lifetime_seconds: Map.get(cfg, :max_lifetime_seconds, 86_400),
      banked_ttl_seconds: Map.get(cfg, :banked_ttl_seconds, 3_600),
      wake_timeout_seconds: 120
    }

    WorkloadCatalog.upsert(ctx.cat_table, name, %{class: "composite", namespace: "embervm", group: group})
  end

  defp group_node(ctx, node_id) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.98.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      group_member_vms: [],
      group_bundle_sets: []
    })
  end

  # A running group instance with two live members, entry published.
  defp running_group(ctx, id, workload, opts \\ []) do
    {:ok, _} =
      GroupStore.create(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "system:group:#{workload}",
        workload: workload,
        node_id: Keyword.get(opts, :node_id, "node-4"),
        subnet_cidr: "10.101.0.0/24",
        entry_member: "a",
        entry_port: 8080,
        listen_port: Keyword.get(opts, :listen_port, 5410),
        secret: "sekret"
      })

    {:ok, _} = GroupStore.member_started(ctx.store, id, %{member_name: "a", member_index: 0, vm_id: "vm-#{id}-a", ip: "10.101.0.10"})
    {:ok, _} = GroupStore.member_started(ctx.store, id, %{member_name: "b", member_index: 1, vm_id: "vm-#{id}-b", ip: "10.101.0.11"})
    {:ok, _} = GroupStore.publish(ctx.store, id, "10.244.0.5", 30_010)
    id
  end

  # Drive a running group to banked directly (bypassing the sweeper), for banked-TTL /
  # lifetime tests that need a pre-existing banked set.
  defp banked_group(ctx, id, workload) do
    _ = running_group(ctx, id, workload)
    {:ok, _} = GroupStore.mark(ctx.store, id, :bank)

    members = [%{name: "a", snapshot_ref: "snap-a"}, %{name: "b", snapshot_ref: "snap-b"}]
    {:ok, _} = GroupStore.bank_ready(ctx.store, id, "set-#{id}", members)
    id
  end

  defp load_ops(ctx, kind) do
    atom = String.to_existing_atom(kind)
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    Enum.filter(ops, &(&1.kind == atom))
  end

  # -- idle detection ---------------------------------------------------------

  test "cx_active==0, flat cx_total delta, no live splice banks the group past idleBankSeconds" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 60})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    # Tick 1: baseline (no prior reading).
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)
    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")

    # Tick 2: idle confirmed (idle_since set this tick); cannot bank on the same tick.
    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)
    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")

    # Tick 3 (65s after idle_since, past idleBankSeconds=60): banks.
    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :banked, set_id: set_id}} = GroupStore.get(ctx.store, "gi-1")
    assert set_id == "set-gi-1"
    assert [{"grp-a", "gi-1"}] = bank_calls(ctx)
    assert length(load_ops(ctx, "group_banked")) == 1
  end

  test "a DEGRADED group is NEVER banked (decision 11), the exclusion is left running" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 60})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    # Member b falls unhealthy: the group stays running but carries the degraded flag.
    {:ok, _} = GroupStore.set_member_health(ctx.store, "gi-1", "b", false)
    assert {true, "b"} = GroupStore.degraded?(ctx.store, "grp-a")

    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    # Well past idleBankSeconds: a NON-degraded group would bank here, but this one
    # stays running and no bank was driven.
    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :running, degraded_member: "b"}} = GroupStore.get(ctx.store, "gi-1")
    assert bank_calls(ctx) == []
  end

  test "a live activator splice blocks banking even at cx_active==0 (zero-splice clause)" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 60})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    # A session spliced during a wake: invisible to Envoy's entry cx_active, but a
    # live splice the sweeper counts as activity.
    ActivatorSplices.incr(ctx.splices, "grp-a")

    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    # Never banks while the splice is live (the idle clock never even arms).
    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")
    assert bank_calls(ctx) == []

    # The splice ends: now the group can idle-bank.
    ActivatorSplices.decr(ctx.splices, "grp-a")

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :banked}} = GroupStore.get(ctx.store, "gi-1")
  end

  test "a scrape failure fails open: no idle-bank against stale stats" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 60})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 300_000)
    set_scrape(ctx, {:error, :timeout})
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")
    assert bank_calls(ctx) == []
  end

  # -- abort-on-recheck -------------------------------------------------------

  test "a connection racing in at the recheck ABORTS the bank (never driven, group stays running)" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 60})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    # Tick 3: the tick-start scan reads cx_active==0 (bank begins), but the recheck's
    # fresh re-scrape reads cx_active==1 (a connection raced in). An Agent-backed call
    # counter distinguishes the two scrape calls deterministically.
    {:ok, call_count} = Agent.start_link(fn -> 0 end)

    scrape_fun = fn _url ->
      n = Agent.get_and_update(call_count, fn n -> {n, n + 1} end)
      if n == 0, do: reading("group-5410", 0, 3), else: reading("group-5410", 1, 3)
    end

    :sys.replace_state(ctx.sweeper, fn s -> %{s | scrape_fun: scrape_fun} end)

    advance(ctx.clock_agent, 65_000)
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")
    assert bank_calls(ctx) == []
    assert load_ops(ctx, "group_banked") == []
  end

  # -- max-lifetime TTL patience ----------------------------------------------

  test "an idle over-lifetime group destroys immediately (members + network torn down)" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{max_lifetime_seconds: 100})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 0, 0))
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :destroyed, terminal_reason: "lifetime"}} = GroupStore.get(ctx.store, "gi-1")

    destroys = Enum.filter(stop_calls(ctx), &(&1.mode == :STOP_GROUP_MEMBER_MODE_DESTROY))
    assert length(destroys) == 2, "both member VMs destroyed"
    assert [%{group_instance_id: "gi-1"}] = delete_net_calls(ctx)
  end

  test "an active over-lifetime group waits the drain patience window, then destroys anyway" do
    ctx = start_stack(lifetime_drain_max_ms: 100_000)
    group_workload(ctx, "grp-a", 5410, %{max_lifetime_seconds: 100})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    # Over lifetime AND actively connected: must NOT destroy yet.
    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 1, 0))
    GroupSweeper.sweep(ctx.sweeper)
    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")

    # Still active, short of the patience window: still alive.
    advance(ctx.clock_agent, 50_000)
    set_scrape(ctx, reading("group-5410", 1, 0))
    GroupSweeper.sweep(ctx.sweeper)
    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")

    # Past the patience window: destroyed anyway, even though still active.
    advance(ctx.clock_agent, 60_000)
    set_scrape(ctx, reading("group-5410", 1, 0))
    GroupSweeper.sweep(ctx.sweeper)
    assert {:ok, %{state: :destroyed, terminal_reason: "lifetime"}} = GroupStore.get(ctx.store, "gi-1")
  end

  # -- banked-TTL terminal (warmth-only) --------------------------------------

  test "a banked set untouched past bankedTtlSeconds DESTROYS the instance (warmth-only terminal)" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{banked_ttl_seconds: 100, max_lifetime_seconds: 1_000_000})
    group_node(ctx, "node-4")
    banked_group(ctx, "gi-1", "grp-a")

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 0, 0))
    GroupSweeper.sweep(ctx.sweeper)

    # An expired set IS the instance's end (no volume floor): the instance is destroyed
    # (reason expired), and each member's bundle is evicted.
    assert {:ok, %{state: :destroyed, terminal_reason: "expired"}} = GroupStore.get(ctx.store, "gi-1")
    evicted_refs = Enum.map(evict_calls(ctx), & &1.snapshot_ref) |> Enum.sort()
    assert evicted_refs == ["snap-a", "snap-b"]
  end

  test "a banked over-lifetime set is destroyed immediately (reason expired)" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{max_lifetime_seconds: 100})
    group_node(ctx, "node-4")
    banked_group(ctx, "gi-1", "grp-a")

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 0, 0))
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :destroyed, terminal_reason: "expired"}} = GroupStore.get(ctx.store, "gi-1")
  end

  test "a banked set within bankedTtlSeconds is kept" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{banked_ttl_seconds: 1_000_000, max_lifetime_seconds: 1_000_000})
    group_node(ctx, "node-4")
    banked_group(ctx, "gi-1", "grp-a")

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 0, 0))
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :banked}} = GroupStore.get(ctx.store, "gi-1")
  end

  # -- forced roll ------------------------------------------------------------

  test "forced roll destroys the live group + deletes the network, keeps the definition" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410)
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    result = GroupSweeper.force_roll(ctx.sweeper, "grp-a")
    assert result == %{destroyed: 1, evicted: 0}

    assert {:ok, %{state: :destroyed, terminal_reason: "forced_roll"}} = GroupStore.get(ctx.store, "gi-1")

    destroys = Enum.filter(stop_calls(ctx), &(&1.mode == :STOP_GROUP_MEMBER_MODE_DESTROY))
    assert length(destroys) == 2
    assert [%{group_instance_id: "gi-1"}] = delete_net_calls(ctx)

    # The DEFINITION is kept: the catalog entry survives, so the next connection
    # fresh-boots.
    assert {:ok, %{class: "composite"}} = WorkloadCatalog.fetch(ctx.cat_table, "grp-a")
  end

  test "forced roll evicts a banked set (keeps the definition, next connect fresh-boots)" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410)
    group_node(ctx, "node-4")
    banked_group(ctx, "gi-1", "grp-a")

    result = GroupSweeper.force_roll(ctx.sweeper, "grp-a")
    assert result == %{destroyed: 0, evicted: 1}

    assert {:ok, %{state: :destroyed, terminal_reason: "forced_roll"}} = GroupStore.get(ctx.store, "gi-1")
    evicted_refs = Enum.map(evict_calls(ctx), & &1.snapshot_ref) |> Enum.sort()
    assert evicted_refs == ["snap-a", "snap-b"]
    assert {:ok, %{class: "composite"}} = WorkloadCatalog.fetch(ctx.cat_table, "grp-a")
  end

  test "forced roll on a workload with no instances is a clean 0/0 (never a 404)" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410)
    group_node(ctx, "node-4")

    assert %{destroyed: 0, evicted: 0} = GroupSweeper.force_roll(ctx.sweeper, "grp-a")
  end

  # -- bank abort returns the group to the fan-out ----------------------------

  test "a bank drive failure leaves the group live and re-publishes" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 60})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    Agent.update(ctx.bank_result, fn _ -> {:error, :bank_partial} end)

    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    advance(ctx.clock_agent, 65_000)
    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    # The bank was driven but failed: the group is still running (the fake bank_fun
    # did not transition it), and the sweeper re-published.
    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")
    assert [{"grp-a", "gi-1"}] = bank_calls(ctx)
  end

  # -- usage: per-member live-seconds (decision 9) ----------------------------

  test "a live group bills per-member live-seconds each tick (group_stats), last_active_at touched" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 1_000_000})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    # Tick 1 baselines the charge clock (bills nothing yet).
    set_scrape(ctx, reading("group-5410", 1, 3))
    GroupSweeper.sweep(ctx.sweeper)
    assert load_ops(ctx, "group_stats") == []

    # Tick 2, 30s later: bills the 30s window x 2 members' compute, and touches
    # last_active_at (the entry listener showed an open connection).
    advance(ctx.clock_agent, 30_000)
    set_scrape(ctx, reading("group-5410", 1, 5))
    GroupSweeper.sweep(ctx.sweeper)

    stats = load_ops(ctx, "group_stats")
    assert length(stats) == 1
    [op] = stats
    # 2 members, default 1 vcpu each, 30s window -> 60 vcpu-seconds.
    assert op.payload["usage"]["vcpu_seconds"] == 60.0

    {:ok, inst} = GroupStore.get(ctx.store, "gi-1")
    assert inst.last_active_at != nil
  end

  # -- raw stats parse --------------------------------------------------------

  test "parse_stats keeps both group- and state- prefixed tcp cx counters" do
    body =
      Jason.encode!(%{
        "stats" => [
          %{"name" => "tcp.group-5410.downstream_cx_active", "value" => 1},
          %{"name" => "tcp.group-5410.downstream_cx_total", "value" => 9},
          %{"name" => "cluster.serve|x.upstream_rq_total", "value" => 5}
        ]
      })

    assert {:ok, %{"group-5410" => %{active: 1, total: 9}}} = GroupSweeper.parse_stats(body)
  end

  # -- status.group (Task 9) --------------------------------------------------

  test "the sweep writes status.group {state,members{live,degraded},setId,subnetCidr} + groupSummary" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 60})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    # A single non-idle tick: no bank, but a status.group write for the running group.
    set_scrape(ctx, reading("group-5410", 1, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert [{"embervm", "grp-a", status_map}] = status_writes(ctx)
    assert status_map["group"]["state"] == "running"
    assert status_map["group"]["members"] == %{"live" => 2, "degraded" => 0}
    assert status_map["group"]["subnetCidr"] == "10.101.0.0/24"
    # A running group holds no banked set, so setId is "" (never nil).
    assert status_map["group"]["setId"] == ""
    assert status_map["groupSummary"] == "running 2/0"
  end

  test "status.group is debounced: unchanged workload is not re-written next tick" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 600})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    set_scrape(ctx, reading("group-5410", 1, 3))
    GroupSweeper.sweep(ctx.sweeper)
    assert length(status_writes(ctx)) == 1

    # Second tick, same facts (still running, still active): no new status write.
    advance(ctx.clock_agent, 5_000)
    set_scrape(ctx, reading("group-5410", 1, 4))
    GroupSweeper.sweep(ctx.sweeper)
    assert length(status_writes(ctx)) == 1
  end

  test "status.group reports degraded state + a degraded member count when a member is unhealthy" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 600})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")
    # Flip member b unhealthy: the running group is now degraded, one live, one degraded.
    {:ok, _} = GroupStore.set_member_health(ctx.store, "gi-1", "b", false)

    set_scrape(ctx, reading("group-5410", 1, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert [{"embervm", "grp-a", status_map}] = status_writes(ctx)
    assert status_map["group"]["state"] == "degraded"
    assert status_map["group"]["members"] == %{"live" => 1, "degraded" => 1}
    assert status_map["groupSummary"] == "degraded 1/1"
  end

  test "status.group carries the banked set_id and zero live members once banked" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 600})
    group_node(ctx, "node-4")
    banked_group(ctx, "gi-1", "grp-a")

    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert [{"embervm", "grp-a", status_map}] = status_writes(ctx)
    assert status_map["group"]["state"] == "banked"
    assert status_map["group"]["setId"] == "set-gi-1"
    # A banked instance cleared its live member vm_ids, so live/degraded are both 0.
    assert status_map["group"]["members"] == %{"live" => 0, "degraded" => 0}
  end

  test "orphan network GC: a node-reported network for a TERMINAL instance is deleted" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 600})
    group_node(ctx, "node-4")
    id = running_group(ctx, "gi-orphan", "grp-a")

    # The instance goes terminal WITHOUT a network teardown (the failed-bank /
    # dead-channel shapes), while the node still reports its group network.
    {:ok, _} = GroupStore.transition(ctx.store, id, :destroy, :group_destroyed, %{reason: "test"}, %{})

    NodeCapacity.put(ctx.cap_table, "node-4", %{
      configured_id: "node-4",
      node_id: "node-4",
      serving_subnet_cidr: "10.98.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      group_member_vms: [],
      group_bundle_sets: [],
      group_networks: [%{group_instance_id: id, cidr: "10.101.0.0/24", bridge: "emg1", member_count: 0}]
    })

    set_scrape(ctx, reading("group-5410", 0, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert [req] = delete_net_calls(ctx)
    assert req.group_instance_id == id
  end

  test "orphan network GC: a LIVE instance's network is never touched" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 600})
    group_node(ctx, "node-4")
    id = running_group(ctx, "gi-live", "grp-a")

    NodeCapacity.put(ctx.cap_table, "node-4", %{
      configured_id: "node-4",
      node_id: "node-4",
      serving_subnet_cidr: "10.98.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      group_member_vms: [],
      group_bundle_sets: [],
      group_networks: [%{group_instance_id: id, cidr: "10.101.0.0/24", bridge: "emg1", member_count: 2}]
    })

    set_scrape(ctx, reading("group-5410", 1, 3))
    GroupSweeper.sweep(ctx.sweeper)

    assert delete_net_calls(ctx) == []
  end

  test "a status-write failure never crashes the sweep" do
    ctx = start_stack()
    group_workload(ctx, "grp-a", 5410, %{idle_bank_seconds: 600})
    group_node(ctx, "node-4")
    running_group(ctx, "gi-1", "grp-a")

    :sys.replace_state(ctx.sweeper, fn s -> %{s | status_writer: fn _ns, _n, _m -> raise "boom" end} end)

    set_scrape(ctx, reading("group-5410", 1, 3))
    assert :ok = GroupSweeper.sweep(ctx.sweeper)
    assert {:ok, %{state: :running}} = GroupStore.get(ctx.store, "gi-1")
  end

  # -- instance-key unification (PR-B0b) ---------------------------------------

  # A per-instance capacity fact carrying the per-instance group inventory the
  # sweeper's dial resolution reads (keyed on group_instance_id).
  defp group_brick(ctx, node_id, pod_uid, opts) do
    NodeCapacity.put(ctx.cap_table, {node_id, pod_uid}, %{
      configured_id: node_id,
      node_id: node_id,
      pod_uid: pod_uid,
      instance_id: "#{node_id}/#{pod_uid}",
      serving_subnet_cidr: "10.98.0.0/24",
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      group_member_vms: Keyword.get(opts, :group_member_vms, []),
      group_bundle_sets: Keyword.get(opts, :group_bundle_sets, [])
    })
  end

  test "group teardown dials the OWNER instance_id even when the node-name alias points at a sibling" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    capture_channel = fn key ->
      Agent.update(dialed, &[key | &1])
      {:ok, :ch}
    end

    ctx = start_stack(channel_fun: capture_channel)
    group_workload(ctx, "grp-a", 5410, %{max_lifetime_seconds: 100})

    # Two co-located instances on node-4. pod-owner reports the live group gi-1;
    # pod-sibling does not (the last registrant the node alias would collapse to). The
    # destroy (StopGroupMember + DeleteGroupNetwork) must dial pod-owner.
    group_brick(ctx, "node-4", "pod-sibling", group_member_vms: [])

    group_brick(ctx, "node-4", "pod-owner",
      group_member_vms: [
        %{vm_id: "vm-gi-1-a", group_instance_id: "gi-1", member_name: "a", ip: "10.101.0.10", healthy: true},
        %{vm_id: "vm-gi-1-b", group_instance_id: "gi-1", member_name: "b", ip: "10.101.0.11", healthy: true}
      ]
    )

    running_group(ctx, "gi-1", "grp-a")

    advance(ctx.clock_agent, 200_000)
    set_scrape(ctx, reading("group-5410", 0, 0))
    GroupSweeper.sweep(ctx.sweeper)

    assert {:ok, %{state: :destroyed}} = GroupStore.get(ctx.store, "gi-1")

    keys = Agent.get(dialed, & &1)
    assert "node-4/pod-owner" in keys
    refute "node-4" in keys
    refute "node-4/pod-sibling" in keys
  end
end
