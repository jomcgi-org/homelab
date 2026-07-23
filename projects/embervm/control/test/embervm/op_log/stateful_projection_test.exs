defmodule Embervm.OpLog.StatefulProjectionTest do
  @moduledoc """
  Exercises the R4 stateful-instance and volume records, the generation-pairing
  audit, and retention discipline on the SQLite op-log backend directly,
  mirroring serving_projection_test.exs (each test opens its own GenServer over a
  fresh temp file, so it can stop/restart to simulate crash recovery). Covers:

    * projection rebuild from a scripted stateful op sequence reproduces exact
      stateful_instances states (start -> publish -> unpublish -> bank -> relight,
      warm; and a full pair-mismatch cold-boot cycle);
    * the volume row is created, its generation tracks every attach's bump, and
      it OUTLIVES the instance (retention never prunes it, and it survives a
      terminal instance);
    * stateful_stats charges usage.request_count by cx_delta, principal from the
      op, vcpu/gb/task_count untouched;
    * retention never prunes a non-terminal stateful instance, prunes a terminal
      one past the 7-day window, and never touches the volumes table;
    * the ops-journal prefix marker never advances past a LIVE stateful
      instance's ops;
    * a kill/restart rebuilds stateful instance AND volume state exactly.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Op
  alias Embervm.OpLog.SQLite

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_oplog_stateful_test_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp start_server(path, extra_opts \\ []) do
    opts = Keyword.merge([path: path, name: nil], extra_opts)
    {:ok, pid} = SQLite.start_link(opts)
    pid
  end

  defp volume_created_op(ts, generation, extra \\ %{}) do
    %Op{
      kind: :volume_created,
      tenant: "t1",
      principal: "p1",
      workload: "scratch-postgres",
      ts: ts,
      payload:
        Map.merge(
          %{node_id: "node-4", generation: generation, size_bytes: 10_737_418_240, allocated_bytes: 0},
          extra
        )
    }
  end

  defp started_op(instance_id, ts, generation, extra \\ %{}) do
    %Op{
      kind: :stateful_started,
      tenant: "t1",
      principal: "p1",
      workload: "scratch-postgres",
      stateful_instance_id: instance_id,
      ts: ts,
      payload:
        Map.merge(
          %{node_id: "node-4", vm_id: "vm-#{instance_id}", generation: generation},
          extra
        )
    }
  end

  defp instance_by_id(server) do
    {:ok, instances} = SQLite.load_stateful_instances(server)
    Map.new(instances, &{&1.instance_id, &1})
  end

  defp volume_by_workload(server) do
    {:ok, volumes} = SQLite.load_volumes(server)
    Map.new(volumes, &{&1.workload, &1})
  end

  test "a scripted start -> publish -> unpublish -> bank -> relight -> publish sequence projects exact state, warm",
       %{path: path} do
    server = start_server(path)

    # Volume created first (generation 1), then the FRESH boot at generation 1.
    {:ok, _} = SQLite.append(server, volume_created_op(90, 1))
    {:ok, _} = SQLite.append(server, started_op("st-1", 100, 1))

    i0 = instance_by_id(server)["st-1"]
    assert i0.state == "starting"
    assert i0.generation == 1
    assert i0.snapshot_ref == nil
    assert volume_by_workload(server)["scratch-postgres"].generation == 1

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_published,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-1",
        ts: 150,
        payload: %{ip: "10.99.0.3", port: 5432}
      })

    i1 = instance_by_id(server)["st-1"]
    assert i1.state == "serving"
    assert i1.ip == "10.99.0.3"
    assert i1.port == 5432

    # Unpublish is audit-only: the VM still exists, so state stays "serving".
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_unpublished,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-1",
        ts: 200,
        payload: %{reason: "idle_bank"}
      })

    assert instance_by_id(server)["st-1"].state == "serving"

    # Bank stamps generation 1 into the bundle and clears the endpoint.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_banked,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-1",
        ts: 250,
        payload: %{snapshot_ref: "stateful/scratch-postgres", size_bytes: 16384, generation: 1}
      })

    banked = instance_by_id(server)["st-1"]
    assert banked.state == "banked"
    assert banked.snapshot_ref == "stateful/scratch-postgres"
    assert banked.snapshot_generation == 1
    assert banked.snapshot_size_bytes == 16384
    assert banked.ip == nil
    # Pair is valid: bundle's stamped generation == volume's current generation.
    assert banked.snapshot_generation == volume_by_workload(server)["scratch-postgres"].generation

    # A warm relight bumps the generation to 2, spends the bundle (clears the
    # snapshot fields), and returns to "starting".
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_relit,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-1",
        ts: 300,
        payload: %{node_id: "node-4", vm_id: "vm-st1-relit", generation: 2}
      })

    relit = instance_by_id(server)["st-1"]
    assert relit.state == "starting"
    assert relit.vm_id == "vm-st1-relit"
    assert relit.generation == 2
    assert relit.snapshot_ref == nil
    assert relit.snapshot_generation == nil
    # The attach bumped the volume generation too.
    assert volume_by_workload(server)["scratch-postgres"].generation == 2

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_published,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-1",
        ts: 350,
        payload: %{ip: "10.99.0.7", port: 5432}
      })

    republished = instance_by_id(server)["st-1"]
    assert republished.state == "serving"
    assert republished.ip == "10.99.0.7"
    assert republished.created_at == 100

    :ok = GenServer.stop(server)
  end

  test "a pair-mismatch cold-boot cycle: bundle evicted, new cold-booted instance, data intact",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, volume_created_op(90, 1))
    {:ok, _} = SQLite.append(server, started_op("st-1", 100, 1))

    # Bank stamps generation 1.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_banked,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-1",
        ts: 200,
        payload: %{snapshot_ref: "stateful/scratch-postgres", size_bytes: 16384, generation: 1}
      })

    # An out-of-band COLD boot bumps the volume to generation 2 WITHOUT the
    # banked bundle witnessing it: the pair is now broken. Modelled as a new
    # instance's stateful_started at generation 2 (an explicit COLD).
    {:ok, _} = SQLite.append(server, started_op("st-2-cold", 250, 2))

    # The banked instance's bundle is now stale; the eager sweep evicts it
    # (terminal for that instance) ...
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_evicted,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-1",
        ts: 260,
        payload: %{reason: "pair_broken"}
      })

    # ... and a later wake that tried to relight falls back to a cold boot at
    # generation 3, recording the reason for full log reconstructability.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_cold_booted,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-3-wake",
        ts: 300,
        payload: %{
          node_id: "node-4",
          vm_id: "vm-st3",
          generation: 3,
          reason: "generation_mismatch"
        }
      })

    by_id = instance_by_id(server)
    assert by_id["st-1"].state == "evicted"
    assert by_id["st-1"].terminal_reason == "pair_broken"
    assert by_id["st-3-wake"].state == "starting"
    assert by_id["st-3-wake"].generation == 3

    # The volume outlived every instance and tracks the latest attach generation.
    vol = volume_by_workload(server)["scratch-postgres"]
    assert vol.generation == 3

    # The cold-boot reason is reconstructable from the op-log alone. A payload
    # read back through read_from is decoded with STRING keys (the durable
    # payload_json round-trip), unlike the atom-keyed map the projection saw at
    # append time, so assert on the string key.
    {:ok, ops} = SQLite.read_from(server, 0)
    cold = Enum.find(ops, &(&1.kind == :stateful_cold_booted))
    assert cold.payload["reason"] == "generation_mismatch"
    assert cold.stateful_instance_id == "st-3-wake"

    :ok = GenServer.stop(server)
  end

  test "each terminal kind projects its terminal state and reason", %{path: path} do
    server = start_server(path)

    for {iid, kind, expected_state} <- [
          {"st-evi", :stateful_evicted, "evicted"},
          {"st-des", :stateful_destroyed, "destroyed"},
          {"st-fai", :stateful_failed, "failed"}
        ] do
      {:ok, _} = SQLite.append(server, started_op(iid, 100, 1))

      {:ok, _} =
        SQLite.append(server, %Op{
          kind: kind,
          tenant: "t1",
          principal: "p1",
          workload: "scratch-postgres",
          stateful_instance_id: iid,
          ts: 500,
          payload: %{reason: expected_state}
        })

      i = instance_by_id(server)[iid]
      assert i.state == expected_state
      assert i.terminal_reason == expected_state
    end

    :ok = GenServer.stop(server)
  end

  test "stateful_stats charges usage.request_count by cx_delta, vcpu/gb/task untouched", %{path: path} do
    server = start_server(path)

    day5 = 5 * 86_400_000

    for delta <- [4, 6] do
      {:ok, _} =
        SQLite.append(server, %Op{
          kind: :stateful_stats,
          tenant: "t1",
          principal: "p1",
          workload: "scratch-postgres",
          stateful_instance_id: nil,
          ts: day5 + delta,
          payload: %{workload: "scratch-postgres", cx_delta: delta, window_ms: 5_000}
        })
    end

    {:ok, page} = SQLite.list_usage(server, since_day: 0)
    row = Enum.find(page.items, &(&1.principal == "p1"))

    assert row.request_count == 10
    assert row.vcpu_seconds == 0.0
    assert row.gb_seconds == 0.0
    assert row.task_count == 0

    :ok = GenServer.stop(server)
  end

  test "retention prunes a terminal instance but never a live one, and never the volume", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, volume_created_op(90, 1))
    {:ok, _} = SQLite.append(server, started_op("st-live", 100, 1))
    {:ok, _} = SQLite.append(server, started_op("st-term", 100, 1))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-term",
        ts: 200,
        payload: %{reason: "forced_roll"}
      })

    eight_days = 8 * 24 * 60 * 60 * 1000
    {:ok, res} = SQLite.compact(server, 100 + eight_days)
    assert res.stateful_instances_compacted == 1

    ids = instance_by_id(server) |> Map.keys() |> Enum.sort()
    assert ids == ["st-live"]

    # The volume is NEVER swept by retention: data outlives every instance.
    assert Map.has_key?(volume_by_workload(server), "scratch-postgres")

    :ok = GenServer.stop(server)
  end

  test "the ops-journal marker never advances past a live stateful instance's ops", %{path: path} do
    server = start_server(path, journal_horizon_ms: 0)

    {:ok, live_seq} = SQLite.append(server, started_op("st-live", 100, 1))
    {:ok, _} = SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 101, payload: %{}})

    {:ok, res} = SQLite.compact(server, 10_000)
    assert res.compacted_through == live_seq - 1

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-live",
        ts: 102,
        payload: %{reason: "destroyed"}
      })

    {:ok, res2} = SQLite.compact(server, 10_000)
    {:ok, max_seq} = SQLite.compacted_through(server)
    assert res2.compacted_through == max_seq
    assert max_seq >= live_seq

    :ok = GenServer.stop(server)
  end

  test "kill/restart rebuilds stateful instance and volume state from the projection", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, volume_created_op(90, 1))
    {:ok, _} = SQLite.append(server, started_op("st-a", 100, 1))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_banked,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: "st-a",
        ts: 200,
        payload: %{snapshot_ref: "stateful/scratch-postgres", size_bytes: 16384, generation: 1}
      })

    :ok = GenServer.stop(server)

    server2 = start_server(path)
    by_id = instance_by_id(server2)

    assert by_id["st-a"].state == "banked"
    assert by_id["st-a"].snapshot_generation == 1
    assert by_id["st-a"].snapshot_ref == "stateful/scratch-postgres"
    assert by_id["st-a"].ip == nil

    vol = volume_by_workload(server2)["scratch-postgres"]
    assert vol.generation == 1
    assert vol.size_bytes == 10_737_418_240

    :ok = GenServer.stop(server2)
  end

  test "a fresh DB reports stateful_instance_id on stateful ops through read_from, NULL on stats and volume ops",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, volume_created_op(90, 1))
    {:ok, _} = SQLite.append(server, started_op("st-r", 100, 1))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :stateful_stats,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        stateful_instance_id: nil,
        ts: 101,
        payload: %{workload: "scratch-postgres", cx_delta: 1, window_ms: 5_000}
      })

    {:ok, ops} = SQLite.read_from(server, 0)

    started = Enum.find(ops, &(&1.kind == :stateful_started))
    assert started.stateful_instance_id == "st-r"
    assert started.task_id == nil
    assert started.session_id == nil
    assert started.serving_instance_id == nil

    stats = Enum.find(ops, &(&1.kind == :stateful_stats))
    assert stats.stateful_instance_id == nil
    assert stats.principal == "p1"

    vol_op = Enum.find(ops, &(&1.kind == :volume_created))
    assert vol_op.stateful_instance_id == nil
    assert vol_op.workload == "scratch-postgres"

    :ok = GenServer.stop(server)
  end

  # -- generation blessing (R7, ADR embervm/011) ------------------------------
  #
  # generation_blessed projects into the volume_blessing table, a SEPARATE
  # table from volumes (see Embervm.OpLog.SQLite's comment on volume_blessing
  # for why): a workload can be blessed BEFORE its FRESH boot's volume_created
  # ever lands, and folding blessing into `volumes` would fabricate a phantom,
  # node_id-less row that breaks the control plane's nil-means-no-volume-yet
  # placement contract.

  defp blessed_op(ts, generation) do
    %Op{
      kind: :generation_blessed,
      tenant: "t1",
      principal: "p1",
      workload: "scratch-postgres",
      ts: ts,
      payload: %{generation: generation}
    }
  end

  defp blessing_by_workload(server) do
    {:ok, rows} = SQLite.load_volume_blessing(server)
    Map.new(rows, &{&1.workload, &1})
  end

  test "generation_blessed upserts volume_blessing, workload-scoped (stateful_instance_id nil), never touching volumes",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, blessed_op(90, 1))
    blessing = blessing_by_workload(server)
    assert blessing["scratch-postgres"].blessed_generation == 1

    # A blessing landing before any volume_created never creates a volumes row
    # (a workload's FIRST wake blesses before the daemon's FRESH boot has
    # created the volume; volumes stays empty until volume_created lands).
    volumes = volume_by_workload(server)
    assert volumes == %{}

    {:ok, ops} = SQLite.read_from(server, 0)
    blessed = Enum.find(ops, &(&1.kind == :generation_blessed))
    assert blessed.stateful_instance_id == nil
    assert blessed.workload == "scratch-postgres"
    assert blessed.payload["generation"] == 1

    :ok = GenServer.stop(server)
  end

  test "generation_blessed after volume_created updates the blessing ledger without disturbing the live generation",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, volume_created_op(90, 3))
    {:ok, _} = SQLite.append(server, blessed_op(91, 4))

    volumes = volume_by_workload(server)
    assert volumes["scratch-postgres"].generation == 3
    assert blessing_by_workload(server)["scratch-postgres"].blessed_generation == 4

    :ok = GenServer.stop(server)
  end

  test "a volume_created upsert after a blessing never disturbs the separate blessing ledger", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, blessed_op(90, 1))
    {:ok, _} = SQLite.append(server, volume_created_op(91, 1))

    assert blessing_by_workload(server)["scratch-postgres"].blessed_generation == 1
    assert volume_by_workload(server)["scratch-postgres"].generation == 1

    :ok = GenServer.stop(server)
  end

  test "kill/restart rebuilds the blessing ledger from the durable projection", %{path: path} do
    server = start_server(path)
    {:ok, _} = SQLite.append(server, volume_created_op(90, 1))
    {:ok, _} = SQLite.append(server, blessed_op(91, 2))
    :ok = GenServer.stop(server)

    server2 = start_server(path)
    assert blessing_by_workload(server2)["scratch-postgres"].blessed_generation == 2
    :ok = GenServer.stop(server2)
  end

  test "a reopened op-log's read_from still round-trips the generation_blessed payload (string keys, value intact)",
       %{path: path} do
    server = start_server(path)
    {:ok, _} = SQLite.append(server, blessed_op(90, 3))
    :ok = GenServer.stop(server)

    # Reopening simulates a control-plane restart reading the ops table fresh
    # (the future replica-catch-up path, per Embervm.OpLog's moduledoc). The
    # payload comes back through decode_payload/1, which ALWAYS yields
    # string-keyed maps (even for an ETF-encoded payload written with atom
    # keys), so a reader must use string keys here, matching every other op
    # kind's read_from assertions in this suite (never `payload.generation`).
    server2 = start_server(path)
    {:ok, ops} = SQLite.read_from(server2, 0)
    blessed = Enum.find(ops, &(&1.kind == :generation_blessed))
    assert blessed.payload["generation"] == 3
    :ok = GenServer.stop(server2)
  end

  # -- checkpoint-dispatch record (R7, ADR embervm/017) -----------------------

  defp dispatched_op(ts, vm_id, generation) do
    %Op{
      kind: :checkpoint_dispatched,
      tenant: "t1",
      principal: "p1",
      workload: "scratch-postgres",
      ts: ts,
      payload: %{vm_id: vm_id, generation: generation}
    }
  end

  defp dispatches_by_workload(server) do
    {:ok, rows} = SQLite.load_checkpoint_dispatches(server)
    Map.new(rows, &{&1.workload, &1})
  end

  test "checkpoint_dispatched projects into checkpoint_dispatch and load returns {vm_id, generation}", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, dispatched_op(90, "vm-1", 5))

    rec = dispatches_by_workload(server)["scratch-postgres"]
    assert rec.vm_id == "vm-1"
    assert rec.generation == 5

    :ok = GenServer.stop(server)
  end

  test "a second checkpoint_dispatched upserts (one row per workload)", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, dispatched_op(90, "vm-1", 5))
    {:ok, _} = SQLite.append(server, dispatched_op(91, "vm-2", 8))

    dispatches = dispatches_by_workload(server)
    assert map_size(dispatches) == 1
    assert dispatches["scratch-postgres"].vm_id == "vm-2"
    assert dispatches["scratch-postgres"].generation == 8

    :ok = GenServer.stop(server)
  end

  test "checkpoint_resolved deletes the dispatch row, and it stays gone across a reopen", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, dispatched_op(90, "vm-1", 5))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :checkpoint_resolved,
        tenant: "t1",
        principal: "p1",
        workload: "scratch-postgres",
        ts: 91,
        payload: %{}
      })

    assert dispatches_by_workload(server) == %{}
    :ok = GenServer.stop(server)

    # An unresolved row must survive a reopen (the durability the auto-heal relies
    # on); a resolved one must stay deleted.
    server2 = start_server(path)
    assert dispatches_by_workload(server2) == %{}
    :ok = GenServer.stop(server2)
  end

  test "an UNRESOLVED checkpoint_dispatched survives a reopen (durable across a CP restart)", %{path: path} do
    server = start_server(path)
    {:ok, _} = SQLite.append(server, dispatched_op(90, "vm-1", 5))
    :ok = GenServer.stop(server)

    server2 = start_server(path)
    rec = dispatches_by_workload(server2)["scratch-postgres"]
    assert rec.vm_id == "vm-1"
    assert rec.generation == 5
    :ok = GenServer.stop(server2)
  end
end
