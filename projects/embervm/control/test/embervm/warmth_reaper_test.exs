defmodule Embervm.WarmthReaperTest do
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, WarmthReaper}

  # A minimal store stub: any GenServer answering :all with a fixed instance list is
  # a drop-in for StatefulStore/GroupStore here, because WarmthReaper only ever calls
  # `Store.all(store)` which is `GenServer.call(store, :all)`. This keeps the test
  # hermetic (no op-log, no ETS store internals) exactly as the base retention tests
  # inject a fake evict_fun/build_fun rather than standing up real daemons.
  defmodule StoreStub do
    use GenServer
    def start_link(instances), do: GenServer.start_link(__MODULE__, instances)
    @impl true
    def init(instances), do: {:ok, instances}
    @impl true
    def handle_call(:all, _from, instances), do: {:reply, instances, instances}
  end

  defp start_store(instances) do
    {:ok, pid} = StoreStub.start_link(instances)
    pid
  end

  defp new_cap_table do
    table = :"wr_cap_#{System.unique_integer([:positive])}"
    NodeCapacity.create(table)
    table
  end

  # Seed one node's reported warmth inventory (the NodeStatus projection
  # NodeRegistry writes into NodeCapacity). instance_id == node_id so
  # WakeInstance.dial_for_bundle/dial_for_group fail-open to the node_id dial key.
  defp put_warmth_fact(table, node_id, stateful_bundles, group_bundle_sets) do
    NodeCapacity.put(table, {node_id, "ds"}, %{
      node_id: node_id,
      instance_id: node_id,
      # A serving_subnet_cidr is unused by the reaper but present on a real fact.
      serving_subnet_cidr: "10.0.0.0/24",
      stateful_bundles: stateful_bundles,
      group_bundle_sets: group_bundle_sets,
      updated_at: 0
    })
  end

  defp stateful_bundle(ref, workload, bytes, exported \\ true) do
    %{snapshot_ref: ref, workload: workload, generation: 1, size_bytes: bytes, exported: exported}
  end

  defp group_set(set_id, group_instance_id, members) do
    %{
      set_id: set_id,
      group_instance_id: group_instance_id,
      exported: true,
      members: members
    }
  end

  defp member(name, ref, bytes), do: %{member_name: name, snapshot_ref: ref, size_bytes: bytes}

  defp stateful_instance(state, ref) do
    %{instance_id: "state-#{ref}", workload: "pg", state: state, snapshot_ref: ref, node_id: "node-4"}
  end

  defp group_instance(state, set_id) do
    %{instance_id: "grp-#{set_id}", workload: "web", state: state, set_id: set_id, node_id: "node-4"}
  end

  defp start_reaper(opts) do
    {:ok, pid} =
      WarmthReaper.start_link(
        Keyword.merge(
          [
            name: nil,
            # 0 disables the timer; tests drive sweep_now/1 synchronously.
            sweep_interval_ms: 0
          ],
          opts
        )
      )

    pid
  end

  defp recording_evict_funs(test_pid) do
    artifact_fun = fn _channel, req ->
      send(test_pid, {:evict_artifact, req.artifact.kind, req.artifact.ref, req.remote})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    snapshot_fun = fn _channel, req ->
      send(test_pid, {:evict_snapshot, req.snapshot_ref})
      {:ok, %Embervm.Node.V1.EvictSnapshotResponse{}}
    end

    {artifact_fun, snapshot_fun}
  end

  defp fake_channel_fun, do: fn _dial_key -> {:ok, :fake_channel} end

  describe "stateful warmth retention" do
    test "evicts an orphaned stateful bundle entirely (local + remote) with the gate ON" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      # The node holds two bundles: one for a live instance (desired) and one whose
      # instance is terminal (orphan). Only the orphan evicts.
      put_warmth_fact(
        table,
        "node-4",
        [
          stateful_bundle("keep-current", "pg", 512),
          stateful_bundle("orphan-dead", "pg", 4_096)
        ],
        []
      )

      stateful = start_store([stateful_instance(:serving, "keep-current"), stateful_instance(:destroyed, "orphan-dead")])
      group = start_store([])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          sweep_enabled: true
        )

      plan = WarmthReaper.sweep_now(reaper)

      entry = Enum.find(plan, &(&1.id == "orphan-dead"))
      assert entry.kind == :stateful
      assert entry.evict_bytes == 4_096
      refute Enum.any?(plan, &(&1.id == "keep-current"))

      # Orphan evicted entirely: local (remote: false) AND remote (remote: true).
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-dead", false}, 1_000
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-dead", true}, 1_000
      # The live instance's current bundle is never touched.
      refute_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "keep-current", _}, 100
    end

    test "a BANKED instance's bundle is protected (banked is non-terminal warmth)" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      put_warmth_fact(table, "node-4", [stateful_bundle("banked-warm", "pg", 2_048)], [])

      stateful = start_store([stateful_instance(:banked, "banked-warm")])
      group = start_store([])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          sweep_enabled: true
        )

      plan = WarmthReaper.sweep_now(reaper)

      assert plan == []
      refute_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "banked-warm", _}, 200
    end

    test "a bundle whose instance the CP no longer tracks at ALL is an orphan and evicts" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      put_warmth_fact(table, "node-4", [stateful_bundle("ghost", "pg", 1_024)], [])

      # The store returns NO instance for this bundle (its row was compacted away):
      # the reaper must still reclaim it (the whole point of the reconciled backstop).
      stateful = start_store([])
      group = start_store([])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          sweep_enabled: true
        )

      plan = WarmthReaper.sweep_now(reaper)

      assert [%{kind: :stateful, id: "ghost", evict_bytes: 1_024}] = plan
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "ghost", false}, 1_000
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "ghost", true}, 1_000
    end
  end

  describe "group warmth retention" do
    test "evicts an orphaned group set: per-member local + one remote set evict (gate ON)" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      put_warmth_fact(table, "node-4", [], [
        group_set("keep-set", "grp-keep-set", [member("a", "m-keep-a", 100), member("b", "m-keep-b", 200)]),
        group_set("orphan-set", "grp-orphan-set", [member("a", "m-orph-a", 1_000), member("b", "m-orph-b", 2_000)])
      ])

      stateful = start_store([])
      group = start_store([group_instance(:running, "keep-set"), group_instance(:destroyed, "orphan-set")])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          sweep_enabled: true
        )

      plan = WarmthReaper.sweep_now(reaper)

      entry = Enum.find(plan, &(&1.id == "orphan-set"))
      assert entry.kind == :group
      assert entry.evict_bytes == 3_000
      refute Enum.any?(plan, &(&1.id == "keep-set"))

      # Local eviction is PER-MEMBER (EvictSnapshot on each member ref); the whole
      # set's remote copy goes in ONE EvictArtifact{remote: true, GROUP_SET, set_id}.
      assert_receive {:evict_snapshot, "m-orph-a"}, 1_000
      assert_receive {:evict_snapshot, "m-orph-b"}, 1_000
      assert_receive {:evict_artifact, :ARTIFACT_KIND_GROUP_SET, "orphan-set", true}, 1_000
      # The live group's set and its members are never touched.
      refute_receive {:evict_snapshot, "m-keep-a"}, 100
      refute_receive {:evict_artifact, :ARTIFACT_KIND_GROUP_SET, "keep-set", _}, 100
    end
  end

  describe "the gate (off by default)" do
    test "with the gate OFF (default) the sweep plans the orphans but evicts NOTHING" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      put_warmth_fact(
        table,
        "node-4",
        [stateful_bundle("orphan-dead", "pg", 4_096)],
        [group_set("orphan-set", "grp-orphan-set", [member("a", "m-orph-a", 1_000)])]
      )

      stateful = start_store([stateful_instance(:destroyed, "orphan-dead")])
      group = start_store([group_instance(:destroyed, "orphan-set")])

      # No sweep_enabled opt => default false (what this PR ships).
      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun
        )

      plan = WarmthReaper.sweep_now(reaper)

      # The plan still names the candidates (dry-run observability)...
      assert Enum.any?(plan, &(&1.id == "orphan-dead"))
      assert Enum.any?(plan, &(&1.id == "orphan-set"))
      # ...but NOTHING is evicted.
      refute_receive {:evict_artifact, _, _, _}, 200
      refute_receive {:evict_snapshot, _}, 200
    end

    test "an empty inventory is a clean no-op (no plan, no evictions)" do
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(self())
      stateful = start_store([])
      group = start_store([])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          sweep_enabled: true
        )

      assert WarmthReaper.sweep_now(reaper) == []
    end
  end
end
