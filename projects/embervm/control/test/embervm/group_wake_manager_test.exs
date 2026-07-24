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

    # Drive the instance to running with its published entry endpoint, exactly as a
    # real relight (banked -> relighting -> creating -> running -> publish) leaves it,
    # so the brain's straggler re-read of GroupStore.entry_endpoint/2 sees the live
    # endpoint. Uses the ETS-force adopt path (adopt_state + adopt_endpoint) to reach
    # running from banked without replaying every FSM edge in the fake.
    defp publish_running(%{store: store, instance_id: instance_id}) when is_binary(instance_id) do
      _ = GroupStore.adopt_state(store, instance_id, :running)
      _ = GroupStore.adopt_endpoint(store, instance_id, "10.0.0.9", 30_010)
      :ok
    end

    defp publish_running(_s), do: :ok

    defp maybe_sleep(%{latency_ms: ms}) when is_integer(ms) and ms > 0, do: Process.sleep(ms)
    defp maybe_sleep(_s), do: :ok
  end

  # The bound-expiry teardown seam (H-fix): records force_roll calls to a per-test
  # probe process when one is registered, so a test can assert an expired wake (or
  # a dead create found by adoption) rolled the instance. Tests within one module
  # run serially (async: true parallelizes across modules), so the registered
  # probe name cannot cross-talk.
  defmodule FakeSweeper do
    def force_roll(workload) do
      case Process.whereis(:gwm_sweeper_probe) do
        nil -> :ok
        pid -> send(pid, {:force_rolled, workload})
      end

      %{destroyed: 1, evicted: 0}
    end
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
    {:ok, stop_calls} = Agent.start_link(fn -> [] end)

    cap_table = :"gwm_cap_#{suffix}"
    cat_table = :"gwm_cat_#{suffix}"
    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    WorkloadCatalog.upsert(cat_table, "grp-a", %{
      class: "composite",
      group:
        Map.merge(
          %{entry: %{member: "leader", port: 8080, listen_port: 5410}},
          Keyword.get(opts, :group_extra, %{})
        )
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

    # Start the fake's state Agent under the ExUnit test supervisor (start_supervised),
    # NOT a bare Agent.start_link with a hand-rolled on_exit. Two robustness reasons:
    # (1) start_supervised tears the child down SYNCHRONOUSLY between tests, so the next
    # test's Agent under the same module name can never collide with a prior test's not-
    # yet-reaped one (a bare Agent.start_link linked to the test process dies
    # asynchronously when the test process exits, and a whereis-then-stop on_exit then
    # races the name being re-registered -> the "no process" exit we hit); (2) no manual
    # stop, so there is no double-stop of an already-dead name. The Agent is keyed by a
    # fixed id (:fake_supervisor) but registered under the module name the fake resolves.
    _ = start_supervised!(%{id: :fake_supervisor, start: {Agent, :start_link, [fn -> sup_state end, [name: FakeSupervisor]]}})

    stop_group_member_fun =
      Keyword.get(opts, :stop_group_member_fun, fn _channel, req ->
        Agent.update(stop_calls, &[req | &1])
        {:ok, %Embervm.Node.V1.StopGroupMemberResponse{teardown_confirmed: true}}
      end)

    mgr_opts =
      [
        name: nil,
        store: store,
        publisher: pub,
        capacity_table: cap_table,
        catalog_table: cat_table,
        supervisor_mod: FakeSupervisor,
        # The shared DNAT-derivation values (same as the live publish): a group /24
        # entry at .10 in the 10.101.0.0/16 supernet derives vm_port = 30000 + 10 =
        # 30010, published as {pod_ip 10.0.0.9, 30010} -> the endpoint the fake's
        # publish_running records AND adoption must re-derive identically.
        supernet: "10.101.0.0/16",
        port_base: 30_000,
        pod_ip: "10.0.0.9",
        clock: clock,
        channel_fun: fn _node -> {:ok, :ch} end,
        stop_group_member_fun: stop_group_member_fun,
        op_log: op_log,
        # Default the teardown seam to the no-op fake: without it an expired-wake
        # teardown would dial the real (absent) GroupSweeper and log a crash.
        sweeper_mod: FakeSweeper,
        reconcile_interval_ms: 0
      ] ++
        Keyword.take(opts, [
          :wake_bound_ms,
          :mono_clock,
          :restore_artifact_fun,
          :node_confirmed_destroy,
          :destroying_alarm_ms,
          :orphan_grace_ms
        ])

    {:ok, mgr} = GroupWakeManager.start_link(mgr_opts)

    %{
      mgr: mgr,
      store: store,
      pub: pub,
      cap_table: cap_table,
      cat_table: cat_table,
      op_log: op_log,
      stop_calls: stop_calls
    }
  end

  defp sup_counts do
    Agent.get(FakeSupervisor, fn s -> {s.creates, s.wakes, s.adopts} end)
  end

  defp stop_calls(ctx), do: Agent.get(ctx.stop_calls, &Enum.reverse(&1))

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
      group_bundle_sets: [],
      # The node advertises the group workload's base as READY (the node-shared
      # base is persistent; a restore-on-miss loses only the per-instance banked
      # bundle SET, not the base), so the cold-pick base-readiness gate
      # (Embervm.Placement.base_ready?/2) is satisfied and instance selection can
      # proceed to the restore/boot. A test wanting a not-yet-advertised instance
      # would override :workloads with an empty/absent grp-a entry.
      workloads: %{"grp-a" => %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap/grp-a"}}
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

  # -- restore-on-miss (R6, Task 8) -------------------------------------------

  test "a complete exported set whose local bundles are gone RESTORES then relights" do
    {:ok, restore_calls} = Agent.start_link(fn -> [] end)

    restore_fun = fn _ch, req ->
      art = req.artifact
      Agent.update(restore_calls, &[%{kind: art.kind, ref: art.ref, workload: art.workload} | &1])
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{bytes_moved: 8192, skipped: false}}
    end

    ctx = start_stack(instance_id: "g-restore", restore_artifact_fun: restore_fun)
    _ = seed_banked(ctx, "g-restore", "set-r")

    # The node reports NO local bundle set for this group (a true local miss: the
    # disk lost it) but a reachable store. Optimistic restore-on-miss then restores
    # the whole GROUP_SET before the delegated relight.
    seed_node(ctx, %{
      group_bundle_sets: [],
      store_reachable: true
    })

    assert {:ok, %{ip: "10.0.0.9", port: 30_010}} = GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")

    # RestoreArtifact was issued for the whole set, and the delegated relight ran.
    assert [%{kind: :ARTIFACT_KIND_GROUP_SET, ref: "set-r", workload: "grp-a"}] = Agent.get(restore_calls, & &1)
    {_creates, wakes, _adopts} = sup_counts()
    assert wakes == 1

    # The restore is auditable from the op-log alone.
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    assert Enum.any?(ops, &(&1.kind == :artifact_restored and &1.payload["ref"] == "set-r"))
  end

  test "an unreachable store on a set miss attempts no restore and relights (fresh-fallback) as before" do
    {:ok, restore_calls} = Agent.start_link(fn -> [] end)
    restore_fun = fn _ch, _req -> Agent.update(restore_calls, &[:called | &1]) && {:error, :unused} end

    ctx = start_stack(instance_id: "g-nostore", restore_artifact_fun: restore_fun)
    _ = seed_banked(ctx, "g-nostore", "set-n")

    seed_node(ctx, %{
      group_bundle_sets: [],
      store_reachable: false
    })

    assert {:ok, %{ip: "10.0.0.9", port: 30_010}} = GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")
    assert Agent.get(restore_calls, & &1) == []
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
    # The DNAT entry endpoint was RE-DERIVED from the entry member's node-reported ip
    # (.10 -> 30000 + 10) and forced back, so the republished endpoint is IDENTICAL to
    # the pre-restart live publish {pod_ip 10.0.0.9, 30010} (NOT the fallback {tap ip,
    # guest port}). This is the "republish identical snapshot without touching a VM" bar.
    assert %{ip: "10.0.0.9", port: 30_010} = GroupStore.entry_endpoint(ctx.store, "grp-a")
    assert inst.entry_ip == "10.0.0.9"
    assert inst.entry_port_published == 30_010
    # The GroupManager owner was respawned for the adopted-live group.
    {_c, _w, adopts} = sup_counts()
    assert adopts >= 1
    # The entry endpoint republishes (the publisher was pinged).
    assert FakePublisher.count(ctx.pub) >= 1
  end

  test "adoption: a node-relit banked group is adopted op-free without orphan destroy" do
    ctx = start_stack()
    instance_id = "g-activator-relit"
    _ = seed_banked(ctx, instance_id)

    # The brick relit the complete local set while the control plane was absent.
    # The returning CP adopts node truth only: no lifecycle backfill op exists for
    # composites because they are warmth-only.
    seed_node(ctx, %{
      group_member_vms: [
        %{
          vm_id: "vm-relit",
          group_instance_id: instance_id,
          member_name: "leader",
          ip: "10.101.0.10",
          healthy: true,
          origin: :INSTANCE_ORIGIN_ACTIVATOR
        }
      ]
    })

    {:ok, ops_before} = SQLite.read_from(ctx.op_log, 0)

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, instance_id)
    assert inst.state == :running
    assert %{ip: "10.0.0.9", port: 30_010} = GroupStore.entry_endpoint(ctx.store, "grp-a")
    assert inst.entry_ip == "10.0.0.9"
    assert inst.entry_port_published == 30_010
    assert {0, 0, 1} = sup_counts()
    assert stop_calls(ctx) == []

    {:ok, ops_after} = SQLite.read_from(ctx.op_log, 0)
    assert ops_after == ops_before
  end

  test "adoption SKIPS a :destroying group even though the node still reports live members" do
    ctx = start_stack()
    instance_id = "g-destroying"

    # Seed an instance, then force it into :destroying: mid node-confirmed teardown
    # (ADR embervm/014 decision 5), the per-member teardown RPCs in flight.
    _ = seed_banked(ctx, instance_id)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :begin_destroy)

    # The node still reports the group's members live: the straggler report that turns
    # on the TLC NoDestroyBeforeConfirm violation if adoption keys off the node.
    seed_node(ctx, %{
      group_member_vms: [%{vm_id: "vm-l", group_instance_id: instance_id, member_name: "leader", ip: "10.101.0.10", healthy: true}]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    # NOT re-adopted to :running; stays destroying for the (gated) redrive to own.
    {:ok, inst} = GroupStore.get(ctx.store, instance_id)
    assert inst.state == :destroying
  end

  test "gated reconcile re-drives a destroying group and records it destroyed" do
    ctx = start_stack(node_confirmed_destroy: true)
    instance_id = "g-redrive"

    _ = seed_banked(ctx, instance_id)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :begin_destroy)

    seed_node(ctx, %{
      group_member_vms: [
        %{
          vm_id: "vm-l",
          group_instance_id: instance_id,
          member_name: "leader",
          ip: "10.101.0.10",
          healthy: true
        }
      ]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    assert {:ok, %{state: :destroyed}} = GroupStore.get(ctx.store, instance_id)

    assert [
             %{
               vm_id: "vm-l",
               member_name: "leader",
               mode: :STOP_GROUP_MEMBER_MODE_DESTROY
             }
           ] = stop_calls(ctx)
  end

  test "gated reconcile confirms a destroying group by owner-reported absence" do
    ctx = start_stack(node_confirmed_destroy: true)
    instance_id = "g-absent"

    _ = seed_banked(ctx, instance_id)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :begin_destroy)
    seed_node(ctx, %{})

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    assert {:ok, %{state: :destroyed}} = GroupStore.get(ctx.store, instance_id)
    assert stop_calls(ctx) == []
  end

  test "gated reconcile leaves a destroying group when its owner is not reporting" do
    ctx = start_stack(node_confirmed_destroy: true)
    instance_id = "g-disconnected"

    _ = seed_banked(ctx, instance_id)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :begin_destroy)

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    assert {:ok, %{state: :destroying}} = GroupStore.get(ctx.store, instance_id)
    assert stop_calls(ctx) == []
  end

  test "gated reconcile destroys a node-reported group member with no CP row" do
    ctx = start_stack(node_confirmed_destroy: true)

    seed_node(ctx, %{
      group_member_vms: [
        %{
          vm_id: "vm-orphan",
          group_instance_id: "g-missing",
          member_name: "worker-0",
          ip: "10.101.0.11",
          healthy: true
        }
      ]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    assert [
             %{
               vm_id: "vm-orphan",
               member_name: "worker-0",
               mode: :STOP_GROUP_MEMBER_MODE_DESTROY
             }
           ] = stop_calls(ctx)
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

  # -- wake-worker bound + adoption self-recovery (Task 10) ------------------

  test "a never-ready member fails the group wake at the bound, releasing single-flight" do
    # The fake wake_group blocks for 3s (a wedged member boot that never opens its
    # port, the R5 `:infinity`-chain symptom); the wake WORKER is bounded to 40ms, so
    # {:wake_timeout} fails the wake before the worker ever reports. The parked caller
    # gets a wake_failed error (NOT a forever-hang), and single-flight releases so a
    # later connection can retry.
    ctx = start_stack(instance_id: "g-1", latency_ms: 3_000, fail: true, wake_bound_ms: 40)
    _ = seed_banked(ctx, "g-1")

    assert {:error, {:wake_failed, :wake_timeout}} =
             GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")

    # Single-flight released: the banked instance is still re-wakeable (the fake never
    # published, so it stayed :banked), and a reconcile leaves it banked (re-wakeable),
    # not skipped forever.
    :ok = GroupWakeManager.reconcile(ctx.mgr)
    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :banked
  end

  test "a bound-expired wake tears its instance down via the sweeper (H-fix)" do
    # A wake that expires at the bound must force-roll the workload's instance:
    # without it a bound-expired CREATE leaves a live-forever :creating instance and
    # every later wake bounces off :already_live (the permanent Gate-1 wedge).
    Process.register(self(), :gwm_sweeper_probe)
    on_exit(fn -> if Process.whereis(:gwm_sweeper_probe) == self(), do: Process.unregister(:gwm_sweeper_probe) end)

    ctx = start_stack(instance_id: "g-1", latency_ms: 3_000, fail: true, wake_bound_ms: 40)
    _ = seed_banked(ctx, "g-1")

    assert {:error, {:wake_failed, :wake_timeout}} =
             GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a")

    assert_receive {:force_rolled, "grp-a"}, 1_000
  end

  test "adoption leaves a :banking instance alone (the sweeper owns the bank)" do
    # Mid-bank the node still reports live members; without the banking skip the
    # adopt_live branch force-flips banking -> running and the sweeper's
    # bank_ready record dies on {:illegal_transition, :running, :bank_ready}
    # (the 2026-07-19 bank_record_failed wedge).
    ctx = start_stack()
    instance_id = "g-banking"
    _ = seed_banked(ctx, instance_id)
    # Back to running, then into :banking (running -> banking), the mid-bank state.
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :relight)
    {:ok, _} = GroupStore.transition(ctx.store, instance_id, :relight_ready, :group_relit, %{}, %{})
    {:ok, _} = GroupStore.publish(ctx.store, instance_id, "10.0.0.9", 30_010)
    {:ok, _} = GroupStore.mark(ctx.store, instance_id, :bank)
    assert {:ok, %{state: :banking}} = GroupStore.get(ctx.store, instance_id)

    seed_node(ctx, %{
      group_member_vms: [%{vm_id: "vm-l", group_instance_id: instance_id, member_name: "leader", ip: "10.101.0.10", healthy: true}]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    {:ok, inst} = GroupStore.get(ctx.store, instance_id)
    assert inst.state == :banking
  end

  test "adoption rolls a dead create (:creating with no in-flight wake) terminal" do
    # A :creating instance with no wake in flight is a dead create (a CP restart
    # mid-create, or a lost bound timer): it can never finish, and adopting its
    # partial member set to running would publish a partial group. Adoption must
    # roll it via the sweeper instead of adopt_live-ing it.
    Process.register(self(), :gwm_sweeper_probe)
    on_exit(fn -> if Process.whereis(:gwm_sweeper_probe) == self(), do: Process.unregister(:gwm_sweeper_probe) end)

    ctx = start_stack(instance_id: nil)

    {:ok, _} =
      GroupStore.create(ctx.store, %{
        instance_id: "g-dead",
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

    # One live member reported by the node (the 1-of-3 zombie shape): without the
    # dead-create branch this instance would be adopt_live'd to running.
    seed_node(ctx, %{
      group_member_vms: [
        %{group_instance_id: "g-dead", member_name: "leader", vm_id: "vm-l", ip: "10.101.0.10", healthy: true}
      ]
    })

    :ok = GroupWakeManager.reconcile(ctx.mgr)

    assert_receive {:force_rolled, "grp-a"}, 1_000
    # The instance was NOT adopted to running (the sweeper owns its teardown).
    {:ok, inst} = GroupStore.get(ctx.store, "g-dead")
    refute inst.state == :running
  end

  test "adoption recovers a workload stuck waking past 2 * wakeTimeoutSeconds" do
    # Directly exercise the adoption self-recovery: an in-flight wake whose worker
    # never reports and whose {:wake_timeout} timer is (modeled as) lost. wake_bound_ms
    # is set huge so the timer never fires within the test; the injected mono_clock
    # jumps past 2 * wakeTimeoutSeconds (1s here) between the wake stamp and the
    # reconcile, so wake_stuck? trips and adoption recovers the workload instead of
    # skipping it. The parked caller is erred out of its :infinity wait.
    {:ok, mono} = Agent.start_link(fn -> 0 end)
    mono_clock = fn -> Agent.get(mono, & &1) end

    ctx =
      start_stack(
        instance_id: "g-1",
        latency_ms: 3_000,
        fail: true,
        wake_bound_ms: 10 * 60_000,
        mono_clock: mono_clock,
        group_extra: %{wake_timeout_seconds: 1}
      )

    _ = seed_banked(ctx, "g-1")

    # Kick a wake that hangs (worker blocked in the fake for 60s). It stamps
    # wake_started at mono 0 and parks the caller.
    caller = Task.async(fn -> GroupWakeManager.wake(ctx.mgr, "grp-a", "system:group:grp-a") end)
    # Let the wake register as in-flight before advancing the clock.
    Process.sleep(50)

    # Advance mono past 2 * wakeTimeoutSeconds (2 * 1s = 2000ms).
    Agent.update(mono, fn _ -> 5_000 end)

    # The reconcile now sees the wake as stuck and recovers it (erra the caller,
    # releases single-flight, leaves the instance re-wakeable).
    :ok = GroupWakeManager.reconcile(ctx.mgr)

    assert {:error, {:wake_failed, :wake_stuck}} = Task.await(caller, 5_000)

    {:ok, inst} = GroupStore.get(ctx.store, "g-1")
    assert inst.state == :banked
  end
end
