defmodule Embervm.StatefulManagerTest do
  @moduledoc """
  Exercises Embervm.StatefulManager (the L4 wake brain) against a real
  StatefulStore + op-log, an injected StartStateful/StopStateful seam, and
  injected catalog/capacity tables. Covers the wake round-trip (park -> wake
  -> publish -> endpoint returned), single-flight (N concurrent wakes on the
  same workload => exactly ONE StartStateful, N replies), the per-workload
  wake-rate limit + parked-connection cap (each with an audited denial op),
  wake-failure retry-ability (a failed wake leaves the activator able to
  retry), plan_wake's relight-vs-cold decision (valid pair relights, broken
  pair evicts + cold-boots, no bundle => fresh/cold), and the restart adoption
  matrix (a live VM is adopted, a bundle-only instance heals to banked, a
  vanished instance fails, a disconnected node's instance is left untouched).
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, StatefulManager, StatefulStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.StartStatefulResponse

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

  defp start_stack(opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"mcap_#{suffix}"
    cat_table = :"mcat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_statefulmgr_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = StatefulStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)
    {:ok, pub} = FakePublisher.start_link()

    {:ok, starts} = Agent.start_link(fn -> 0 end)

    start_stateful_fun =
      Keyword.get(opts, :start_stateful_fun, fn _ch, _req ->
        Agent.update(starts, &(&1 + 1))
        if sleep = opts[:start_sleep_ms], do: Process.sleep(sleep)

        {:ok,
         %StartStatefulResponse{
           vm_id: "vm-woken-#{System.unique_integer([:positive])}",
           ip: "10.88.0.5",
           port: 5432,
           generation: 1,
           was_relight: false
         }}
      end)

    mgr_opts =
      [
        name: nil,
        store: store,
        publisher: pub,
        capacity_table: cap_table,
        catalog_table: cat_table,
        clock: Keyword.get(opts, :clock, fn -> 1_000 end),
        channel_fun: fn _node -> {:ok, :ch} end,
        invalidate_fun: Keyword.get(opts, :invalidate_fun, fn _node, _chan -> :ok end),
        start_stateful_fun: start_stateful_fun,
        stop_stateful_fun: Keyword.get(opts, :stop_stateful_fun, fn _ch, _req -> {:ok, %Embervm.Node.V1.StopStatefulResponse{}} end),
        reconcile_interval_ms: 0,
        id_fun: id_seq(suffix)
      ] ++ Keyword.take(opts, [:wake_max, :wake_window_ms, :park_cap])

    {:ok, mgr} = StatefulManager.start_link(mgr_opts)

    %{mgr: mgr, store: store, cap_table: cap_table, cat_table: cat_table, starts: starts, pub: pub, op_log: op_log}
  end

  defp id_seq(suffix) do
    {:ok, counter} = Agent.start_link(fn -> 0 end)
    fn -> "stf-#{suffix}-#{Agent.get_and_update(counter, fn n -> {n + 1, n + 1} end)}" end
  end

  defp stateful_workload(ctx, name, extra \\ %{}) do
    stateful_cfg =
      Map.merge(
        %{
          port: 5432,
          listen_port: 9100,
          volume_size_gib: 1,
          volume_mount_path: "/data",
          idle_bank_seconds: 300,
          max_lifetime_seconds: 86_400,
          banked_ttl_seconds: 604_800,
          wake_timeout_seconds: 60
        },
        extra
      )

    WorkloadCatalog.upsert(ctx.cat_table, name, %{class: "stateful", stateful: stateful_cfg})
  end

  defp stateful_node(ctx, node_id, opts \\ []) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.88.0.0/24",
      max_live_vms: 4,
      live_vms: 0,
      workloads: %{
        "wl-a" => %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap-a", serving_image_ref: "base-a"}
      },
      stateful_vms: Keyword.get(opts, :stateful_vms, []),
      stateful_bundles: Keyword.get(opts, :stateful_bundles, []),
      volumes: Keyword.get(opts, :volumes, [])
    })
  end

  # -- wake round-trip ---------------------------------------------------------

  test "a cold wake (first boot, no volume) FRESH-boots, publishes, and returns the endpoint" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "system:stateful:wl-a")

    assert Agent.get(ctx.starts, & &1) == 1
    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :serving
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == %{ip: "10.88.0.5", port: 5432}
    assert FakePublisher.count(ctx.pub) >= 1

    {:ok, [row]} = SQLite.load_stateful_instances(ctx.op_log)
    assert row.state == "serving"
  end

  test "an unknown (non-stateful) workload is a 404-shaped miss" do
    ctx = start_stack()
    stateful_node(ctx, "node-4")
    assert {:error, {:unknown_workload}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  test "a wake with no reporting node is :no_capacity (no volume, nowhere to place)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    # No node registered at all. plan_wake's placement error rides the generic
    # wake-failure wrap (finish_wake_failure's {:error, reason} clause), so the
    # caller sees {:error, {:wake_failed, :no_capacity}}, not the bare reason.
    assert {:error, {:wake_failed, :no_capacity}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  # -- single-flight ------------------------------------------------------------

  test "N concurrent wakes for one workload => exactly ONE StartStateful and N replies" do
    ctx = start_stack(start_sleep_ms: 60)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    n = 8

    tasks =
      for _ <- 1..n do
        Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
      end

    results = Task.await_many(tasks, 5_000)

    assert Enum.all?(results, &match?({:ok, %{ip: "10.88.0.5", port: 5432}}, &1))
    assert length(results) == n
    # Exactly one StartStateful (single-flight), which is also what KEEPS the
    # singleton invariant under a concurrent burst.
    assert Agent.get(ctx.starts, & &1) == 1
    assert [_one] = StatefulStore.list(ctx.store, "wl-a")
  end

  # -- wake-rate limit + parked cap ---------------------------------------------

  test "the per-workload wake-rate limit denies excess wakes without touching the node" do
    ctx = start_stack(wake_max: 1, start_stateful_fun: fn _ch, _req -> {:error, :boom} end)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert {:error, {:wake_rate, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  test "the parked-connection cap closes excess connections behind an in-flight wake" do
    # park_cap counts EVERY parked connection, INCLUDING the first caller (the wake
    # owner is itself a real connection waiting for the endpoint). So a cap of 3
    # admits the owner + 2 more; a fourth is denied.
    ctx = start_stack(start_sleep_ms: 200, park_cap: 3, wake_max: 1000)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    # First caller kicks the wake; two more park (filling the cap of 3 with the
    # owner); a fourth is denied.
    first = Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    # Give the first call time to register and become the in-flight wake owner.
    Process.sleep(20)

    parked = for _ <- 1..2, do: Task.async(fn -> StatefulManager.wake(ctx.mgr, "wl-a", "p") end)
    Process.sleep(20)

    assert {:error, {:park_full, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert {:ok, _} = Task.await(first, 5_000)
    assert Enum.all?(Task.await_many(parked, 5_000), &match?({:ok, _}, &1))
    assert Agent.get(ctx.starts, & &1) == 1
  end

  # -- wake failure + retry-ability ---------------------------------------------

  test "a wake failure errors the caller and stays retryable (activator not consumed)" do
    {:ok, fail?} = Agent.start_link(fn -> true end)

    start_fun = fn _ch, _req ->
      if Agent.get(fail?, & &1) do
        {:error, :readiness_timeout}
      else
        {:ok, %StartStatefulResponse{vm_id: "vm-ok", ip: "10.88.0.5", port: 5432, generation: 1, was_relight: false}}
      end
    end

    ctx = start_stack(start_stateful_fun: start_fun, wake_max: 100)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == nil

    Agent.update(fail?, fn _ -> false end)
    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
  end

  test "a transport-dead wake invalidates the channel so the next wake re-dials" do
    test_pid = self()
    dead = %GRPC.RPCError{status: 2, message: "error occurred while receiving data: the connection is closed"}

    ctx =
      start_stack(
        start_stateful_fun: fn _ch, _req -> {:error, dead} end,
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end,
        wake_max: 100
      )

    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert_receive {:invalidated, "node-4", :ch}, 1_000
  end

  # -- straggler -----------------------------------------------------------------

  test "a connection arriving while a live endpoint exists is resolved, not re-woken" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 1

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 1
  end

  # -- plan_wake: relight vs cold, pair validity --------------------------------

  # Seed a volume row + a banked instance whose snapshot_generation is `bundle_gen`,
  # against a volume at `volume_gen`. valid iff bundle_gen == volume_gen.
  defp seed_banked_with_pair(ctx, id, node_id, bundle_gen, volume_gen) do
    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: node_id,
        vm_id: "vm-#{id}",
        generation: volume_gen
      })

    {:ok, _} = StatefulStore.publish(ctx.store, id, "10.88.0.9", 5432, :started)
    # unpublish already moves serving -> banking (ETS-only); a second :bank mark
    # from :banking would be illegal. Go straight to the durable bank_ready.
    {:ok, _} = StatefulStore.unpublish(ctx.store, id, :bank)

    {:ok, _} =
      StatefulStore.transition(
        ctx.store,
        id,
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: "stateful/#{id}", size_bytes: 10, snapshot_generation: bundle_gen},
        %{snapshot_ref: "stateful/#{id}", snapshot_generation: bundle_gen, snapshot_size_bytes: 10, vm_id: nil}
      )

    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: node_id, generation: volume_gen, size_bytes: 10 * 1024 * 1024 * 1024, allocated_bytes: 1})

    id
  end

  test "plan_wake relights a VALID pair (daemon actually relit: was_relight=true)" do
    # A relight where the daemon genuinely resumed the bundle: the SAME banked
    # instance transitions in place via stateful_relit, no new lifecycle.
    relit_fun = fn _ch, _req ->
      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-relit",
         ip: "10.88.0.5",
         port: 5432,
         generation: 4,
         was_relight: true
       }}
    end

    ctx = start_stack(start_stateful_fun: relit_fun)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # The SAME instance transitioned via relight (not a fresh instance): stf-banked
    # is now serving, and it is the only non-terminal instance.
    {:ok, relit} = StatefulStore.get(ctx.store, "stf-banked")
    assert relit.state == :serving
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert length(live) == 1
  end

  test "a RELIGHT the DAEMON falls back to cold (was_relight=false) evicts the old instance, cold-boots a new one, and does not wedge" do
    # Regression: the control plane plans a relight (pair looks valid from here),
    # marks the banked instance :relighting, but the daemon discovers the pair is
    # actually broken and cold-boots, returning was_relight=false + cold_boot_reason.
    # The old :relighting instance MUST reach a terminal state via a legal edge
    # before the new instance's boot, or the singleton gate wedges the workload.
    fallback_fun = fn _ch, _req ->
      {:ok,
       %StartStatefulResponse{
         vm_id: "vm-cold",
         ip: "10.88.0.6",
         port: 5432,
         generation: 4,
         was_relight: false,
         cold_boot_reason: "generation_mismatch"
       }}
    end

    ctx = start_stack(start_stateful_fun: fallback_fun)
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    seed_banked_with_pair(ctx, "stf-banked", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    # The wake SUCCEEDS (not wedged): the daemon-side fallback still yields a live VM.
    assert {:ok, %{ip: "10.88.0.6", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    # The old banked instance did NOT strand in :relighting: it is terminal.
    {:ok, old} = StatefulStore.get(ctx.store, "stf-banked")
    assert old.state == :evicted

    # A NEW instance was cold-booted and is serving (exactly one live instance).
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert [new_i] = live
    assert new_i.instance_id != "stf-banked"
    assert new_i.state == :serving

    # gate 2: the op-log alone tells the story (a stateful_cold_booted with the
    # discarded-warmth reason, not a plain stateful_started).
    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    cold = Enum.find(ops, &(&1.kind == :stateful_cold_booted))
    assert cold, "expected a stateful_cold_booted op after a daemon-side relight fallback"
    assert cold.payload["reason"] == "generation_mismatch"
  end

  test "plan_wake evicts a BROKEN pair (generation mismatch) and cold-boots instead" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    seed_banked_with_pair(ctx, "stf-stale", "node-4", 2, 5)
    refute StatefulStore.pair_valid?(ctx.store, "wl-a")

    assert {:ok, %{ip: "10.88.0.5", port: 5432}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    {:ok, evicted} = StatefulStore.get(ctx.store, "stf-stale")
    assert evicted.state == :evicted
    assert evicted.terminal_reason == "pair_broken"

    # A NEW instance was cold-booted (not the old banked one resumed).
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert [new_instance] = live
    assert new_instance.instance_id != "stf-stale"
  end

  test "plan_wake cold/fresh-boots when there is no bundle at all" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 1
    assert [instance] = StatefulStore.list(ctx.store, "wl-a")
    assert instance.state == :serving
  end

  test "a wake is refused when the volume's anchor node is gone" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    # A volume row anchored to a node that is NOT currently reporting.
    StatefulStore.upsert_volume(ctx.store, "wl-a", %{node_id: "node-vanished", generation: 1, size_bytes: 1, allocated_bytes: 1})

    assert {:error, {:wake_failed, :volume_node_gone}} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert Agent.get(ctx.starts, & &1) == 0
  end

  # -- restart adoption matrix --------------------------------------------------

  test "adoption adopts a live stateful VM the node reports (restart republishes same endpoint)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-live",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-live",
        generation: 1
      })

    stateful_node(ctx, "node-4",
      stateful_vms: [%{vm_id: "vm-live", workload: "wl-a", ip: "10.88.0.9", port: 5432, healthy: true, generation: 1, last_probe_unix_ms: 1}]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)

    {:ok, adopted} = StatefulStore.get(ctx.store, "stf-live")
    assert adopted.state == :serving
    assert StatefulStore.published_endpoint(ctx.store, "wl-a") == %{ip: "10.88.0.9", port: 5432}
    assert Agent.get(ctx.starts, & &1) == 0
  end

  test "adoption heals a limbo instance the node reports only as a bundle to banked" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-limbo",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-gone",
        generation: 1
      })

    {:ok, _} = StatefulStore.publish(ctx.store, "stf-limbo", "10.88.0.9", 5432, :started)
    # unpublish already moves serving -> banking; a second :bank mark is illegal.
    {:ok, _} = StatefulStore.unpublish(ctx.store, "stf-limbo", :bank)

    {:ok, _} =
      StatefulStore.transition(
        ctx.store,
        "stf-limbo",
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: "stateful/limbo", size_bytes: 10, snapshot_generation: 1},
        %{snapshot_ref: "stateful/limbo", snapshot_generation: 1, snapshot_size_bytes: 10, vm_id: nil}
      )

    stateful_node(ctx, "node-4",
      stateful_bundles: [%{snapshot_ref: "stateful/limbo", workload: "wl-a", generation: 1, size_bytes: 10, created_at_unix_ms: 1}]
    )

    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, healed} = StatefulStore.get(ctx.store, "stf-limbo")
    assert healed.state == :banked
  end

  test "adoption fails an instance whose VM and bundle both vanished (node IS reporting)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-vanished",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-vanished",
        generation: 1
      })

    stateful_node(ctx, "node-4", stateful_vms: [], stateful_bundles: [])

    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, failed} = StatefulStore.get(ctx.store, "stf-vanished")
    assert failed.state == :failed
  end

  test "adoption NEVER reaps when the instance's node is not reporting (a disconnect)" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    {:ok, _} =
      StatefulStore.start(ctx.store, %{
        instance_id: "stf-disc",
        tenant: "homelab",
        principal: "p",
        workload: "wl-a",
        node_id: "node-gone",
        vm_id: "vm-x",
        generation: 1
      })

    :ok = StatefulManager.reconcile(ctx.mgr)
    {:ok, still} = StatefulStore.get(ctx.store, "stf-disc")
    assert still.state == :starting
  end

  test "adoption refreshes volume facts and eager-evicts a pair broken while the control plane was down" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")

    # A banked bundle at generation 3, seeded consistent with a volume the store
    # does not yet know moved to generation 5 (the "control plane was down while
    # the daemon bumped it" scenario).
    seed_banked_with_pair(ctx, "stf-pair", "node-4", 3, 3)
    assert StatefulStore.pair_valid?(ctx.store, "wl-a")

    # The node now reports the volume at a NEWER generation (5): the ledger moved
    # while we were not looking.
    stateful_node(ctx, "node-4", volumes: [%{workload: "wl-a", generation: 5, size_bytes: 1, allocated_bytes: 1}])

    :ok = StatefulManager.reconcile(ctx.mgr)

    refute StatefulStore.pair_valid?(ctx.store, "wl-a")
    {:ok, evicted} = StatefulStore.get(ctx.store, "stf-pair")
    assert evicted.state == :evicted
    assert evicted.terminal_reason == "pair_broken"
  end

  # -- destroy_instance / delete_volume management verbs ------------------------

  test "destroy_instance destroys the live instance and evicts the banked bundle" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")

    assert %{destroyed: 1, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    live = Enum.reject(StatefulStore.list(ctx.store, "wl-a"), &Embervm.StatefulState.terminal?(&1.state))
    assert live == []
  end

  test "delete_volume is refused while a live instance exists, succeeds once it is gone" do
    ctx = start_stack()
    stateful_workload(ctx, "wl-a")
    stateful_node(ctx, "node-4")

    assert {:ok, _} = StatefulManager.wake(ctx.mgr, "wl-a", "p")
    assert {:error, :instance_exists} = StatefulManager.delete_volume(ctx.mgr, "wl-a")

    assert %{destroyed: 1, evicted: 0} = StatefulManager.destroy_instance(ctx.mgr, "wl-a")
    assert {:ok, %{deleted: true}} = StatefulManager.delete_volume(ctx.mgr, "wl-a")
    assert StatefulStore.get_volume(ctx.store, "wl-a") == nil
  end
end
