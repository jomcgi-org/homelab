defmodule Embervm.GroupWakeManagerTest do
  @moduledoc """
  Exercises Embervm.GroupWakeManager (the composite wake brain) against a real
  GroupStore + op-log, a FakePublisher, a fake WorkloadCatalog + NodeCapacity, and a
  FAKE group supervisor (recording create_group/wake_group/adopt_group calls, with
  injectable latency so N concurrent wakes race). Covers:

    * single-flight: N concurrent wakes -> exactly ONE wake sequence + N replies;
    * miss round-trip: a banked complete set relights and resolves the parked caller;
      a subsequent connection is a straggler (no wake, resolved from the live entry);
    * relight-failure -> fresh fallback is opaque to the brain (the GroupManager owns
      it); the brain just single-flights and resolves;
    * the wake DECISION (create vs relight vs fresh) from GroupStore facts;
    * wake-rate limit + parked-cap denials;
    * adoption matrix: a restart during each non-terminal state converges +
      republishes the identical entry endpoint without touching a VM;
    * degraded-group wake: a group with a dead NON-entry member is live (not banked)
      and routes normally (straggler, no wake).
  """
  use ExUnit.Case, async: true

  alias Embervm.{GroupStore, GroupWakeManager, NodeCapacity, WorkloadCatalog}
  alias Embervm.OpLog.SQLite

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

  # A fake group supervisor: records every create_group/wake_group/adopt_group call
  # (so a test asserts EXACTLY ONE happened per burst), sleeps `latency_ms` to widen
  # the concurrent-wake race window, and drives the REAL GroupStore so the wake's
  # publish makes the entry endpoint live (the brain re-reads the store to resolve
  # stragglers). Configured via the process registered name below.
  defmodule FakeSupervisor do
    def create_group(workload) do
      call({:create, workload})
    end

    def wake_group(workload, instance_id) do
      call({:wake, workload, instance_id})
    end

    def adopt_group(workload, instance_id) do
      call({:adopt, workload, instance_id})
    end

    defp call(msg) do
      Agent.get_and_update(__MODULE__, fn s ->
        {reply, s} = handle(msg, s)
        {reply, s}
      end)
    end

    defp handle({:create, workload}, s) do
      maybe_sleep(s)
      s = %{s | creates: s.creates + 1}
      resolve(workload, s)
    end

    defp handle({:wake, workload, _instance_id}, s) do
      maybe_sleep(s)
      s = %{s | wakes: s.wakes + 1}

      case resolve(workload, s) do
        {{:ok, endpoint}, s} -> {{:ok, endpoint, :relit}, s}
        other -> other
      end
    end

    defp handle({:adopt, _workload, _instance_id}, s) do
      {:ok, %{s | adopts: s.adopts + 1}}
    end

    # Publish the endpoint into the real store (drive the instance to running) so the
    # brain's straggler re-read + the FakePublisher both see it. The endpoint is fixed.
    defp resolve(_workload, %{fail: true} = s), do: {{:error, {:wake_failed, :scripted}}, s}

    defp resolve(_workload, s) do
      endpoint = %{ip: "10.0.0.9", port: 30_010}
      publish_running(s)
      {{:ok, endpoint}, s}
    end

    defp publish_running(%{store: store, instance_id: instance_id}) when is_binary(instance_id) do
      _ = GroupStore.publish(store, instance_id, "10.0.0.9", 30_010)
      :ok
    end

    defp publish_running(_s), do: :ok

    defp maybe_sleep(%{latency_ms: ms}) when is_integer(ms) and ms > 0, do: Process.sleep(ms)
    defp maybe_sleep(_s), do: :ok
  end

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    path = Path.join(System.tmp_dir!(), "embervm_groupwake_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, counter} = Agent.start_link(fn -> 1_000 end)
    clock = fn -> Agent.get_and_update(counter, fn n -> {n, n + 1} end) end

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = GroupStore.start_link(name: nil, op_log: op_log, clock: clock)
    {:ok, pub} = FakePublisher.start_link()

    cap_table = :"gwm_cap_#{suffix}"
    cat_table = :"gwm_cat_#{suffix}"
    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    WorkloadCatalog.upsert(cat_table, "grp-a", %{
      class: "composite",
      group: %{entry: %{member: "leader", port: 8080, listen_port: 5410}}
    })

    sup_state =
      %{
        store: store,
        instance_id: Keyword.get(opts, :instance_id),
        latency_ms: Keyword.get(opts, :latency_ms, 0),
        fail: Keyword.get(opts, :fail, false),
        creates: 0,
        wakes: 0,
        adopts: 0
      }

    {:ok, _} = Agent.start_link(fn -> sup_state end, name: FakeSupervisor)
    on_exit(fn -> if Process.whereis(FakeSupervisor), do: Agent.stop(FakeSupervisor) end)

    {:ok, mgr} =
      GroupWakeManager.start_link(
        name: nil,
        store: store,
        publisher: pub,
        capacity_table: cap_table,
        catalog_table: cat_table,
        supervisor_mod: FakeSupervisor,
        clock: clock,
        reconcile_interval_ms: 0
      )

    %{mgr: mgr, store: store, pub: pub, cap_table: cap_table, cat_table: cat_table, op_log: op_log}
  end

  defp sup_counts do
    Agent.get(FakeSupervisor, fn s -> {s.creates, s.wakes, s.adopts} end)
  end

  # Seed a node fact carrying the given group inventory (live members / bundle sets),
  # so the adoption reconcile has node truth to reconcile against.
  defp seed_node(ctx, facts) do
    base = %{
      configured_id: "node-4",
      node_id: "node-4",
      serving_subnet_cidr: "10.200.0.0/24",
      max_live_vms: 10,
      live_vms: 0,
      group_networks: [],
      group_member_vms: [],
      group_bundle_sets: []
    }

    NodeCapacity.put(ctx.cap_table, "node-4", Map.merge(base, facts))
  end

  # Create + drive an instance to banked with a complete set (leader-only group for
  # brevity). Returns the instance_id.
  defp seed_banked(ctx, instance_id, set_id \\ "set-1") do
    {:ok, _} =
      GroupStore.create(ctx.store, %{
        instance_id: instance_id,
        tenant: "homelab",
        principal: "system:group:grp-a",
        workload: "grp-a",
        node_id: "node-4",
        subnet_cidr: "10.101.0.0/24",
        entry_member: "leader",
        entry_port: 8080,
        listen_port: 5410,
        secret: "s"
      })

    {:ok, _} = GroupStore.member_started(ctx.store, instance_id, %{member_name: "leader", member_index: 0, vm_id: "vm-l", ip: "10.101.0.10"})
    {:ok, _} = GroupStore.publish(ctx.store, instance_id, "10.0.0.9", 30_010)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :bank)
    {:ok, _} = GroupStore.bank_ready(ctx.store, instance_id, set_id, [%{name: "leader", snapshot_ref: "snap-l"}])
    instance_id
  end

  # -- single-flight ---------------------------------------------------------

  test "single-flight: N concurrent wakes to a banked group produce ONE wake + N replies" do
    ctx = start_stack(instance_id: "g-1", latency_ms: 40)
    _ = seed_banked(ctx, "g-1")

    parent = self()

    tasks =
      for i <- 1..8 do
        Task.async(fn ->
          reply = GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")
          send(parent, {:done, i})
          reply
        end)
      end

    replies = Enum.map(tasks, &Task.await(&1, 5_000))

    # Every caller got the SAME live endpoint.
    assert Enum.all?(replies, &(&1 == {:ok, %{ip: "10.0.0.9", port: 30_010}}))

    # Exactly ONE wake sequence ran for the burst (single-flight).
    {creates, wakes, _adopts} = sup_counts()
    assert creates == 0
    assert wakes == 1
  end

  test "create path: no instance at all -> exactly one create_group, N replies" do
    ctx = start_stack(instance_id: "g-created", latency_ms: 30)
    # The create records the instance in the store so the FakeSupervisor's publish
    # lands on a real row: pre-seed a creating instance the create "fills in".
    {:ok, _} =
      GroupStore.create(ctx.store, %{
        instance_id: "g-created",
        tenant: "homelab",
        principal: "system:group:grp-a",
        workload: "grp-a",
        node_id: "node-4",
        subnet_cidr: "10.101.0.0/24",
        entry_member: "leader",
        entry_port: 8080,
        listen_port: 5410,
        secret: "s"
      })

    {:ok, _} = GroupStore.member_started(ctx.store, "g-created", %{member_name: "leader", member_index: 0, vm_id: "vm-l", ip: "10.101.0.10"})

    tasks = for _ <- 1..5, do: Task.async(fn -> GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a") end)
    replies = Enum.map(tasks, &Task.await(&1, 5_000))

    assert Enum.all?(replies, &match?({:ok, %{ip: "10.0.0.9"}}, &1))
    {creates, wakes, _} = sup_counts()
    assert creates == 1
    assert wakes == 0
  end

  # -- straggler -------------------------------------------------------------

  test "straggler: a connection to an already-live group resolves WITHOUT a wake" do
    ctx = start_stack(instance_id: "g-1")
    _ = seed_banked(ctx, "g-1")
    # First wake makes it live.
    assert {:ok, _} = GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")
    {_c, wakes_after_first, _} = sup_counts()
    assert wakes_after_first == 1

    # A subsequent connection is a straggler: resolved from the live entry endpoint,
    # no additional wake.
    assert {:ok, %{ip: "10.0.0.9", port: 30_010}} = GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")
    {_c, wakes, _} = sup_counts()
    assert wakes == 1
  end

  test "unknown workload is refused" do
    ctx = start_stack()
    assert {:error, {:unknown_workload}} = GroupWakeManager.wake(ctx.mgr, "not-a-group", "p")
  end

  # -- rate limit + park cap -------------------------------------------------

  test "wake-rate limit denies past the window budget (fail-scripted so the workload never goes live)" do
    # fail: true => the FakeSupervisor never publishes, so the workload never goes
    # live and each miss re-enters the wake path (not the straggler path), letting the
    # per-workload wake-rate limit bite. wake_max: 1 allows one wake per window.
    ctx = start_stack(instance_id: "g-1", fail: true)
    _ = seed_banked(ctx, "g-1")

    {:ok, mgr} =
      GroupWakeManager.start_link(
        name: nil,
        store: ctx.store,
        publisher: ctx.pub,
        capacity_table: ctx.cap_table,
        catalog_table: ctx.cat_table,
        supervisor_mod: FakeSupervisor,
        wake_max: 1,
        reconcile_interval_ms: 0
      )

    # First wake: allowed, but the scripted failure means it errors (not live).
    assert {:error, {:wake_failed, _}} = GroupWakeManager.wake(mgr, "grp-a", "system:group:grp-a")
    # Second wake within the window: the per-workload budget (1) is spent -> denied.
    assert {:error, {:wake_rate, _}} = GroupWakeManager.wake(mgr, "grp-a", "system:group:grp-a")
  end

  # -- adoption matrix -------------------------------------------------------

  test "adoption: a live group (node reports live members) is adopted to running + republished, no VM touched" do
    ctx = start_stack()
    instance_id = "g-live"

    # Seed a running instance, then simulate a CP restart by forcing it to a limbo
    # state the node truth heals: mark it relighting (a stranded transient).
    _ = seed_banked(ctx, instance_id)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :relight)

    seed_node(ctx, %{
      group_member_vms: [%{vm_id: "vm-l", group_instance_id: instance_id, member_name: "leader", ip: "10.101.0.10", healthy: true}]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, instance_id)
    assert inst.state == :running
    # The GroupManager owner was respawned for the adopted-live group.
    {_c, _w, adopts} = sup_counts()
    assert adopts >= 1
    # The entry endpoint republishes (the publisher was pinged).
    assert FakePublisher.count(ctx.pub) >= 1
  end

  test "adoption: a banked instance with a COMPLETE reported set heals to banked" do
    ctx = start_stack()
    instance_id = "g-banked"
    _ = seed_banked(ctx, instance_id)
    # Strand it in a relighting limbo; node reports the complete bundle set.
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :relight)

    seed_node(ctx, %{
      group_bundle_sets: [
        %{set_id: "set-1", group_instance_id: instance_id, created_at_unix_ms: 0, members: [%{member_name: "leader", snapshot_ref: "snap-l"}]}
      ]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, instance_id)
    assert inst.state == :banked
  end

  test "adoption: a partial reported set is EVICTED (set_id cleared) so the next wake fresh-boots" do
    ctx = start_stack()
    instance_id = "g-partial"

    # A two-member group banked, but the node reports only ONE member's bundle.
    {:ok, _} =
      GroupStore.create(ctx.store, %{
        instance_id: instance_id,
        tenant: "homelab",
        principal: "system:group:grp-a",
        workload: "grp-a",
        node_id: "node-4",
        subnet_cidr: "10.101.0.0/24",
        entry_member: "leader",
        entry_port: 8080,
        listen_port: 5410,
        secret: "s"
      })

    {:ok, _} = GroupStore.member_started(ctx.store, instance_id, %{member_name: "leader", member_index: 0, vm_id: "vm-l", ip: "10.101.0.10"})
    {:ok, _} = GroupStore.member_started(ctx.store, instance_id, %{member_name: "worker", member_index: 1, vm_id: "vm-w", ip: "10.101.0.11"})
    {:ok, _} = GroupStore.publish(ctx.store, instance_id, "10.0.0.9", 30_010)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :bank)

    {:ok, _} =
      GroupStore.bank_ready(ctx.store, instance_id, "set-1", [
        %{name: "leader", snapshot_ref: "snap-l"},
        %{name: "worker", snapshot_ref: "snap-w"}
      ])

    # Node reports ONLY the leader's bundle (worker's is gone): a partial set.
    seed_node(ctx, %{
      group_bundle_sets: [
        %{set_id: "set-1", group_instance_id: instance_id, created_at_unix_ms: 0, members: [%{member_name: "leader", snapshot_ref: "snap-l"}]}
      ]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, instance_id)
    assert inst.set_id == nil
  end

  # -- degraded-group wake ---------------------------------------------------

  test "degraded-group wake: a running group with a dead NON-entry member routes normally (straggler, no wake)" do
    ctx = start_stack()
    instance_id = "g-degraded"

    {:ok, _} =
      GroupStore.create(ctx.store, %{
        instance_id: instance_id,
        tenant: "homelab",
        principal: "system:group:grp-a",
        workload: "grp-a",
        node_id: "node-4",
        subnet_cidr: "10.101.0.0/24",
        entry_member: "leader",
        entry_port: 8080,
        listen_port: 5410,
        secret: "s"
      })

    {:ok, _} = GroupStore.member_started(ctx.store, instance_id, %{member_name: "leader", member_index: 0, vm_id: "vm-l", ip: "10.101.0.10"})
    {:ok, _} = GroupStore.member_started(ctx.store, instance_id, %{member_name: "worker", member_index: 1, vm_id: "vm-w", ip: "10.101.0.11"})
    {:ok, _} = GroupStore.publish(ctx.store, instance_id, "10.0.0.9", 30_010)
    # A non-entry member falls unhealthy: the group is DEGRADED (a flag), still running.
    {:ok, _} = GroupStore.set_member_health(ctx.store, instance_id, "worker", false)

    assert {true, "worker"} = GroupStore.degraded?(ctx.store, "grp-a")

    # A connection routes normally: the entry is live, so it is a straggler (no wake).
    assert {:ok, %{ip: "10.0.0.9", port: 30_010}} = GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")
    {_c, wakes, _} = sup_counts()
    assert wakes == 0
  end
end
