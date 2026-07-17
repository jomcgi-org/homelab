defmodule Embervm.StatefulStoreTest do
  @moduledoc """
  Exercises Embervm.StatefulStore against a real (unnamed) Embervm.OpLog.SQLite on
  a fresh temp file per test, mirroring the ServingStore test idiom. Proves the
  write-through discipline (op-log before ETS), the singleton invariant, the
  publish/unpublish/bank/relight/cold-boot sequences, the single-endpoint fact,
  per-workload counts, boot-rebuild equivalence from the durable projection, and
  the pairing + eager-eviction primitives (snapshot_generation vs volume.generation).
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.SQLite
  alias Embervm.StatefulStore

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_statefulstore_test_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp start_pair(path, opts \\ []) do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    clock = Keyword.get(opts, :clock, sequential_clock())
    {:ok, store} = StatefulStore.start_link(op_log: op_log, name: nil, clock: clock)
    {op_log, store}
  end

  defp sequential_clock do
    {:ok, counter} = Agent.start_link(fn -> 1_000 end)
    fn -> Agent.get_and_update(counter, fn n -> {n, n + 1} end) end
  end

  defp start_instance(store, opts \\ []) do
    StatefulStore.start(store, %{
      instance_id: Keyword.get(opts, :instance_id, "sf-1"),
      tenant: "homelab",
      principal: Keyword.get(opts, :principal, "p1"),
      workload: Keyword.get(opts, :workload, "wl-a"),
      node_id: Keyword.get(opts, :node_id, "node-4"),
      vm_id: Keyword.get(opts, :vm_id, "vm-1"),
      generation: Keyword.get(opts, :generation, 0)
    })
  end

  # Drive a live instance all the way to `banked`, stamping the given snapshot
  # generation into the bundle (the pair key). Returns the banked instance.
  defp bank(store, instance_id, workload, snapshot_generation) do
    {:ok, _} = StatefulStore.publish(store, instance_id, "10.99.0.9", 9000, :started)
    {:ok, _} = StatefulStore.unpublish(store, instance_id, :bank)

    {:ok, banked} =
      StatefulStore.transition(
        store,
        instance_id,
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: "stateful/#{workload}/s", size_bytes: 2_000_000, generation: snapshot_generation},
        %{snapshot_ref: "stateful/#{workload}/s", snapshot_size_bytes: 2_000_000, snapshot_generation: snapshot_generation}
      )

    banked
  end

  # -- start + the singleton invariant ---------------------------------------

  test "start appends stateful_started and inserts a starting (not-yet-published) row", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, instance} = start_instance(store, generation: 3)

    assert instance.instance_id == "sf-1"
    assert instance.state == :starting
    refute instance.healthy
    assert instance.generation == 3
    assert instance.ip == nil

    {:ok, got} = StatefulStore.get(store, "sf-1")
    assert got.state == :starting

    # Write-through: the durable projection agrees.
    {:ok, [row]} = SQLite.load_stateful_instances(op_log)
    assert row.instance_id == "sf-1"
    assert row.state == "starting"
    assert row.workload == "wl-a"

    assert StatefulStore.counts(store) == %{"wl-a" => %{live: 1, banked: 0}}
    assert StatefulStore.published_endpoint(store, "wl-a") == nil
  end

  test "start refuses a second LIVE instance for a workload without appending an op", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store, instance_id: "sf-1")

    assert {:error, :already_live} = start_instance(store, instance_id: "sf-2")

    # Only the first start was ever appended.
    {:ok, rows} = SQLite.load_stateful_instances(op_log)
    assert length(rows) == 1
    assert StatefulStore.counts(store) == %{"wl-a" => %{live: 1, banked: 0}}
  end

  test "a banked-only workload is NOT blocked from a fresh cold-boot start", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store, instance_id: "sf-1")
    _banked = bank(store, "sf-1", "wl-a", 1)

    # The banked instance holds no live VM, so a fresh cold boot may start.
    assert {:ok, cold} = start_instance(store, instance_id: "sf-2", generation: 2)
    assert cold.state == :starting
    assert StatefulStore.counts(store) == %{"wl-a" => %{live: 1, banked: 1}}
  end

  # -- publish / unpublish + the single-endpoint fact -------------------------

  test "publish moves starting -> serving, marks healthy, and enters the fan-out", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)

    {:ok, published} = StatefulStore.publish(store, "sf-1", "10.99.0.9", 9000, :started)
    assert published.state == :serving
    assert published.healthy
    assert published.ip == "10.99.0.9"
    assert published.port == 9000

    assert StatefulStore.published_endpoint(store, "wl-a") == %{ip: "10.99.0.9", port: 9000}

    # The durable audit record shows serving with the endpoint.
    {:ok, [row]} = SQLite.load_stateful_instances(op_log)
    assert row.state == "serving"
    assert row.ip == "10.99.0.9"
    assert row.port == 9000
  end

  test "unpublish moves serving -> banking ETS-only, leaves the fan-out, appends NO op", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    {:ok, _} = StatefulStore.publish(store, "sf-1", "10.99.0.9", 9000, :started)

    {:ok, banking} = StatefulStore.unpublish(store, "sf-1", :bank)
    assert banking.state == :banking
    refute banking.healthy
    assert banking.ip == nil

    assert StatefulStore.published_endpoint(store, "wl-a") == nil
    # Still a live instance (banking holds a VM), just not serving.
    assert StatefulStore.counts(store) == %{"wl-a" => %{live: 1, banked: 0}}

    # unpublish is ETS-only: the durable projection still shows serving (the last
    # durable op was stateful_published). No stateful_unpublished op was appended.
    {:ok, [row]} = SQLite.load_stateful_instances(op_log)
    assert row.state == "serving"
  end

  test "an unhealthy serving instance is not in the fan-out", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    {:ok, _} = StatefulStore.publish(store, "sf-1", "10.99.0.9", 9000, :started)

    assert StatefulStore.published_endpoint(store, "wl-a") == %{ip: "10.99.0.9", port: 9000}

    {:ok, _} = StatefulStore.set_health(store, "sf-1", false)
    assert StatefulStore.published_endpoint(store, "wl-a") == nil

    {:ok, _} = StatefulStore.set_health(store, "sf-1", true)
    assert StatefulStore.published_endpoint(store, "wl-a") == %{ip: "10.99.0.9", port: 9000}
  end

  # -- bank / relight / cold-boot sequences -----------------------------------

  test "bank (serving -> banking -> banked) clears the endpoint and moves to banked count", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)

    banked = bank(store, "sf-1", "wl-a", 1)

    assert banked.state == :banked
    assert banked.snapshot_ref == "stateful/wl-a/s"
    assert banked.snapshot_generation == 1
    assert banked.ip == nil
    refute banked.healthy
    assert StatefulStore.published_endpoint(store, "wl-a") == nil
    assert StatefulStore.counts(store) == %{"wl-a" => %{live: 0, banked: 1}}
  end

  test "relight (banked -> relighting -> starting) returns a live starting instance", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store, generation: 0)
    _banked = bank(store, "sf-1", "wl-a", 1)

    # WARM wake: transient relighting marker (ETS-only), then durable stateful_relit.
    {:ok, relighting} = StatefulStore.mark(store, "sf-1", :relight)
    assert relighting.state == :relighting
    assert StatefulStore.counts(store) == %{"wl-a" => %{live: 1, banked: 0}}

    {:ok, relit} =
      StatefulStore.transition(
        store,
        "sf-1",
        :relight_ready,
        :stateful_relit,
        %{node_id: "node-4", vm_id: "vm-relit", generation: 2},
        %{node_id: "node-4", vm_id: "vm-relit", generation: 2, snapshot_ref: nil, snapshot_generation: nil}
      )

    assert relit.state == :starting
    assert relit.generation == 2
    # The bundle is spent by the relight (its stamped generation is stale).
    assert relit.snapshot_generation == nil

    # Durable projection agrees: starting, post-bump generation, cleared bundle.
    {:ok, [row]} = SQLite.load_stateful_instances(op_log)
    assert row.state == "starting"
    assert row.generation == 2
    assert row.snapshot_generation == nil
  end

  test "cold_boot/2 records a NEW instance via stateful_cold_booted carrying the reason", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store, generation: 0)
    _banked = bank(store, "sf-1", "wl-a", 1)

    # A wake that discarded warmth cold-boots a NEW instance (the old banked one is
    # evicted separately by the wake path); cold_boot/2 records the new lifecycle as
    # stateful_cold_booted{reason} rather than a plain stateful_started (gate 2). The
    # banked sf-1 is not live, so the singleton gate admits the new cold boot.
    {:ok, cold} =
      StatefulStore.cold_boot(store, %{
        instance_id: "sf-2",
        tenant: "t1",
        principal: "p",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: "vm-cold",
        generation: 2,
        reason: "generation_mismatch"
      })

    assert cold.instance_id == "sf-2"
    assert cold.state == :starting
    assert cold.generation == 2

    # The discarded-warmth reason is reconstructable from the op payload's string key.
    {:ok, ops} = SQLite.read_from(op_log, 0)
    cold_op = Enum.find(ops, &(&1.kind == :stateful_cold_booted))
    assert cold_op.stateful_instance_id == "sf-2"
    assert cold_op.payload["reason"] == "generation_mismatch"
  end

  # -- terminal + illegal -----------------------------------------------------

  test "an illegal transition is rejected and does not append or mutate", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)

    # starting has no :unpublish edge.
    assert {:error, {:illegal_transition, :starting, :unpublish}} =
             StatefulStore.unpublish(store, "sf-1", :bank)

    {:ok, got} = StatefulStore.get(store, "sf-1")
    assert got.state == :starting

    {:ok, [row]} = SQLite.load_stateful_instances(op_log)
    assert row.state == "starting"
  end

  test "a terminal transition records the reason and drops the endpoint", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store)
    {:ok, _} = StatefulStore.publish(store, "sf-1", "10.99.0.9", 9000, :started)

    {:ok, destroyed} =
      StatefulStore.transition(store, "sf-1", :destroy, :stateful_destroyed, %{reason: "deleted"}, %{})

    assert destroyed.state == :destroyed
    assert destroyed.terminal_reason == "deleted"
    refute destroyed.healthy
    assert destroyed.ip == nil
    assert StatefulStore.published_endpoint(store, "wl-a") == nil
    assert StatefulStore.counts(store) == %{"wl-a" => %{live: 0, banked: 0}}
  end

  # -- pairing ----------------------------------------------------------------

  test "pair_valid? is true when snapshot_generation == volume.generation", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store, generation: 0)
    _banked = bank(store, "sf-1", "wl-a", 5)

    # Set the volume's current generation to MATCH the stamped bundle generation.
    _ = StatefulStore.upsert_volume(store, "wl-a", %{generation: 5, allocated_bytes: 12_345})
    assert StatefulStore.pair_valid?(store, "wl-a")
  end

  test "a boot bumps the volume pair-key in real time so a same-generation bank pairs without a reconcile", %{path: path} do
    {_op_log, store} = start_pair(path)
    # A cold/fresh boot at generation 7 must set the volume pair-key to 7
    # IMMEDIATELY (not only on the next refresh_volume_facts reconcile). Before the
    # fix the ETS volume row lagged the freshly-banked bundle, so a workload that
    # banks faster than the reconcile interval (demo-postgres, idleBankSeconds:1)
    # never paired and cold-booted forever. No upsert_volume / reconcile here.
    {:ok, _} = start_instance(store, generation: 7)
    _ = bank(store, "sf-1", "wl-a", 7)
    assert StatefulStore.pair_valid?(store, "wl-a")
  end

  test "pair_valid? is false when the generations diverge", %{path: path} do
    {_op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store, generation: 0)
    _banked = bank(store, "sf-1", "wl-a", 5)

    _ = StatefulStore.upsert_volume(store, "wl-a", %{generation: 6})
    refute StatefulStore.pair_valid?(store, "wl-a")
  end

  test "pair_valid? is false when either side is missing", %{path: path} do
    {_op_log, store} = start_pair(path)

    # No banked instance and no volume: false.
    refute StatefulStore.pair_valid?(store, "wl-a")

    # A banked instance but no volume row: still false (no current generation).
    {:ok, _} = start_instance(store, generation: 0)
    _banked = bank(store, "sf-1", "wl-a", 5)
    refute StatefulStore.pair_valid?(store, "wl-a")

    # A volume but no banked instance (a different workload with only a volume row):
    # still false (no bundle to pair).
    _ = StatefulStore.upsert_volume(store, "wl-b", %{generation: 1})
    refute StatefulStore.pair_valid?(store, "wl-b")
  end

  # -- eager eviction of broken pairs -----------------------------------------

  test "eager_evict_broken_pairs evicts a pair-broken banked instance with reason pair_broken", %{path: path} do
    {op_log, store} = start_pair(path)
    {:ok, _} = start_instance(store, workload: "wl-a", instance_id: "sf-a", generation: 0)
    {:ok, _} = start_instance(store, workload: "wl-b", instance_id: "sf-b", generation: 0)

    _ = bank(store, "sf-a", "wl-a", 3)
    _ = bank(store, "sf-b", "wl-b", 7)

    # wl-a's pair is VALID (volume gen == bundle gen), wl-b's is BROKEN.
    _ = StatefulStore.upsert_volume(store, "wl-a", %{generation: 3})
    _ = StatefulStore.upsert_volume(store, "wl-b", %{generation: 8})

    evicted = StatefulStore.eager_evict_broken_pairs(store)
    assert evicted == ["sf-b"]

    {:ok, sf_b} = StatefulStore.get(store, "sf-b")
    assert sf_b.state == :evicted
    assert sf_b.terminal_reason == "pair_broken"

    # wl-a's valid banked bundle is untouched.
    {:ok, sf_a} = StatefulStore.get(store, "sf-a")
    assert sf_a.state == :banked

    # The eviction is DURABLE (a stateful_evicted op landed), so a rebuild agrees.
    {:ok, rows} = SQLite.load_stateful_instances(op_log)
    row_b = Enum.find(rows, &(&1.instance_id == "sf-b"))
    assert row_b.state == "evicted"
  end

  # -- counts across live + banked --------------------------------------------

  test "counts reflects live and banked across workloads", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, _} = start_instance(store, workload: "wl-a", instance_id: "sf-a1", generation: 0)
    {:ok, _} = StatefulStore.publish(store, "sf-a1", "10.0.0.1", 8080, :started)

    {:ok, _} = start_instance(store, workload: "wl-b", instance_id: "sf-b1", generation: 0)
    _ = bank(store, "sf-b1", "wl-b", 1)

    assert StatefulStore.counts(store) == %{
             "wl-a" => %{live: 1, banked: 0},
             "wl-b" => %{live: 0, banked: 1}
           }
  end

  # -- boot rebuild equivalence ----------------------------------------------

  test "a fresh store rebuilt from the projection matches the pre-restart facts", %{path: path} do
    {op_log, store} = start_pair(path)

    # wl-a: a live serving instance.
    {:ok, _} = start_instance(store, instance_id: "sf-a", workload: "wl-a", generation: 0)
    {:ok, _} = StatefulStore.publish(store, "sf-a", "10.0.0.1", 8080, :started)

    # wl-b: a banked instance (transient banking never persisted).
    {:ok, _} = start_instance(store, instance_id: "sf-b", workload: "wl-b", generation: 0)
    _ = bank(store, "sf-b", "wl-b", 2)

    # Restart: a NEW store over the SAME op-log rebuilds from the durable projection.
    {:ok, store2} = StatefulStore.start_link(op_log: op_log, name: nil, clock: sequential_clock())

    # The serving wl-a endpoint is byte-identical after rebuild (healthy assumed for
    # a rebuilt serving row, so it re-enters the fan-out with the same fact).
    assert StatefulStore.published_endpoint(store2, "wl-a") == %{ip: "10.0.0.1", port: 8080}

    # The banked wl-b instance rebuilt as banked with its stamped bundle generation.
    assert StatefulStore.counts(store2) == %{
             "wl-a" => %{live: 1, banked: 0},
             "wl-b" => %{live: 0, banked: 1}
           }

    {:ok, sf_b} = StatefulStore.get(store2, "sf-b")
    assert sf_b.state == :banked
    assert sf_b.snapshot_generation == 2
  end
end
