defmodule Embervm.ServingManagerTest do
  @moduledoc """
  Exercises Embervm.ServingManager (the activator) against a real ServingStore +
  op-log, an injected StartServing seam (a fakenode stand-in, with optional latency
  injection for the single-flight property test), and injected catalog/capacity
  tables. Covers the miss round-trip (wake -> publish -> endpoint returned),
  single-flight (N concurrent misses => exactly ONE StartServing, N responses),
  the wake-rate 429, wake-failure 503 + retry-ability, the straggler path (a live
  endpoint exists: resolved, not woken), and the restart adoption matrix.
  """
  # async: false: put_env/delete_env on EMBERVM_PLACEMENT_RETRY here would leak the
  # gate into other async modules' gate-off assertions and flake CI.
  use ExUnit.Case, async: false

  alias Embervm.{NodeCapacity, NodeRegistry, ServingManager, ServingStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Scheduler.Inflight
  alias Embervm.Node.V1.{NodeStatus, ResourceSpec, ServingVm, StartServingResponse, StopServingResponse}

  # A no-op publisher (a GenServer that counts publish/1 casts) so the manager's
  # EndpointPublisher.publish call has a live target without a real sidecar.
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
    # A private in-flight ledger, so one test's settled claim (held for seconds)
    # never debits another test's same-named brick.
    inflight_table = Keyword.get_lazy(opts, :inflight_table, fn -> Inflight.create(:"minfl_#{suffix}") end)

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_servingmgr_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = ServingStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)
    {:ok, pub} = FakePublisher.start_link()

    # Injected StartServing seam: counts calls (single-flight proof) and returns a
    # deterministic endpoint, with optional latency so concurrent misses overlap.
    {:ok, starts} = Agent.start_link(fn -> 0 end)
    {:ok, stop_calls} = Agent.start_link(fn -> [] end)
    {:ok, destroy_confirmed} = Agent.start_link(fn -> Keyword.get(opts, :destroy_confirmed, true) end)

    start_serving_fun =
      Keyword.get(opts, :start_serving_fun, fn _ch, _req ->
        Agent.update(starts, &(&1 + 1))
        if sleep = opts[:start_sleep_ms], do: Process.sleep(sleep)
        {:ok, %StartServingResponse{vm_id: "vm-woken-#{System.unique_integer([:positive])}", ip: "10.99.0.5", port: 8080}}
      end)

    stop_serving_fun = fn _ch, req ->
      Agent.update(stop_calls, &[req | &1])
      {:ok, %StopServingResponse{teardown_confirmed: Agent.get(destroy_confirmed, & &1)}}
    end

    mgr_opts =
      [
        name: nil,
        store: store,
        publisher: pub,
        capacity_table: cap_table,
        catalog_table: cat_table,
        inflight_table: inflight_table,
        clock: Keyword.get(opts, :clock, fn -> 1_000 end),
        # Default echoes nothing (a fixed channel); a retry test overrides this to
        # echo the dial_id so its StartServing stub can reject a SPECIFIC brick.
        channel_fun: Keyword.get(opts, :channel_fun, fn _node -> {:ok, :ch} end),
        invalidate_fun: Keyword.get(opts, :invalidate_fun, fn _node, _chan -> :ok end),
        start_serving_fun: start_serving_fun,
        stop_serving_fun: Keyword.get(opts, :stop_serving_fun, stop_serving_fun),
        op_log: op_log,
        reconcile_interval_ms: 0,
        id_fun: id_seq(suffix),
        node_confirmed_destroy: Keyword.get(opts, :node_confirmed_destroy, false),
        destroying_alarm_ms: Keyword.get(opts, :destroying_alarm_ms, 300_000)
      ] ++ Keyword.take(opts, [:wake_max, :wake_window_ms, :park_cap, :restore_artifact_fun])

    {:ok, mgr} = ServingManager.start_link(mgr_opts)

    %{
      mgr: mgr,
      store: store,
      cap_table: cap_table,
      cat_table: cat_table,
      inflight_table: inflight_table,
      starts: starts,
      stop_calls: stop_calls,
      destroy_confirmed: destroy_confirmed,
      pub: pub,
      op_log: op_log
    }
  end

  defp stop_calls(ctx), do: Agent.get(ctx.stop_calls, &Enum.reverse(&1))

  defp id_seq(suffix) do
    {:ok, counter} = Agent.start_link(fn -> 0 end)
    fn -> "srv-#{suffix}-#{Agent.get_and_update(counter, fn n -> {n + 1, n + 1} end)}" end
  end

  defp serving_workload(ctx, name) do
    WorkloadCatalog.upsert(ctx.cat_table, name, %{
      class: "serving",
      serving: %{host: "#{name}.example", port: 8080, health_path: "/healthz", min_instances: 0, max_instances: 2}
    })
  end

  defp serving_node(ctx, node_id, opts \\ []) do
    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.99.0.0/24",
      max_live_vms: 4,
      live_vms: 0,
      workloads: %{
        "wl-a" => %{
          base_state: :BASE_BUILD_STATE_READY,
          snapshot_ref: "snap-a",
          serving_image_ref: Keyword.get(opts, :serving_image_ref, "base-a")
        }
      },
      serving_vms: Keyword.get(opts, :serving_vms, []),
      serving_snapshots: Keyword.get(opts, :serving_snapshots, []),
      store_reachable: Keyword.get(opts, :store_reachable, false)
    })
  end

  # Give these grace tests the production capacity writer rather than inserting
  # a hand-built NodeCapacity row. The fake watch remains blocked until teardown;
  # NodeStatus is injected synchronously so the manager clock alone controls the
  # before-grace and after-grace observations.
  defp register_grace_node(ctx, clock) do
    true = :ets.delete(ctx.cap_table)

    {:ok, registry} =
      NodeRegistry.start_link(
        name: nil,
        table: ctx.cap_table,
        nodes: [],
        watch_startup: false,
        registry_resync_ms: 0,
        clock: clock,
        connect_fun: fn _address -> {:ok, :grace_channel} end,
        watch_fun: fn _channel, _node_id, _emit ->
          receive do: (:never -> {:ok, :closed})
        end,
        sync_registry_fun: fn _channel, _node_id, _request -> :ok end,
        disconnect_fun: fn :grace_channel -> :ok end,
        channel_updater_fun: fn _instance_id, _address -> :ok end,
        channel_remover_fun: fn _instance_id -> :ok end,
        base_builder_updater_fun: fn _update -> :ok end,
        reassign_fun: fn _node_id -> :ok end
      )

    on_exit(fn -> Embervm.TestProcess.stop_safely(registry) end)

    :ok =
      NodeRegistry.register(registry, %{
        "node" => "node-4",
        "pod_uid" => "grace-pod",
        "address" => "10.0.0.4:9090"
      })

    {registry, "node-4/grace-pod"}
  end

  defp req, do: %{method: "GET", path: "/og-image", headers: %{}, body: ""}

  # -- miss round-trip -------------------------------------------------------

  test "a cold miss wakes the workload, publishes, and returns the endpoint" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    assert {:ok, %{ip: "10.99.0.5", port: 8080}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")

    # Exactly one StartServing, one instance now published, publisher was asked.
    assert Agent.get(ctx.starts, & &1) == 1
    assert [instance] = ServingStore.list(ctx.store, "wl-a")
    assert instance.state == :published
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == [%{ip: "10.99.0.5", port: 8080}]
    assert FakePublisher.count(ctx.pub) >= 1

    # The durable projection recorded serving_started + serving_published.
    {:ok, [row]} = SQLite.load_serving_instances(ctx.op_log)
    assert row.state == "published"
  end

  test "an unknown (non-serving) workload is a 404 miss" do
    ctx = start_stack()
    serving_node(ctx, "node-4")
    # No catalog entry for wl-a.
    assert {:error, {:unknown_workload}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
  end

  # -- single-flight ---------------------------------------------------------

  test "N concurrent misses for one workload => exactly ONE StartServing and N responses" do
    ctx = start_stack(start_sleep_ms: 60)
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    n = 8

    tasks =
      for _ <- 1..n do
        Task.async(fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)
      end

    results = Task.await_many(tasks, 5_000)

    # Every caller got the SAME live endpoint...
    assert Enum.all?(results, &match?({:ok, %{ip: "10.99.0.5", port: 8080}}, &1))
    assert length(results) == n
    # ...off exactly ONE StartServing (single-flight).
    assert Agent.get(ctx.starts, & &1) == 1
    # And exactly one instance exists.
    assert [_one] = ServingStore.list(ctx.store, "wl-a")
  end

  # -- wake-rate limit -------------------------------------------------------

  test "the per-(workload) wake-rate limit 429s excess wakes without touching the node" do
    # wake_max 1: the first miss wakes, a SECOND distinct wake in the window is 429.
    # Use a workload with NO node so the first wake fails fast (no capacity), freeing
    # the ledger, then the second miss trips the rate limit rather than parking.
    ctx = start_stack(wake_max: 1, start_serving_fun: fn _ch, _req -> {:error, :boom} end)
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    # First miss consumes the one wake token (and fails the wake -> 503).
    assert {:error, {:wake_failed, _}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    # Second miss in the window: rate-limited (429), node never touched again.
    assert {:error, {:wake_rate, _}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
  end

  # -- wake failure + retry-ability ------------------------------------------

  test "a wake failure 503s the caller and stays retryable (activator not consumed)" do
    {:ok, fail?} = Agent.start_link(fn -> true end)

    start_fun = fn _ch, _req ->
      if Agent.get(fail?, & &1) do
        {:error, :readiness_timeout}
      else
        {:ok, %StartServingResponse{vm_id: "vm-ok", ip: "10.99.0.5", port: 8080}}
      end
    end

    ctx = start_stack(start_serving_fun: start_fun, wake_max: 100)
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    # First miss: the wake fails -> 503.
    assert {:error, {:wake_failed, _}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    # No live endpoint was published (activator stays the fallback).
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []

    # Recover the node; the NEXT miss retries the wake and succeeds.
    Agent.update(fail?, fn _ -> false end)
    assert {:ok, %{ip: "10.99.0.5", port: 8080}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == [%{ip: "10.99.0.5", port: 8080}]
  end

  test "a transport-dead wake invalidates the channel so the next miss re-dials" do
    # Regression for the noded-pod-restart wedge: a StartServing failing because the
    # channel's transport is dead (wrapped as an RPCError) must tear the cached channel
    # down, else serving stays wedged on that node until the control plane restarts.
    test_pid = self()
    dead = %GRPC.RPCError{status: 2, message: "error occurred while receiving data: the connection is closed"}

    ctx =
      start_stack(
        start_serving_fun: fn _ch, _req -> {:error, dead} end,
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end,
        wake_max: 100
      )

    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert_receive {:invalidated, "node-4", :ch}, 1_000
  end

  test "a server-status wake failure leaves the channel up (no needless invalidate)" do
    test_pid = self()

    ctx =
      start_stack(
        start_serving_fun: fn _ch, _req -> {:error, %GRPC.RPCError{status: 9, message: "snapshot lost"}} end,
        invalidate_fun: fn node_id, chan -> send(test_pid, {:invalidated, node_id, chan}) end,
        wake_max: 100
      )

    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    assert {:error, {:wake_failed, _}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    refute_receive {:invalidated, _, _}, 300
  end

  # -- straggler -------------------------------------------------------------

  test "a request arriving while a healthy endpoint exists is resolved, not re-woken" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    # Warm it (one wake).
    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert Agent.get(ctx.starts, & &1) == 1

    # A straggler miss now finds the live endpoint and is proxied WITHOUT a new wake.
    assert {:ok, %{ip: "10.99.0.5", port: 8080}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert Agent.get(ctx.starts, & &1) == 1
  end

  # -- restart adoption matrix -----------------------------------------------

  test "adoption rebinds a live serving VM the node reports (restart republishes same endpoint)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")

    # Simulate a pre-restart instance: started + published, then the control plane
    # "restarted" (the durable row survives; the endpoint fact is re-learned from the
    # node). Record a started instance directly in the store.
    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: "srv-live",
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-live",
        ip: "10.99.0.9",
        port: 8080
      })

    # The node reports that vm as a LIVE healthy serving VM.
    serving_node(ctx, "node-4",
      serving_vms: [%{vm_id: "vm-live", workload: "wl-a", ip: "10.99.0.9", port: 8080, healthy: true, last_probe_unix_ms: 1}]
    )

    :ok = ServingManager.reconcile(ctx.mgr)

    {:ok, adopted} = ServingStore.get(ctx.store, "srv-live")
    assert adopted.state == :published
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == [%{ip: "10.99.0.9", port: 8080}]
    # No StartServing was issued (the VM was never touched).
    assert Agent.get(ctx.starts, & &1) == 0
  end

  describe "boot-relative endpoint grace" do
    test "a serving instance within the grace window never loses its endpoint" do
      {:ok, now} = Agent.start_link(fn -> 0 end)
      clock = fn -> Agent.get(now, & &1) end
      ctx = start_stack(clock: clock)

      {:ok, _} =
        ServingStore.start(ctx.store, %{
          instance_id: "srv-grace-live",
          tenant: "homelab",
          principal: "test",
          workload: "wl-grace",
          node_id: "node-4",
          vm_id: "vm-grace-live",
          ip: "10.99.0.9",
          port: 8080,
          base_snapshot_ref: "base:grace",
          base_digest: "sha256:grace"
        })

      {:ok, _} = ServingStore.publish(ctx.store, "srv-grace-live", "10.99.0.9", 8080, :started)
      endpoints = [%{ip: "10.99.0.9", port: 8080}]
      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints

      {registry, instance_id} = register_grace_node(ctx, clock)

      # No capacity exists until the brick's first NodeStatus. Repeated reconcile
      # passes during the normal deploy gap must preserve the optimistic endpoint
      # all the way through the last millisecond of the shared grace window.
      for elapsed_ms <- [30_000, 60_000, 89_999] do
        Agent.update(now, fn _ -> elapsed_ms end)
        :ok = ServingManager.reconcile(ctx.mgr)
        assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints
      end

      :ok =
        NodeRegistry.inject_status(registry, instance_id, %NodeStatus{
          node_id: "node-4",
          serving_vms: [
            %ServingVm{
              vm_id: "vm-grace-live",
              workload: "wl-grace",
              ip: "10.99.0.9",
              port: 8080,
              healthy: true
            }
          ]
        })

      # Reconcile at the same pre-deadline instant so the fresh NodeStatus is
      # adopted before the manager's boot-relative grace closes.
      :ok = ServingManager.reconcile(ctx.mgr)
      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints

      # Once adopted from node truth, the endpoint remains valid after the
      # deadline because its owner has reported at least once.
      Agent.update(now, fn _ -> 90_001 end)
      :ok = ServingManager.reconcile(ctx.mgr)
      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints
    end

    test "a serving instance whose node never reports loses its endpoint after grace expires" do
      {:ok, now} = Agent.start_link(fn -> 0 end)
      clock = fn -> Agent.get(now, & &1) end
      ctx = start_stack(clock: clock)

      {:ok, _} =
        ServingStore.start(ctx.store, %{
          instance_id: "srv-grace-silent",
          tenant: "homelab",
          principal: "test",
          workload: "wl-grace",
          node_id: "node-4",
          vm_id: "vm-grace-silent",
          ip: "10.99.0.9",
          port: 8080,
          base_snapshot_ref: "base:grace",
          base_digest: "sha256:grace"
        })

      {:ok, _} = ServingStore.publish(ctx.store, "srv-grace-silent", "10.99.0.9", 8080, :started)
      endpoints = [%{ip: "10.99.0.9", port: 8080}]
      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints

      {_registry, _instance_id} = register_grace_node(ctx, clock)

      # Registration is not a capacity fact. Keep the last durable publication
      # through grace minus one millisecond while the node remains silent.
      Agent.update(now, fn _ -> 89_999 end)
      :ok = ServingManager.reconcile(ctx.mgr)
      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints

      # After the boot-relative bound, the still-empty capacity table withdraws
      # only endpoint health; the manager and durable lifecycle remain intact.
      Agent.update(now, fn _ -> 90_001 end)
      :ok = ServingManager.reconcile(ctx.mgr)
      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == []
    end

    test "a node that reported once is never withdrawn even if later silent" do
      {:ok, now} = Agent.start_link(fn -> 0 end)
      clock = fn -> Agent.get(now, & &1) end
      ctx = start_stack(clock: clock)

      {:ok, _} =
        ServingStore.start(ctx.store, %{
          instance_id: "srv-grace-seen",
          tenant: "homelab",
          principal: "test",
          workload: "wl-grace",
          node_id: "node-4",
          vm_id: "vm-grace-seen",
          ip: "10.99.0.9",
          port: 8080,
          base_snapshot_ref: "base:grace",
          base_digest: "sha256:grace"
        })

      {:ok, _} = ServingStore.publish(ctx.store, "srv-grace-seen", "10.99.0.9", 8080, :started)
      endpoints = [%{ip: "10.99.0.9", port: 8080}]
      {registry, instance_id} = register_grace_node(ctx, clock)

      :ok =
        NodeRegistry.inject_status(registry, instance_id, %NodeStatus{
          node_id: "node-4",
          serving_vms: [
            %ServingVm{
              vm_id: "vm-grace-seen",
              workload: "wl-grace",
              ip: "10.99.0.9",
              port: 8080,
              healthy: true
            }
          ]
        })

      assert {:ok, %{node_id: "node-4"}} = NodeCapacity.fetch(ctx.cap_table, "node-4")
      :ok = ServingManager.reconcile(ctx.mgr)
      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints
      assert MapSet.member?(:sys.get_state(ctx.mgr).seen_reporting_nodes, "node-4")

      NodeCapacity.drop(ctx.cap_table, {"node-4", "grace-pod"})
      assert NodeCapacity.fetch(ctx.cap_table, "node-4") == :error

      Agent.update(now, fn _ -> 90_001 end)

      for _ <- 1..3 do
        :ok = ServingManager.reconcile(ctx.mgr)
      end

      assert ServingStore.published_endpoints(ctx.store, "wl-grace") == endpoints
    end
  end

  test "adoption SKIPS a :destroying instance even though the node still reports its live VM" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")

    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: "srv-destroying",
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-live",
        ip: "10.99.0.9",
        port: 8080
      })

    # Drive the instance into :destroying via the begin_destroy FSM edge: mid
    # node-confirmed teardown (ADR embervm/014 decision 5), the teardown RPC in flight.
    {:ok, _} = ServingStore.mark(ctx.store, "srv-destroying", :begin_destroy)

    # The node still reports the VM as live+healthy: the exact straggler report that
    # turns on the TLC NoDestroyBeforeConfirm violation if adoption keys off the node.
    serving_node(ctx, "node-4",
      serving_vms: [%{vm_id: "vm-live", workload: "wl-a", ip: "10.99.0.9", port: 8080, healthy: true, last_probe_unix_ms: 1}]
    )

    :ok = ServingManager.reconcile(ctx.mgr)

    # NOT re-adopted to :published; stays destroying for the (gated) redrive to own.
    {:ok, inst} = ServingStore.get(ctx.store, "srv-destroying")
    assert inst.state == :destroying
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []
    assert Agent.get(ctx.starts, & &1) == 0
  end

  test "adoption leaves a :banking instance and a bank-draining instance alone while the node still reports the VM" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")

    for {instance_id, vm_id, ip} <- [{"srv-banking", "vm-banking", "10.99.0.11"}, {"srv-draining", "vm-draining", "10.99.0.12"}] do
      {:ok, _} =
        ServingStore.start(ctx.store, %{
          instance_id: instance_id,
          tenant: "homelab",
          principal: "serving:wl-a",
          workload: "wl-a",
          node_id: "node-4",
          vm_id: vm_id,
          ip: ip,
          port: 8080
        })

      {:ok, _} = ServingStore.publish(ctx.store, instance_id, ip, 8080, :started)
    end

    # The sweeper's drain-before-bank: unpublish with reason :bank, then mark
    # :bank. The VM stays live (and healthy) on the node through both steps.
    {:ok, _} = ServingStore.unpublish(ctx.store, "srv-draining", :bank)
    {:ok, _} = ServingStore.unpublish(ctx.store, "srv-banking", :bank)
    {:ok, _} = ServingStore.mark(ctx.store, "srv-banking", :bank)

    serving_node(ctx, "node-4",
      serving_vms: [
        %{vm_id: "vm-banking", workload: "wl-a", ip: "10.99.0.11", port: 8080, healthy: true, last_probe_unix_ms: 1},
        %{vm_id: "vm-draining", workload: "wl-a", ip: "10.99.0.12", port: 8080, healthy: true, last_probe_unix_ms: 1}
      ]
    )

    :ok = ServingManager.reconcile(ctx.mgr)

    # Neither row is forced back to :published: the endpoint stays out of the
    # fan-out and the sweeper's bank_ready transition stays legal.
    {:ok, banking} = ServingStore.get(ctx.store, "srv-banking")
    assert banking.state == :banking
    {:ok, draining} = ServingStore.get(ctx.store, "srv-draining")
    assert draining.state == :draining
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []

    # The node finished the bank: its NodeStatus now reports neither the VM nor a
    # snapshot the row knows about (snapshot_ref is only recorded by the sweeper's
    # bank_ready step, which may still be queued behind its stats scrape). The
    # row is the sweeper's to finish; reconcile must not fail it as vanished.
    serving_node(ctx, "node-4", serving_vms: [], serving_snapshots: [])
    :ok = ServingManager.reconcile(ctx.mgr)

    {:ok, banking} = ServingStore.get(ctx.store, "srv-banking")
    assert banking.state == :banking
    {:ok, draining} = ServingStore.get(ctx.store, "srv-draining")
    assert draining.state == :draining
    assert {:ok, _} = ServingStore.mark(ctx.store, "srv-banking", :bank_ready)
  end

  test "node-confirmed reconcile leaves an unconfirmed destroy intent and re-drives it" do
    ctx = start_stack(node_confirmed_destroy: true, destroy_confirmed: false)
    serving_workload(ctx, "wl-a")

    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: "srv-destroying",
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-live",
        ip: "10.99.0.9",
        port: 8080
      })

    {:ok, _} =
      ServingStore.transition(
        ctx.store,
        "srv-destroying",
        :begin_destroy,
        :serving_destroying,
        %{reason: :forced_roll},
        %{}
      )

    serving_node(ctx, "node-4",
      serving_vms: [%{vm_id: "vm-live", workload: "wl-a", ip: "10.99.0.9", port: 8080, healthy: true}]
    )

    :ok = ServingManager.reconcile(ctx.mgr)
    {:ok, destroying} = ServingStore.get(ctx.store, "srv-destroying")
    assert destroying.state == :destroying
    assert [%{mode: :STOP_SERVING_MODE_DESTROY, vm_id: "vm-live"}] = stop_calls(ctx)

    Agent.update(ctx.destroy_confirmed, fn _ -> true end)
    :ok = ServingManager.reconcile(ctx.mgr)

    {:ok, destroyed} = ServingStore.get(ctx.store, "srv-destroying")
    assert destroyed.state == :destroyed
    assert [_, %{mode: :STOP_SERVING_MODE_DESTROY, vm_id: "vm-live"}] = stop_calls(ctx)
  end

  test "node-confirmed reconcile confirms a destroying serving VM by reported absence" do
    ctx = start_stack(node_confirmed_destroy: true)
    serving_workload(ctx, "wl-a")

    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: "srv-gone",
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-gone",
        ip: "10.99.0.9",
        port: 8080
      })

    {:ok, _} =
      ServingStore.transition(
        ctx.store,
        "srv-gone",
        :begin_destroy,
        :serving_destroying,
        %{reason: :forced_roll},
        %{}
      )

    serving_node(ctx, "node-4", serving_vms: [])

    :ok = ServingManager.reconcile(ctx.mgr)

    {:ok, destroyed} = ServingStore.get(ctx.store, "srv-gone")
    assert destroyed.state == :destroyed
    assert stop_calls(ctx) == []
  end

  test "node-confirmed reconcile destroys a rowless reported serving VM" do
    ctx = start_stack(node_confirmed_destroy: true)
    serving_workload(ctx, "wl-a")

    serving_node(ctx, "node-4",
      serving_vms: [%{vm_id: "vm-orphan", workload: "wl-a", ip: "10.99.0.9", port: 8080, healthy: true}]
    )

    :ok = ServingManager.reconcile(ctx.mgr)

    assert [%{mode: :STOP_SERVING_MODE_DESTROY, vm_id: "vm-orphan"}] = stop_calls(ctx)
    assert ServingStore.all(ctx.store) == []
  end

  test "ADR embervm/018: reconcile ADOPTS an origin-ACTIVATOR serving VM instead of orphan-destroying it" do
    # node_confirmed_destroy on so the orphan-destroy pass is live: an ordinary
    # rowless VM would be destroyed here (the test above), but an ACTIVATOR VM is
    # adopted and skipped, never double-killed.
    ctx = start_stack(node_confirmed_destroy: true)
    serving_workload(ctx, "wl-a")

    # The brick woke a VM during a CP gap: reported origin ACTIVATOR with a vm_id
    # the control plane never issued and NO matching CP row.
    serving_node(ctx, "node-4",
      serving_vms: [
        %{
          vm_id: "vm-brick",
          workload: "wl-a",
          ip: "10.99.0.9",
          port: 8080,
          healthy: true,
          origin: :INSTANCE_ORIGIN_ACTIVATOR
        }
      ]
    )

    :ok = ServingManager.reconcile(ctx.mgr)

    # No StopServing(DESTROY) fired for it (contrast the rowless-orphan test above).
    assert stop_calls(ctx) == []

    # A published CP row was minted from node truth and its endpoint is in the
    # fan-out, keyed by the brick-minted vm_id (used as the instance id).
    assert [instance] = ServingStore.all(ctx.store)
    assert instance.vm_id == "vm-brick"
    assert instance.workload == "wl-a"
    assert instance.state == :published
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == [%{ip: "10.99.0.9", port: 8080}]

    # A second reconcile is a no-op (the row now exists, so the mint path is not
    # re-entered and the VM heals through the normal adopt_live path).
    :ok = ServingManager.reconcile(ctx.mgr)
    assert stop_calls(ctx) == []
    assert length(ServingStore.all(ctx.store)) == 1
  end

  test "adoption heals a starting-limbo instance the node reports only as a snapshot to banked" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")

    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: "srv-limbo",
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-gone",
        ip: "10.99.0.9",
        port: 8080
      })

    # Give it a snapshot_ref (as if it had banked) via a direct transition path:
    # unpublish is illegal from starting, so drive it published->draining->banking->banked.
    {:ok, _} = ServingStore.publish(ctx.store, "srv-limbo", "10.99.0.9", 8080, :started)
    {:ok, _} = ServingStore.unpublish(ctx.store, "srv-limbo", :banked)
    {:ok, _} = ServingStore.mark(ctx.store, "srv-limbo", :bank)

    {:ok, _} =
      ServingStore.transition(ctx.store, "srv-limbo", :bank_ready, :serving_banked,
        %{snapshot_ref: "serving/s-limbo", size_bytes: 1000, generation: 1},
        %{snapshot_ref: "serving/s-limbo", snapshot_size_bytes: 1000, generation: 1}
      )

    # Now the node reports ONLY the snapshot (no live VM): adoption keeps it banked.
    serving_node(ctx, "node-4", serving_snapshots: [%{snapshot_ref: "serving/s-limbo", workload: "wl-a", size_bytes: 1000, created_at_unix_ms: 1}])

    :ok = ServingManager.reconcile(ctx.mgr)
    {:ok, healed} = ServingStore.get(ctx.store, "srv-limbo")
    assert healed.state == :banked
  end

  test "adoption fails an instance whose VM and snapshot both vanished (node IS reporting)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")

    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: "srv-vanished",
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-vanished",
        ip: "10.99.0.9",
        port: 8080
      })

    # The node is reporting but lists NEITHER this vm NOR any snapshot for it.
    serving_node(ctx, "node-4", serving_vms: [], serving_snapshots: [])

    :ok = ServingManager.reconcile(ctx.mgr)
    {:ok, failed} = ServingStore.get(ctx.store, "srv-vanished")
    assert failed.state == :failed
  end

  test "adoption NEVER reaps when the instance's node is not reporting (a disconnect)" do
    ctx = start_stack()
    serving_workload(ctx, "wl-a")

    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: "srv-disc",
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-gone",
        vm_id: "vm-x",
        ip: "10.99.0.9",
        port: 8080
      })

    # node-gone is absent from the capacity table entirely (a disconnect). The
    # instance must be LEFT UNTOUCHED, not failed.
    :ok = ServingManager.reconcile(ctx.mgr)
    {:ok, still} = ServingStore.get(ctx.store, "srv-disc")
    assert still.state == :starting
  end

  # -- stale-lineage relight reject (D-R3.11.3 follow-up) --------------------

  # Seed a banked instance born from `base_snapshot_ref`, whose snapshot the node
  # reports as `snapshot_ref` (so node_for_relight resolves it).
  defp seed_banked(ctx, id, base_snapshot_ref, snapshot_ref, port \\ 8080) do
    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: id,
        tenant: "homelab",
        principal: "serving:wl-a",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-#{id}",
        ip: "10.99.0.9",
        port: port,
        base_snapshot_ref: base_snapshot_ref
      })

    {:ok, _} = ServingStore.publish(ctx.store, id, "10.99.0.9", port, :started)
    {:ok, _} = ServingStore.unpublish(ctx.store, id, :bank)
    {:ok, _} = ServingStore.mark(ctx.store, id, :bank)

    {:ok, _} =
      ServingStore.transition(ctx.store, id, :bank_ready, :serving_banked,
        %{snapshot_ref: snapshot_ref, size_bytes: 10, generation: 1},
        %{snapshot_ref: snapshot_ref, snapshot_size_bytes: 10, generation: 1, vm_id: nil}
      )

    id
  end

  defp recording_start_fun do
    {:ok, srcs} = Agent.start_link(fn -> [] end)

    fun = fn _ch, req ->
      Agent.update(srcs, &[elem(req.source, 0) | &1])
      {:ok, %StartServingResponse{vm_id: "vm-woke", ip: "10.99.0.5", port: 8080}}
    end

    {fun, srcs}
  end

  test "relight sends the GUEST port, not the stored (projected) endpoint port" do
    # Regression for the DNAT-projection relight bug (D-R3.11.4 fallout): the store keeps
    # instance.port as the PUBLISHED endpoint port (podIP:vmPort, e.g. 30002) for routing,
    # but a relight must send the GUEST port (spec.serving.port, 8080) so noded probes the
    # guest correctly. Reusing instance.port made relight probe tapIP:30002 -> refused.
    {:ok, ports} = Agent.start_link(fn -> [] end)

    ctx =
      start_stack(
        start_serving_fun: fn _ch, req ->
          Agent.update(ports, &[req.port | &1])
          {:ok, %StartServingResponse{vm_id: "vm-relit", ip: "10.99.0.5", port: 8080}}
        end
      )

    serving_workload(ctx, "wl-a")

    serving_node(ctx, "node-4",
      serving_snapshots: [%{snapshot_ref: "serving/s-1", workload: "wl-a", size_bytes: 10, created_at_unix_ms: 1}]
    )

    # Banked instance whose STORED port is the projected DNAT port 30002 (the bug
    # trigger); current lineage (base-a matches the node's serving_image_ref) so it relights.
    seed_banked(ctx, "srv-1", "base-a", "serving/s-1", 30002)

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    # The relight sent the guest port 8080 (serving_workload's serving.port), NOT 30002.
    assert Agent.get(ports, & &1) == [8080]
  end

  # The CP places a serving wake on the catalog's memMib, so the boot must carry
  # the same size. With no ResourceSpec noded admitted the boot at need 0 and
  # booted the driver default (1 vCPU / 1024 MiB), so a 128 MiB ping guest ran at
  # 1024 and a guest declaring more than 1024 ran undersized.
  defp sized_serving_workload(cat_table, name) do
    WorkloadCatalog.upsert(cat_table, name, %{
      class: "serving",
      vcpus: 2,
      mem_mib: 128,
      serving: %{host: "#{name}.example", port: 8080, health_path: "/healthz", min_instances: 0, max_instances: 2}
    })
  end

  defp recording_requests do
    {:ok, reqs} = Agent.start_link(fn -> [] end)

    fun = fn _ch, req ->
      Agent.update(reqs, &[req | &1])
      {:ok, %StartServingResponse{vm_id: "vm-#{System.unique_integer([:positive])}", ip: "10.99.0.5", port: 8080}}
    end

    {fun, reqs}
  end

  test "a relight StartServing carries the catalog's ResourceSpec" do
    {fun, reqs} = recording_requests()
    ctx = start_stack(start_serving_fun: fun)
    sized_serving_workload(ctx.cat_table, "wl-a")

    serving_node(ctx, "node-4",
      serving_snapshots: [%{snapshot_ref: "serving/s-1", workload: "wl-a", size_bytes: 10, created_at_unix_ms: 1}]
    )

    seed_banked(ctx, "srv-1", "base-a", "serving/s-1", 30002)
    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert [%{source: {:relight, _}} = relight] = Agent.get(reqs, & &1)
    assert relight.resources == %ResourceSpec{vcpus: 2, mem_mib: 128}
  end

  test "a cold StartServing carries the catalog's ResourceSpec" do
    {fun, reqs} = recording_requests()
    ctx = start_stack(start_serving_fun: fun)
    sized_serving_workload(ctx.cat_table, "wl-a")
    serving_node(ctx, "node-4")

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert [%{source: {:fresh, _}} = fresh] = Agent.get(reqs, & &1)
    assert fresh.resources == %ResourceSpec{vcpus: 2, mem_mib: 128}
  end

  test "a catalog entry with no declared size sends the size placement charged (1 vCPU, 512 MiB)" do
    {fun, reqs} = recording_requests()
    ctx = start_stack(start_serving_fun: fun)
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4")

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert [fresh] = Agent.get(reqs, & &1)
    assert fresh.resources == %ResourceSpec{vcpus: 1, mem_mib: 512}
  end

  # -- restore-on-miss (R6, Task 8) -------------------------------------------

  test "a banked instance whose snapshot is gone locally but exported RESTORES then relights" do
    {:ok, restore_calls} = Agent.start_link(fn -> [] end)

    restore_fun = fn _ch, req ->
      art = req.artifact
      Agent.update(restore_calls, &[%{kind: art.kind, ref: art.ref, workload: art.workload} | &1])
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{bytes_moved: 2048, skipped: false}}
    end

    relit_fun = fn _ch, _req ->
      {:ok, %StartServingResponse{vm_id: "vm-relit", ip: "10.99.0.5", port: 8080}}
    end

    ctx = start_stack(start_serving_fun: relit_fun, restore_artifact_fun: restore_fun)
    serving_workload(ctx, "wl-a")

    # The node reports NO serving snapshots (a true local miss) but a reachable store,
    # so the banked instance stays a relight candidate and the wake restores first.
    serving_node(ctx, "node-4", serving_snapshots: [], store_reachable: true)

    seed_banked(ctx, "srv-1", "base-a", "serving/s-1", 30002)

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")

    assert [%{kind: :ARTIFACT_KIND_SERVING, ref: "serving/s-1", workload: "wl-a"}] = Agent.get(restore_calls, & &1)

    {:ok, ops} = SQLite.read_from(ctx.op_log, 0)
    assert Enum.any?(ops, &(&1.kind == :artifact_restored and &1.payload["ref"] == "serving/s-1"))
  end

  test "a banked snapshot gone locally with an UNREACHABLE store attempts no restore and cold-boots" do
    {:ok, restore_calls} = Agent.start_link(fn -> [] end)
    restore_fun = fn _ch, _req -> Agent.update(restore_calls, &[:called | &1]) && {:error, :unused} end

    {fun, srcs} = recording_start_fun()

    ctx = start_stack(start_serving_fun: fun, restore_artifact_fun: restore_fun)
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4", serving_snapshots: [], store_reachable: false)

    seed_banked(ctx, "srv-1", "base-a", "serving/s-1", 30002)

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    # No restore attempted, and the wake cold-booted (a FRESH source, not a relight).
    assert Agent.get(restore_calls, & &1) == []
    assert Agent.get(srcs, & &1) == [:fresh]
  end

  test "a STALE-lineage banked instance is NOT relit: the wake cold-boots the current base" do
    {fun, srcs} = recording_start_fun()
    ctx = start_stack(start_serving_fun: fun)
    serving_workload(ctx, "wl-a")
    # Node reports the CURRENT serving_image_ref "base-a" (see serving_node) AND the
    # stale snapshot; the banked instance was born from a SUPERSEDED base "base-OLD".
    serving_node(ctx, "node-4",
      serving_snapshots: [%{snapshot_ref: "serving/s-old", workload: "wl-a", size_bytes: 10, created_at_unix_ms: 1}]
    )

    seed_banked(ctx, "srv-old", "base-OLD", "serving/s-old")

    assert {:ok, %{ip: "10.99.0.5", port: 8080}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    # The wake was a COLD boot (:fresh), not a relight of the stale snapshot.
    assert Agent.get(srcs, & &1) == [:fresh]
  end

  test "a CURRENT-lineage banked instance IS relit (the reject is lineage-specific)" do
    {fun, srcs} = recording_start_fun()
    ctx = start_stack(start_serving_fun: fun)
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-4",
      serving_snapshots: [%{snapshot_ref: "serving/s-cur", workload: "wl-a", size_bytes: 10, created_at_unix_ms: 1}]
    )

    # base_snapshot_ref matches the node's current serving_image_ref "base-a".
    seed_banked(ctx, "srv-cur", "base-a", "serving/s-cur")

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert Agent.get(srcs, & &1) == [:relight]
  end

  # -- reject/retry placement (ADR 014 decision 3) ---------------------------

  # Seed one co-located brick on `node_id` with a DISTINCT instance/pod so two
  # bricks share a node. Base-ready + a real budget so it is a cold-placement
  # candidate. Keyed by the {node_id, pod_uid} tuple (the per-instance key).
  defp serving_brick(ctx, node_id, pod_uid, instance_id) do
    NodeCapacity.put(ctx.cap_table, {node_id, pod_uid}, %{
      # configured_id is the NODE NAME (matching production + the serving_node
      # helper), not the instance_id: fleet placement returns
      # fact.configured_id as the node_id, which WakeInstance.cold_candidates then
      # matches against each brick's :node_id field. Setting it to instance_id here
      # would make that filter ("node-4" == "node-4/pod-a") fail and yield no
      # eligible instance, which is exactly the bug this fixture had.
      configured_id: node_id,
      node_id: node_id,
      pod_uid: pod_uid,
      instance_id: instance_id,
      size_class: "8gi",
      mem_headroom_mib: 8_000,
      mem_budget_mib: 8_192,
      serving_subnet_cidr: "10.99.0.0/24",
      max_live_vms: 4,
      live_vms: 0,
      workloads: %{
        "wl-a" => %{
          base_state: :BASE_BUILD_STATE_READY,
          snapshot_ref: "snap-a",
          serving_image_ref: "base-a"
        }
      },
      serving_vms: [],
      serving_snapshots: [],
      store_reachable: false
    })
  end

  # A StartServing stub that rejects the FIRST `reject_count` bricks DIALED with
  # RESOURCE_EXHAUSTED (status 8) and succeeds any later dial. Counter-based, NOT
  # identity-based: the frontier head is chosen by a workload hash (phash2), so
  # which co-located brick is dialed first is not knowable at test-authoring time;
  # rejecting by DIAL ORDER makes the retry fire regardless of the hash. Records
  # the dial order for assertions.
  defp rejecting_start_fun(reject_count) do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    fun = fn dial_id, _req ->
      n = Agent.get_and_update(dialed, fn ids -> {length(ids), ids ++ [dial_id]} end)

      if n < reject_count do
        {:error, %GRPC.RPCError{status: 8, message: "noded: pressure:mem"}}
      else
        {:ok, %StartServingResponse{vm_id: "vm-#{dial_id}", ip: "10.99.0.5", port: 8080}}
      end
    end

    {fun, dialed}
  end

  test "gate ON: first brick rejects under pressure -> second co-located brick serves" do
    System.put_env("EMBERVM_PLACEMENT_RETRY", "1")
    on_exit(fn -> System.delete_env("EMBERVM_PLACEMENT_RETRY") end)

    # Reject the FIRST brick dialed (whichever the hash picks as head), succeed the
    # second: the retry fires regardless of frontier order.
    {start_fun, dialed} = rejecting_start_fun(1)
    # channel echoes the dial_id so the stub sees which brick is dialed.
    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end)
    serving_workload(ctx, "wl-a")
    serving_brick(ctx, "node-4", "pod-a", "node-4/pod-a")
    serving_brick(ctx, "node-4", "pod-b", "node-4/pod-b")

    assert {:ok, %{ip: "10.99.0.5", port: 8080}} =
             ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")

    # BOTH bricks were dialed: the first rejected, the retry landed the second.
    order = Agent.get(dialed, & &1)
    assert length(order) == 2
    assert Enum.sort(order) == ["node-4/pod-a", "node-4/pod-b"]
    # The published instance is the one that succeeded (the non-rejecting brick).
    assert [instance] = ServingStore.list(ctx.store, "wl-a")
    assert instance.state == :published
  end

  test "gate ON: each retry candidate uses its own serving base ref" do
    {:ok, attempts} = Agent.start_link(fn -> [] end)

    start_fun = fn dial_id, req ->
      n = Agent.get_and_update(attempts, fn calls -> {length(calls), calls ++ [{dial_id, req}]} end)

      if n == 0 do
        {:error, %GRPC.RPCError{status: 8, message: "pressure:mem"}}
      else
        {:ok, %StartServingResponse{vm_id: "vm-#{dial_id}", ip: "10.99.0.5", port: 8080}}
      end
    end

    System.put_env("EMBERVM_PLACEMENT_RETRY", "1")
    on_exit(fn -> System.delete_env("EMBERVM_PLACEMENT_RETRY") end)
    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end)
    serving_workload(ctx, "wl-a")
    serving_node(ctx, "node-a", serving_image_ref: "vendor-a")
    serving_node(ctx, "node-b", serving_image_ref: "vendor-b")

    assert {:ok, %{ip: "10.99.0.5", port: 8080}} =
             ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")

    [{first_id, _first_req}, {second_id, second_req}] = Agent.get(attempts, & &1)
    second_node = if second_id == "node-a", do: "node-a", else: "node-b"
    expected_ref = if second_node == "node-a", do: "vendor-a", else: "vendor-b"
    assert second_req.source == {:fresh, %Embervm.Node.V1.FreshSource{serving_image_ref: expected_ref}}
    refute first_id == second_id
    assert [instance] = ServingStore.list(ctx.store, "wl-a")
    assert instance.node_id == second_node
  end

  test "gate OFF: a first-brick rejection is NOT retried (503, single attempt)" do
    # Gate unset (default off): the sole cold pick is attempted once; its
    # RESOURCE_EXHAUSTED becomes a wake failure, the second brick is never dialed.
    System.delete_env("EMBERVM_PLACEMENT_RETRY")

    # Reject every brick dialed (a high count) so the single gate-off attempt
    # rejects whichever head the hash picks.
    {start_fun, dialed} = rejecting_start_fun(99)
    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end)
    serving_workload(ctx, "wl-a")
    serving_brick(ctx, "node-4", "pod-a", "node-4/pod-a")
    serving_brick(ctx, "node-4", "pod-b", "node-4/pod-b")

    assert {:error, {:wake_failed, _}} =
             ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")

    # Exactly ONE dial: gate off never chased the second brick.
    assert length(Agent.get(dialed, & &1)) == 1
  end

  # -- in-flight claims and pinned demand ---------------------------------------

  # The BrickController demand casts the manager process sent while `fun` ran, as
  # {tag, workload, need}. Only the manager is traced.
  defp manager_demand_casts(ctx, fun) do
    :erlang.trace(ctx.mgr, true, [:send])
    result = fun.()
    :erlang.trace(ctx.mgr, false, [:send])
    ref = :erlang.trace_delivered(ctx.mgr)
    assert_receive {:trace_delivered, _, ^ref}

    casts =
      Stream.repeatedly(fn ->
        receive do
          {:trace, _, _kind, {:"$gen_cast", {tag, wl, need}}, _to} when tag in [:denial, :empty_demand] -> {tag, wl, need}
          {:trace, _, _kind, _msg, _to} -> :skip
        after
          0 -> :done
        end
      end)
      |> Enum.take_while(&(&1 != :done))
      |> Enum.reject(&(&1 == :skip))

    {result, casts}
  end

  # A classed brick with `headroom_mib` that has wl-a and wl-b's serving bases READY.
  defp sized_serving_brick(ctx, node_id, pod_uid, headroom_mib, opts \\ []) do
    ready = %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap", serving_image_ref: "base-a"}

    NodeCapacity.put(ctx.cap_table, {node_id, pod_uid}, %{
      configured_id: node_id,
      node_id: node_id,
      pod_uid: pod_uid,
      instance_id: "#{node_id}/#{pod_uid}",
      size_class: "4gi",
      mem_headroom_mib: headroom_mib,
      mem_budget_mib: 4_096,
      serving_subnet_cidr: "10.99.0.0/24",
      max_live_vms: 4,
      live_vms: Keyword.get(opts, :live_vms, 0),
      workloads: %{"wl-a" => ready, "wl-b" => ready},
      serving_vms: [],
      serving_snapshots: Keyword.get(opts, :serving_snapshots, []),
      store_reachable: Keyword.get(opts, :store_reachable, false)
    })
  end

  defp sized_serving_workload(ctx, name, mem_mib) do
    WorkloadCatalog.upsert(ctx.cat_table, name, %{
      class: "serving",
      mem_mib: mem_mib,
      serving: %{host: "#{name}.example", port: 8080, health_path: "/healthz", min_instances: 0, max_instances: 2}
    })
  end

  test "a cold create claims its brick before StartServing and settles the claim on success" do
    test = self()
    ledger = Inflight.create(:"sm_claim_#{System.unique_integer([:positive])}")

    start_fun = fn dial_id, _req ->
      send(test, {:pending_at_start, dial_id, Inflight.pending_mib(dial_id, table: ledger)})
      {:ok, %StartServingResponse{vm_id: "vm-claimed", ip: "10.99.0.5", port: 8080}}
    end

    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end, inflight_table: ledger)
    serving_workload(ctx, "wl-a")
    serving_brick(ctx, "node-4", "pod-a", "node-4/pod-a")

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    # The claim was in the ledger when the daemon was asked to boot.
    assert_received {:pending_at_start, "node-4/pod-a", 512}
    # Settled: it holds until the brick's next report counts the VM.
    assert Inflight.pending_mib("node-4/pod-a", table: ledger) == 512
  end

  test "a wake in flight debits its brick, so another workload's concurrent cold create places elsewhere" do
    test = self()

    start_fun = fn dial_id, req ->
      send(test, {:started, req.trace.workload, dial_id, self()})

      receive do
        :go -> {:ok, %StartServingResponse{vm_id: "vm-#{req.trace.workload}", ip: "10.99.0.5", port: 8080}}
      end
    end

    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end)
    sized_serving_workload(ctx, "wl-a", 3_000)
    sized_serving_workload(ctx, "wl-b", 3_000)
    # Two 4Gi bricks that each fit ONE 3000 MiB guest on the headroom they report.
    sized_serving_brick(ctx, "node-4", "pod-a", 4_000)
    sized_serving_brick(ctx, "node-4", "pod-b", 4_000)

    miss_a = Task.async(fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)
    assert_receive {:started, "wl-a", dial_a, worker_a}
    assert Inflight.pending_mib(dial_a, table: ctx.inflight_table) == 3_000

    # Neither brick has reported wl-a's VM; only the claim steers wl-b away.
    miss_b = Task.async(fn -> ServingManager.miss(ctx.mgr, "wl-b", req(), "serving:wl-b") end)
    assert_receive {:started, "wl-b", dial_b, worker_b}
    refute dial_b == dial_a

    send(worker_a, :go)
    send(worker_b, :go)
    assert {:ok, _} = Task.await(miss_a)
    assert {:ok, _} = Task.await(miss_b)
  end

  test "gate ON: a refused hop releases its claim and the brick that boots keeps one" do
    System.put_env("EMBERVM_PLACEMENT_RETRY", "1")
    on_exit(fn -> System.delete_env("EMBERVM_PLACEMENT_RETRY") end)

    {start_fun, dialed} = rejecting_start_fun(1)
    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end)
    serving_workload(ctx, "wl-a")
    serving_brick(ctx, "node-4", "pod-a", "node-4/pod-a")
    serving_brick(ctx, "node-4", "pod-b", "node-4/pod-b")

    assert {:ok, _} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    [refused, booted] = Agent.get(dialed, & &1)
    assert Inflight.pending_mib(refused, table: ctx.inflight_table) == 0
    assert Inflight.pending_mib(booted, table: ctx.inflight_table) == 512
  end

  test "a failed cold create leaves no claim behind" do
    {start_fun, _dialed} = rejecting_start_fun(99)
    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end)
    serving_workload(ctx, "wl-a")
    serving_brick(ctx, "node-4", "pod-a", "node-4/pod-a")

    assert {:error, {:wake_failed, _}} = ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")
    assert Inflight.entries(table: ctx.inflight_table) == []
  end

  test "a cold create the fleet cannot host records one denial" do
    ctx = start_stack()
    sized_serving_workload(ctx, "wl-a", 3_000)
    sized_serving_brick(ctx, "node-4", "pod-a", 1_000)

    assert {{:error, {:wake_failed, :no_capacity}}, [{:denial, "wl-a", 3_000}]} =
             manager_demand_casts(ctx, fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)
  end

  test "a restore-relight whose full anchor node is the only brick wakes cold, records demand once and claims nothing" do
    ctx = start_stack()
    sized_serving_workload(ctx, "wl-a", 3_000)
    # The banked snapshot's node reports a reachable store but no local snapshot, so
    # the wake plans a restore onto a cold pick ON THAT NODE, whose only brick is
    # full. The pinned pick records no demand; the unpinned cold create it falls
    # back to finds no room either, and only that miss records demand.
    sized_serving_brick(ctx, "node-4", "pod-a", 1_000, store_reachable: true)
    seed_banked(ctx, "srv-1", "base-a", "serving/s-1")

    assert {{:error, {:wake_failed, :no_capacity}}, [{:denial, "wl-a", 3_000}]} =
             manager_demand_casts(ctx, fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)

    assert Agent.get(ctx.starts, & &1) == 0
    assert {:ok, %{state: :banked, snapshot_ref: "serving/s-1"}} = ServingStore.get(ctx.store, "srv-1")
    assert Inflight.entries(table: ctx.inflight_table) == []
  end

  # -- a full owning brick wakes cold ------------------------------------------

  @s1_snapshot [%{snapshot_ref: "serving/s-1", workload: "wl-a", size_bytes: 10, created_at_unix_ms: 1}]

  # A StartServing stub that refuses every boot on the brick dialled as
  # `full_dial` with RESOURCE_EXHAUSTED, as noded does when that brick cannot fit
  # the guest, and boots anywhere else. Reports each dial and its source kind.
  defp full_brick_start_fun(full_dial) do
    test = self()

    fn dial_id, req ->
      send(test, {:start, dial_id, elem(req.source, 0)})

      if dial_id == full_dial do
        {:error, %GRPC.RPCError{status: 8, message: "noded: pressure:mem"}}
      else
        {:ok, %StartServingResponse{vm_id: "vm-#{dial_id}", ip: "10.99.0.5", port: 8080}}
      end
    end
  end

  defp woken_instances(ctx), do: Enum.reject(ServingStore.list(ctx.store, "wl-a"), &(&1.instance_id == "srv-1"))

  test "a relight its owning brick refuses wakes cold on another brick and keeps the snapshot banked" do
    ctx = start_stack(start_serving_fun: full_brick_start_fun("node-4/pod-a"), channel_fun: fn dial_id -> {:ok, dial_id} end)
    sized_serving_workload(ctx, "wl-a", 3_000)
    # The owner's last report fits the guest, but a racing boot has taken the room
    # by the time the relight lands. It is also the fuller brick, so a cold
    # placement that did not skip it would dial it first and be refused again.
    sized_serving_brick(ctx, "node-4", "pod-a", 3_600, serving_snapshots: @s1_snapshot)
    sized_serving_brick(ctx, "node-5", "pod-b", 4_000)
    seed_banked(ctx, "srv-1", "base-a", "serving/s-1")

    assert {{:ok, %{ip: "10.99.0.5", port: 8080}}, []} =
             manager_demand_casts(ctx, fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)

    assert_received {:start, "node-4/pod-a", :relight}
    assert_received {:start, "node-5/pod-b", :fresh}
    refute_received {:start, _, _}

    # The snapshot stays banked for a later relight, and the cold boot serves.
    assert {:ok, %{state: :banked, snapshot_ref: "serving/s-1"}} = ServingStore.get(ctx.store, "srv-1")
    assert [%{state: :published, node_id: "node-5"}] = woken_instances(ctx)
    assert Inflight.pending_mib("node-4/pod-a", table: ctx.inflight_table) == 0
    assert Inflight.pending_mib("node-5/pod-b", table: ctx.inflight_table) == 3_000
  end

  test "a banked snapshot on a brick that cannot fit the guest wakes cold without a relight" do
    ctx = start_stack(start_serving_fun: full_brick_start_fun("node-4/pod-a"), channel_fun: fn dial_id -> {:ok, dial_id} end)
    sized_serving_workload(ctx, "wl-a", 3_000)
    sized_serving_brick(ctx, "node-4", "pod-a", 1_000, serving_snapshots: @s1_snapshot)
    sized_serving_brick(ctx, "node-5", "pod-b", 4_000)
    seed_banked(ctx, "srv-1", "base-a", "serving/s-1")

    assert {{:ok, _endpoint}, []} =
             manager_demand_casts(ctx, fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)

    assert_received {:start, "node-5/pod-b", :fresh}
    refute_received {:start, _, _}
    assert {:ok, %{state: :banked}} = ServingStore.get(ctx.store, "srv-1")
    assert [%{state: :published, node_id: "node-5"}] = woken_instances(ctx)
  end

  test "a restore-relight whose anchor node is full wakes cold on another brick and keeps the snapshot banked" do
    {:ok, restore_calls} = Agent.start_link(fn -> [] end)

    restore_fun = fn _ch, req ->
      Agent.update(restore_calls, &[req | &1])
      {:error, :unused}
    end

    ctx =
      start_stack(
        start_serving_fun: full_brick_start_fun("node-4/pod-a"),
        channel_fun: fn dial_id -> {:ok, dial_id} end,
        restore_artifact_fun: restore_fun
      )

    sized_serving_workload(ctx, "wl-a", 3_000)
    # The snapshot's anchor node reports a reachable store but no local snapshot, so
    # the wake plans a restore onto that node, whose only brick cannot fit the guest.
    sized_serving_brick(ctx, "node-4", "pod-a", 1_000, store_reachable: true)
    sized_serving_brick(ctx, "node-5", "pod-b", 4_000)
    seed_banked(ctx, "srv-1", "base-a", "serving/s-1")

    assert {{:ok, %{ip: "10.99.0.5", port: 8080}}, []} =
             manager_demand_casts(ctx, fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)

    # No restore onto the full node: the same wake booted fresh on the brick with room.
    assert Agent.get(restore_calls, & &1) == []
    assert_received {:start, "node-5/pod-b", :fresh}
    refute_received {:start, _, _}
    assert {:ok, %{state: :banked, snapshot_ref: "serving/s-1"}} = ServingStore.get(ctx.store, "srv-1")
    assert [%{state: :published, node_id: "node-5"}] = woken_instances(ctx)
    assert Inflight.pending_mib("node-4/pod-a", table: ctx.inflight_table) == 0
    assert Inflight.pending_mib("node-5/pod-b", table: ctx.inflight_table) == 3_000
  end

  test "a refused relight with nowhere else to boot records demand once and fails the wake" do
    ctx = start_stack(start_serving_fun: full_brick_start_fun("node-4/pod-a"), channel_fun: fn dial_id -> {:ok, dial_id} end)
    sized_serving_workload(ctx, "wl-a", 3_000)
    sized_serving_brick(ctx, "node-4", "pod-a", 4_000, serving_snapshots: @s1_snapshot)
    seed_banked(ctx, "srv-1", "base-a", "serving/s-1")

    assert {{:error, {:wake_failed, :no_capacity}}, [{:denial, "wl-a", 3_000}]} =
             manager_demand_casts(ctx, fn -> ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a") end)

    assert_received {:start, "node-4/pod-a", :relight}
    refute_received {:start, _, _}
    assert {:ok, %{state: :banked, snapshot_ref: "serving/s-1"}} = ServingStore.get(ctx.store, "srv-1")
    assert woken_instances(ctx) == []
    assert Inflight.entries(table: ctx.inflight_table) == []
  end
end
