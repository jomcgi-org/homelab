defmodule Embervm.ServingStoreTest do
  @moduledoc """
  Exercises Embervm.ServingStore against a real (unnamed) Embervm.OpLog.SQLite on
  a fresh temp file per test, mirroring the SessionStore test idiom. Proves the
  write-through discipline (op-log before ETS), the publish/unpublish endpoint
  facts, health flips, per-workload counts, the published_endpoints stable
  ordering the publisher relies on, adoption from node truth, and boot-rebuild
  equivalence from the durable projection (the property the publisher's
  byte-identical-rebuild depends on).
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.SQLite
  alias Embervm.ServingStore

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_servingstore_test_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp start_pair(path, opts \\ []) do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    clock = Keyword.get(opts, :clock, sequential_clock())
    {:ok, store} = ServingStore.start_link(op_log: op_log, name: nil, clock: clock)
    {op_log, store}
  end

  defp sequential_clock do
    {:ok, counter} = Agent.start_link(fn -> 1_000 end)
    fn -> Agent.get_and_update(counter, fn n -> {n, n + 1} end) end
  end

  defp start_instance(store, opts \\ []) do
    ServingStore.start(store, %{
      instance_id: Keyword.get(opts, :instance_id, "srv-1"),
      tenant: "homelab",
      principal: Keyword.get(opts, :principal, "p1"),
      workload: Keyword.get(opts, :workload, "wl-a"),
      node_id: Keyword.get(opts, :node_id, "node-4"),
      vm_id: Keyword.get(opts, :vm_id, "vm-1"),
      ip: Keyword.get(opts, :ip, "10.99.0.5"),
      port: Keyword.get(opts, :port, 8080),
      base_snapshot_ref: "base:img@sha256:abc",
      base_digest: "sha256:abc"
    })
  end

  # -- start -----------------------------------------------------------------

  test "start appends serving_started and inserts a starting (not-yet-published) row", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, instance} = start_instance(store)

    assert instance.instance_id == "srv-1"
    assert instance.state == :starting
    refute instance.healthy
    assert instance.ip == "10.99.0.5"

    {:ok, got} = ServingStore.get(store, "srv-1")
    assert got.state == :starting

    # Write-through: the durable projection agrees.
    {:ok, [row]} = SQLite.load_serving_instances(op_log)
    assert row.instance_id == "srv-1"
    assert row.state == "starting"
    assert row.workload == "wl-a"

    # A starting instance is live (holds a VM) but not in the fan-out yet.
    assert ServingStore.counts(store, "wl-a") == %{live: 1, banked: 0}
    assert ServingStore.published_endpoints(store, "wl-a") == []
  end

  # -- publish / unpublish + the fan-out fact ---------------------------------

  test "publish moves starting -> published, marks healthy, and enters the fan-out", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)

    {:ok, published} = ServingStore.publish(store, "srv-1", "10.99.0.9", 9000, :started)
    assert published.state == :published
    assert published.healthy
    assert published.ip == "10.99.0.9"
    assert published.port == 9000

    assert ServingStore.published_endpoints(store, "wl-a") == [%{ip: "10.99.0.9", port: 9000}]
    assert ServingStore.serving_workloads(store) == ["wl-a"]

    # The durable audit record shows published with the endpoint.
    {:ok, [row]} = SQLite.load_serving_instances(op_log)
    assert row.state == "published"
    assert row.ip == "10.99.0.9"
    assert row.port == 9000
  end

  test "unpublish moves published -> draining and leaves the fan-out", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    {:ok, _} = ServingStore.publish(store, "srv-1", "10.99.0.9", 9000, :started)

    {:ok, drained} = ServingStore.unpublish(store, "srv-1", :drain)
    assert drained.state == :draining
    refute drained.healthy

    assert ServingStore.published_endpoints(store, "wl-a") == []
    # Still a live instance (draining holds a VM), just not published.
    assert ServingStore.counts(store, "wl-a") == %{live: 1, banked: 0}
  end

  test "an unhealthy published instance is not in the fan-out even before unpublish", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    {:ok, _} = ServingStore.publish(store, "srv-1", "10.99.0.9", 9000, :started)

    assert ServingStore.published_endpoints(store, "wl-a") == [%{ip: "10.99.0.9", port: 9000}]

    # Node health probe flips it unhealthy (no FSM transition, no op): it drops out
    # of the fan-out fact immediately.
    {:ok, _} = ServingStore.set_health(store, "srv-1", false)
    assert ServingStore.published_endpoints(store, "wl-a") == []

    # Recovery restores it.
    {:ok, _} = ServingStore.set_health(store, "srv-1", true)
    assert ServingStore.published_endpoints(store, "wl-a") == [%{ip: "10.99.0.9", port: 9000}]
  end

  # -- published_endpoints ordering (the byte-identical-rebuild property) -----

  test "published_endpoints is ordered by instance_id, deterministically", %{path: path} do
    {_op_log, store} = start_pair(path)
    # Insert out of id order to prove the sort.
    {:ok, _} = start_instance(store, instance_id: "srv-c", vm_id: "vm-c", ip: "10.0.0.3")
    {:ok, _} = start_instance(store, instance_id: "srv-a", vm_id: "vm-a", ip: "10.0.0.1")
    {:ok, _} = start_instance(store, instance_id: "srv-b", vm_id: "vm-b", ip: "10.0.0.2")

    {:ok, _} = ServingStore.publish(store, "srv-c", "10.0.0.3", 8080, :started)
    {:ok, _} = ServingStore.publish(store, "srv-a", "10.0.0.1", 8080, :started)
    {:ok, _} = ServingStore.publish(store, "srv-b", "10.0.0.2", 8080, :started)

    assert ServingStore.published_endpoints(store, "wl-a") == [
             %{ip: "10.0.0.1", port: 8080},
             %{ip: "10.0.0.2", port: 8080},
             %{ip: "10.0.0.3", port: 8080}
           ]
  end

  # -- transitions + counts ---------------------------------------------------

  test "bank (draining -> banking -> banked) clears the endpoint and moves to banked count", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    {:ok, _} = ServingStore.publish(store, "srv-1", "10.99.0.9", 9000, :started)
    {:ok, _} = ServingStore.unpublish(store, "srv-1", :banked)

    # Transient banking marker (ETS-only), then the durable serving_banked.
    {:ok, banking} = ServingStore.mark(store, "srv-1", :bank)
    assert banking.state == :banking
    assert ServingStore.counts(store, "wl-a") == %{live: 1, banked: 0}

    {:ok, banked} =
      ServingStore.transition(
        store,
        "srv-1",
        :bank_ready,
        :serving_banked,
        %{snapshot_ref: "serving/s-1", size_bytes: 2_000_000, generation: 1},
        %{snapshot_ref: "serving/s-1", snapshot_size_bytes: 2_000_000, generation: 1}
      )

    assert banked.state == :banked
    assert banked.snapshot_ref == "serving/s-1"
    assert ServingStore.counts(store, "wl-a") == %{live: 0, banked: 1}
    # A banked instance is not a serving workload's LIVE set member.
    assert ServingStore.serving_workloads(store) == []
  end

  test "an illegal transition is rejected and does not append or mutate", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)

    # starting has no :unpublish edge.
    assert {:error, {:illegal_transition, :starting, :unpublish}} =
             ServingStore.unpublish(store, "srv-1", :drain)

    {:ok, got} = ServingStore.get(store, "srv-1")
    assert got.state == :starting

    # Only the serving_started op was ever appended.
    {:ok, [row]} = SQLite.load_serving_instances(op_log)
    assert row.state == "starting"
  end

  test "a terminal transition records the reason and drops the endpoint", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    {:ok, _} = ServingStore.publish(store, "srv-1", "10.99.0.9", 9000, :started)

    {:ok, destroyed} =
      ServingStore.transition(store, "srv-1", :destroy, :serving_destroyed, %{reason: "deleted"}, %{})

    assert destroyed.state == :destroyed
    assert destroyed.terminal_reason == "deleted"
    refute destroyed.healthy
    assert destroyed.ip == nil
    assert ServingStore.published_endpoints(store, "wl-a") == []
    assert ServingStore.counts(store, "wl-a") == %{live: 0, banked: 0}
  end

  # -- adoption ---------------------------------------------------------------

  test "adopt_state forces ETS from node truth without an op, skipping terminal rows", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)

    # Adopt a live starting instance to published (node reports a live serving VM).
    :ok = ServingStore.adopt_state(store, "srv-1", :published)
    {:ok, got} = ServingStore.get(store, "srv-1")
    assert got.state == :published

    # No op was appended by adoption (only the original serving_started).
    {:ok, [row]} = SQLite.load_serving_instances(op_log)
    assert row.state == "starting"

    # A terminal instance is never resurrected by adoption.
    {:ok, _} =
      ServingStore.transition(store, "srv-1", :destroy, :serving_destroyed, %{reason: "deleted"}, %{})

    :ok = ServingStore.adopt_state(store, "srv-1", :published)
    {:ok, still} = ServingStore.get(store, "srv-1")
    assert still.state == :destroyed
  end

  test "adopt_activator mints a published row from node truth; backfill_created makes it durable and idempotent",
       %{path: path} do
    {op_log, store} = start_pair(path)

    # ADR embervm/018 Fork A: the brick woke a VM during a CP gap, so NO CP row
    # exists. Mint it from node truth (ETS-only, already published/in the fan-out).
    :ok =
      ServingStore.adopt_activator(store, %{
        instance_id: "vm-brick-1",
        tenant: "homelab",
        principal: nil,
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-brick-1",
        ip: "10.99.0.7",
        port: 8080
      })

    {:ok, got} = ServingStore.get(store, "vm-brick-1")
    assert got.state == :published
    assert ServingStore.published_endpoints(store, "wl-a") == [%{ip: "10.99.0.7", port: 8080}]
    # Nothing durable yet: adoption is ETS-only, like adopt_state/adopt_endpoint.
    assert {:ok, []} = SQLite.load_serving_instances(op_log)

    # backfill_created writes the durable serving_started + serving_published.
    assert :ok = ServingStore.backfill_created(store, "vm-brick-1")
    {:ok, [row]} = SQLite.load_serving_instances(op_log)
    assert row.state == "published"

    # Idempotent (INSERT OR IGNORE): a re-drive leaves exactly the same durable row.
    assert :ok = ServingStore.backfill_created(store, "vm-brick-1")
    {:ok, [row2]} = SQLite.load_serving_instances(op_log)
    assert row2.state == "published"

    # A NEW store over the SAME op-log recovers the published endpoint from the
    # backfilled projection (the durable record now matches the live fan-out).
    {:ok, store2} = ServingStore.start_link(op_log: op_log, name: nil, clock: sequential_clock())
    assert ServingStore.published_endpoints(store2, "wl-a") == [%{ip: "10.99.0.7", port: 8080}]

    # adopt_activator is a no-op once a row exists (the mint is strictly once-only).
    :ok = ServingStore.adopt_activator(store, %{instance_id: "vm-brick-1", tenant: "homelab", workload: "wl-a"})
    {:ok, still} = ServingStore.get(store, "vm-brick-1")
    assert still.state == :published

    # An unknown instance backfills to an error (never a silent success).
    assert {:error, _} = ServingStore.backfill_created(store, "nope")
  end

  test "adopt_endpoint rebinds the routable endpoint without an op", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    :ok = ServingStore.adopt_state(store, "srv-1", :published)

    :ok =
      ServingStore.adopt_endpoint(store, "srv-1", "node-4", "vm-live", %{
        ip: "10.99.0.42",
        port: 8080,
        healthy: true
      })

    assert ServingStore.published_endpoints(store, "wl-a") == [%{ip: "10.99.0.42", port: 8080}]
  end

  # -- boot rebuild equivalence ----------------------------------------------

  test "a fresh store rebuilt from the projection matches the pre-restart facts", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, _} = start_instance(store, instance_id: "srv-1", workload: "wl-a", vm_id: "vm-1", ip: "10.0.0.1")
    {:ok, _} = ServingStore.publish(store, "srv-1", "10.0.0.1", 8080, :started)

    {:ok, _} = start_instance(store, instance_id: "srv-2", workload: "wl-b", vm_id: "vm-2", ip: "10.0.0.2")
    {:ok, _} = ServingStore.publish(store, "srv-2", "10.0.0.2", 8080, :started)
    {:ok, _} = ServingStore.unpublish(store, "srv-2", :banked)
    {:ok, _} = ServingStore.mark(store, "srv-2", :bank)

    {:ok, _} =
      ServingStore.transition(
        store,
        "srv-2",
        :bank_ready,
        :serving_banked,
        %{snapshot_ref: "serving/s-2", size_bytes: 1_000_000, generation: 1},
        %{snapshot_ref: "serving/s-2", snapshot_size_bytes: 1_000_000, generation: 1}
      )

    # Restart: a NEW store over the SAME op-log rebuilds from the durable projection.
    {:ok, store2} = ServingStore.start_link(op_log: op_log, name: nil, clock: sequential_clock())

    # The published wl-a endpoint is byte-identical after rebuild (healthy assumed
    # for a rebuilt published row, so it re-enters the fan-out with the same fact).
    assert ServingStore.published_endpoints(store2, "wl-a") == [%{ip: "10.0.0.1", port: 8080}]
    # The banked wl-b instance rebuilt as banked (transient banking never persisted).
    assert ServingStore.counts(store2, "wl-b") == %{live: 0, banked: 1}
    {:ok, srv2} = ServingStore.get(store2, "srv-2")
    assert srv2.state == :banked
    assert srv2.snapshot_ref == "serving/s-2"
  end
end
