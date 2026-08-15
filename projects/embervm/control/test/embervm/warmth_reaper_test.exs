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
      send(test_pid, {:evict_artifact_req, req})
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
          channel_fun: fake_channel_fun(),
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
          channel_fun: fake_channel_fun(),
          sweep_enabled: true
        )

      plan = WarmthReaper.sweep_now(reaper)

      assert [%{kind: :stateful, id: "ghost", evict_bytes: 1_024}] = plan
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "ghost", false}, 1_000
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "ghost", true}, 1_000
    end
  end

  describe "empty-binding stateful orphan lookup" do
    defp empty_binding_reaper(table, artifact_fun, snapshot_fun, index_fun, enabled \\ true) do
      stateful = start_store([stateful_instance(:destroyed, "orphan-nobind")])
      group = start_store([])

      start_reaper(
        capacity_table: table,
        stateful_store: stateful,
        group_store: group,
        evict_artifact_fun: artifact_fun,
        evict_snapshot_fun: snapshot_fun,
        channel_fun: fake_channel_fun(),
        remote_stateful_index_fun: index_fun,
        sweep_enabled: enabled
      )
    end

    test "remote hit recovers vendor and workload, then evicts local before remote" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)
      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-nobind", "", 4_096)], [])

      reaper =
        empty_binding_reaper(table, artifact_fun, snapshot_fun, fn ->
          {:ok, %{"orphan-nobind" => %{vendor: "amd", workload: "demo-postgres"}}}
        end)

      WarmthReaper.sweep_now(reaper)
      assert_receive {:evict_artifact_req, %{remote: false}}, 1_000
      assert_receive {:evict_artifact_req, %{remote: true, vendor: "amd", artifact: %{workload: "demo-postgres"}}}, 1_000
    end

    test "remote miss evicts local only" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)
      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-nobind", nil, 4_096)], [])

      reaper = empty_binding_reaper(table, artifact_fun, snapshot_fun, fn -> {:ok, %{}} end)
      WarmthReaper.sweep_now(reaper)

      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-nobind", false}, 1_000
      refute_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-nobind", true}, 300
    end

    test "remote index failure preserves both copies" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)
      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-nobind", "", 4_096)], [])

      reaper = empty_binding_reaper(table, artifact_fun, snapshot_fun, fn -> {:error, :timeout} end)
      WarmthReaper.sweep_now(reaper)

      refute_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-nobind", _}, 300
    end

    test "gate off evicts nothing after a remote hit" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)
      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-nobind", "", 4_096)], [])

      reaper =
        empty_binding_reaper(table, artifact_fun, snapshot_fun, fn ->
          {:ok, %{"orphan-nobind" => %{vendor: "amd", workload: "demo-postgres"}}}
        end, false)

      WarmthReaper.sweep_now(reaper)
      refute_receive {:evict_artifact, _, _, _}, 300
      refute_receive {:evict_snapshot, _}, 100
    end

    test "fetches the remote index once for several empty-binding bundles" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)
      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-a", "", 1), stateful_bundle("orphan-b", "", 2)], [])
      stateful = start_store([stateful_instance(:destroyed, "orphan-a"), stateful_instance(:destroyed, "orphan-b")])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: start_store([]),
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          channel_fun: fake_channel_fun(),
          remote_stateful_index_fun: fn -> send(test_pid, :remote_index_lookup); {:ok, %{}} end,
          sweep_enabled: true
        )

      WarmthReaper.sweep_now(reaper)
      assert_receive :remote_index_lookup, 1_000
      refute_receive :remote_index_lookup, 300
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
          channel_fun: fake_channel_fun(),
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

  # #38: the remote (S3) evict is the recovery copy; it must fire ONLY when the
  # paired local disk evict succeeded, so the reaper never destroys the off-node
  # copy of a bundle the node itself refused/failed to remove locally.
  describe "remote evict is gated on local success (#38)" do
    test "stateful: local failure withholds the remote S3 evict" do
      test_pid = self()
      table = new_cap_table()
      # A local (remote: false) evict that FAILS; the remote would succeed if reached.
      # Records every call so the test can assert the remote never ran.
      artifact_fun = fn _channel, req ->
        send(test_pid, {:evict_artifact, req.artifact.kind, req.artifact.ref, req.remote})

        if req.remote do
          {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
        else
          {:error, :failed_precondition}
        end
      end

      {_unused, snapshot_fun} = recording_evict_funs(test_pid)

      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-dead", "pg", 4_096)], [])
      stateful = start_store([stateful_instance(:destroyed, "orphan-dead")])
      group = start_store([])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          channel_fun: fake_channel_fun(),
          sweep_enabled: true
        )

      WarmthReaper.sweep_now(reaper)

      # The local evict was attempted...
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-dead", false}, 1_000
      # ...but because it failed, the remote (recovery copy) evict NEVER fired.
      refute_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-dead", true}, 300
    end

    test "stateful: local success permits the remote S3 evict" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-dead", "pg", 4_096)], [])
      stateful = start_store([stateful_instance(:destroyed, "orphan-dead")])
      group = start_store([])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          channel_fun: fake_channel_fun(),
          sweep_enabled: true
        )

      WarmthReaper.sweep_now(reaper)

      # Local ok THEN remote: the recovery copy is dropped only after the disk copy.
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-dead", false}, 1_000
      assert_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-dead", true}, 1_000
    end

    test "group: one member's local failure withholds the whole-set remote evict" do
      test_pid = self()
      table = new_cap_table()

      # Record every artifact call so we can assert the GROUP_SET remote never ran.
      artifact_fun = fn _channel, req ->
        send(test_pid, {:evict_artifact, req.artifact.kind, req.artifact.ref, req.remote})
        {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
      end

      # Member "a" evicts fine; member "b"'s local evict fails (e.g. a live relit
      # member): FAILED_PRECONDITION from noded's in-use guard.
      snapshot_fun = fn _channel, req ->
        send(test_pid, {:evict_snapshot, req.snapshot_ref})

        if req.snapshot_ref == "m-orph-b" do
          {:error, :failed_precondition}
        else
          {:ok, %Embervm.Node.V1.EvictSnapshotResponse{}}
        end
      end

      put_warmth_fact(table, "node-4", [], [
        group_set("orphan-set", "grp-orphan-set", [member("a", "m-orph-a", 1_000), member("b", "m-orph-b", 2_000)])
      ])

      stateful = start_store([])
      group = start_store([group_instance(:destroyed, "orphan-set")])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          channel_fun: fake_channel_fun(),
          sweep_enabled: true
        )

      WarmthReaper.sweep_now(reaper)

      # BOTH members are still attempted locally (independent, run-every semantics)...
      assert_receive {:evict_snapshot, "m-orph-a"}, 1_000
      assert_receive {:evict_snapshot, "m-orph-b"}, 1_000
      # ...but because one member's local evict failed, the whole-set remote copy is kept.
      refute_receive {:evict_artifact, :ARTIFACT_KIND_GROUP_SET, "orphan-set", true}, 300
    end
  end

  # #38 fix C remains fail-safe when the remote index is unavailable. The group
  # layout is not searchable by set id in the same way, so its empty binding still
  # skips both copies.
  describe "empty-binding lookup failures and groups remain fail-safe (#38 fix C)" do
    test "a stateful orphan with no workload binding and no remote store runs no evict" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      # The disk-reported bundle has workload "" (pre-sidecar boot scan). Its
      # instance is destroyed, so it classifies as an orphan.
      put_warmth_fact(table, "node-4", [stateful_bundle("orphan-nobind", "", 4_096)], [])
      stateful = start_store([stateful_instance(:destroyed, "orphan-nobind")])
      group = start_store([])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          channel_fun: fake_channel_fun(),
          sweep_enabled: true
        )

      plan = WarmthReaper.sweep_now(reaper)
      # It is still PLANNED as an orphan (visibility) ...
      assert Enum.any?(plan, &(&1.id == "orphan-nobind"))
      # ... but NOTHING is evicted, local or remote.
      refute_receive {:evict_artifact, :ARTIFACT_KIND_STATEFUL, "orphan-nobind", _}, 300
    end

    test "a group orphan with no group_instance_id binding runs no member local and no remote evict" do
      test_pid = self()
      table = new_cap_table()
      {artifact_fun, snapshot_fun} = recording_evict_funs(test_pid)

      # set with group_instance_id "" (pre-sidecar). group_set/3 takes the gid as
      # arg 2; pass "".
      put_warmth_fact(table, "node-4", [], [
        group_set("orphan-nobind-set", "", [member("a", "m-nb-a", 1_000), member("b", "m-nb-b", 2_000)])
      ])

      stateful = start_store([])
      group = start_store([group_instance(:destroyed, "orphan-nobind-set")])

      reaper =
        start_reaper(
          capacity_table: table,
          stateful_store: stateful,
          group_store: group,
          evict_artifact_fun: artifact_fun,
          evict_snapshot_fun: snapshot_fun,
          channel_fun: fake_channel_fun(),
          sweep_enabled: true
        )

      plan = WarmthReaper.sweep_now(reaper)
      assert Enum.any?(plan, &(&1.id == "orphan-nobind-set"))
      # No per-member local evict, no whole-set remote evict.
      refute_receive {:evict_snapshot, "m-nb-a"}, 300
      refute_receive {:evict_snapshot, "m-nb-b"}, 200
      refute_receive {:evict_artifact, :ARTIFACT_KIND_GROUP_SET, "orphan-nobind-set", true}, 200
    end
  end
end
