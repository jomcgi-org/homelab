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

  alias Embervm.{NodeCapacity, ServingManager, ServingStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.StartServingResponse

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

    start_serving_fun =
      Keyword.get(opts, :start_serving_fun, fn _ch, _req ->
        Agent.update(starts, &(&1 + 1))
        if sleep = opts[:start_sleep_ms], do: Process.sleep(sleep)
        {:ok, %StartServingResponse{vm_id: "vm-woken-#{System.unique_integer([:positive])}", ip: "10.99.0.5", port: 8080}}
      end)

    mgr_opts =
      [
        name: nil,
        store: store,
        publisher: pub,
        capacity_table: cap_table,
        catalog_table: cat_table,
        clock: Keyword.get(opts, :clock, fn -> 1_000 end),
        # Default echoes nothing (a fixed channel); a retry test overrides this to
        # echo the dial_id so its StartServing stub can reject a SPECIFIC brick.
        channel_fun: Keyword.get(opts, :channel_fun, fn _node -> {:ok, :ch} end),
        invalidate_fun: Keyword.get(opts, :invalidate_fun, fn _node, _chan -> :ok end),
        start_serving_fun: start_serving_fun,
        op_log: op_log,
        reconcile_interval_ms: 0,
        id_fun: id_seq(suffix)
      ] ++ Keyword.take(opts, [:wake_max, :wake_window_ms, :park_cap, :restore_artifact_fun])

    {:ok, mgr} = ServingManager.start_link(mgr_opts)

    %{mgr: mgr, store: store, cap_table: cap_table, cat_table: cat_table, starts: starts, pub: pub, op_log: op_log}
  end

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
          serving_image_ref: "base-a"
        }
      },
      serving_vms: Keyword.get(opts, :serving_vms, []),
      serving_snapshots: Keyword.get(opts, :serving_snapshots, []),
      store_reachable: Keyword.get(opts, :store_reachable, false)
    })
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
      # helper), not the instance_id: ServingPlacement.node_for_create returns
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

  # A StartServing stub keyed on the dialed instance (the channel echoes the
  # dial_id): each id in `reject_ids` returns RESOURCE_EXHAUSTED (status 8), any
  # other id succeeds. Records the dial order for assertions.
  defp rejecting_start_fun(reject_ids) do
    {:ok, dialed} = Agent.start_link(fn -> [] end)

    fun = fn dial_id, _req ->
      Agent.update(dialed, fn ids -> ids ++ [dial_id] end)

      if dial_id in reject_ids do
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

    {start_fun, dialed} = rejecting_start_fun(["node-4/pod-a"])
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

  test "gate OFF: a first-brick rejection is NOT retried (503, single attempt)" do
    # Gate unset (default off): the sole cold pick is attempted once; its
    # RESOURCE_EXHAUSTED becomes a wake failure, the second brick is never dialed.
    System.delete_env("EMBERVM_PLACEMENT_RETRY")

    {start_fun, dialed} = rejecting_start_fun(["node-4/pod-a", "node-4/pod-b"])
    ctx = start_stack(start_serving_fun: start_fun, channel_fun: fn dial_id -> {:ok, dial_id} end)
    serving_workload(ctx, "wl-a")
    serving_brick(ctx, "node-4", "pod-a", "node-4/pod-a")
    serving_brick(ctx, "node-4", "pod-b", "node-4/pod-b")

    assert {:error, {:wake_failed, _}} =
             ServingManager.miss(ctx.mgr, "wl-a", req(), "serving:wl-a")

    # Exactly ONE dial: gate off never chased the second brick.
    assert length(Agent.get(dialed, & &1)) == 1
  end
end
