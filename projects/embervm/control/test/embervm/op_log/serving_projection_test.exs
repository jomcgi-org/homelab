defmodule Embervm.OpLog.ServingProjectionTest do
  @moduledoc """
  Exercises the R3 serving-instance records, endpoint audit ops, and retention
  discipline on the SQLite op-log backend directly, mirroring
  session_projection_test.exs exactly (each test opens its own GenServer over a
  fresh temp file, so it can stop/restart the process to simulate crash
  recovery). Covers:

    * projection rebuild from a scripted serving op sequence reproduces exact
      serving_instances states (start -> publish -> unpublish -> bank -> relit,
      and each terminal kind);
    * serving_stats charges the (principal, day) usage.request_count projection
      by rq_delta, with principal read from the op (never a join), and
      vcpu_seconds/gb_seconds untouched (D-R3.2.1);
    * retention never prunes a non-terminal serving instance, prunes a
      terminal one past the 7-day window;
    * the ops-journal prefix marker never advances past a LIVE serving
      instance's ops;
    * a kill/restart rebuilds serving instance state exactly from the durable
      projection.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Op
  alias Embervm.OpLog.SQLite

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_oplog_serving_test_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp start_server(path, extra_opts \\ []) do
    opts = Keyword.merge([path: path, name: nil], extra_opts)
    {:ok, pid} = SQLite.start_link(opts)
    pid
  end

  defp started_op(instance_id, principal, ts, extra \\ %{}) do
    %Op{
      kind: :serving_started,
      tenant: "t1",
      principal: principal,
      workload: "sandbox-serving",
      serving_instance_id: instance_id,
      ts: ts,
      payload:
        Map.merge(
          %{
            node_id: "node-4",
            vm_id: "vm-#{instance_id}",
            ip: "10.99.0.2",
            port: 8080,
            base_snapshot_ref: "base:sha256:abc",
            base_digest: "sha256:abc"
          },
          extra
        )
    }
  end

  defp instance_by_id(server) do
    {:ok, instances} = SQLite.load_serving_instances(server)
    Map.new(instances, &{&1.instance_id, &1})
  end

  test "a scripted start -> publish -> unpublish -> bank -> relight -> publish sequence projects exact state",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, started_op("srv-1", "p1", 100))

    i0 = instance_by_id(server)["srv-1"]
    assert i0.state == "starting"
    assert i0.ip == "10.99.0.2"
    assert i0.port == 8080

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_published,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-1",
        ts: 150,
        payload: %{ip: "10.99.0.2", port: 8080, reason: "started"}
      })

    i1 = instance_by_id(server)["srv-1"]
    assert i1.state == "published"

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_unpublished,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-1",
        ts: 200,
        payload: %{ip: "10.99.0.2", port: 8080, reason: "drain"}
      })

    assert instance_by_id(server)["srv-1"].state == "draining"

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_banked,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-1",
        ts: 250,
        payload: %{snapshot_ref: "serving/srv-1", size_bytes: 3072, generation: 1}
      })

    banked = instance_by_id(server)["srv-1"]
    assert banked.state == "banked"
    assert banked.snapshot_ref == "serving/srv-1"
    assert banked.snapshot_size_bytes == 3072
    assert banked.generation == 1
    # A bank clears the endpoint fact: a relight gets a fresh allocation.
    assert banked.ip == nil
    assert banked.port == nil

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_relit,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-1",
        ts: 300,
        payload: %{node_id: "node-4", vm_id: "vm-srv1-relit"}
      })

    relit = instance_by_id(server)["srv-1"]
    assert relit.state == "starting"
    assert relit.vm_id == "vm-srv1-relit"

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_published,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-1",
        ts: 350,
        payload: %{ip: "10.99.0.5", port: 8080, reason: "relit"}
      })

    republished = instance_by_id(server)["srv-1"]
    assert republished.state == "published"
    # The relight's fresh IP, distinct from the birth allocation.
    assert republished.ip == "10.99.0.5"

    # Lineage recorded at start survives the whole sequence.
    assert republished.base_snapshot_ref == "base:sha256:abc"
    assert republished.base_digest == "sha256:abc"
    assert republished.created_at == 100

    :ok = GenServer.stop(server)
  end

  test "each terminal kind projects its terminal state and reason", %{path: path} do
    server = start_server(path)

    for {iid, kind, expected_state} <- [
          {"srv-evi", :serving_evicted, "evicted"},
          {"srv-des", :serving_destroyed, "destroyed"},
          {"srv-fai", :serving_failed, "failed"}
        ] do
      {:ok, _} = SQLite.append(server, started_op(iid, "p1", 100))

      {:ok, _} =
        SQLite.append(server, %Op{
          kind: kind,
          tenant: "t1",
          principal: "p1",
          workload: "sandbox-serving",
          serving_instance_id: iid,
          ts: 500,
          payload: %{reason: expected_state}
        })

      i = instance_by_id(server)[iid]
      assert i.state == expected_state
      assert i.terminal_reason == expected_state
    end

    :ok = GenServer.stop(server)
  end

  test "serving_stats charges usage.request_count by rq_delta, principal from the op, vcpu/gb untouched",
       %{path: path} do
    server = start_server(path)

    day5 = 5 * 86_400_000

    # Workload-scoped: no serving_instance_id, principal/tenant carried on the
    # op's own top-level fields (D-R3.2.1), exactly like every other op kind.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_stats,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: nil,
        ts: day5 + 10,
        payload: %{workload: "sandbox-serving", rq_delta: 7, window_ms: 5_000}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_stats,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: nil,
        ts: day5 + 20,
        payload: %{workload: "sandbox-serving", rq_delta: 3, window_ms: 5_000}
      })

    {:ok, page} = SQLite.list_usage(server, since_day: 0)
    row = Enum.find(page.items, &(&1.principal == "p1"))

    # Two scrapes accumulate: 7 + 3 = 10.
    assert row.request_count == 10
    # Serving live-seconds are deferred (D-R3.2.1): never charged here.
    assert row.vcpu_seconds == 0.0
    assert row.gb_seconds == 0.0
    # task_count is a DISTINCT counter from request_count; serving_stats must
    # never touch it.
    assert row.task_count == 0

    :ok = GenServer.stop(server)
  end

  test "retention never prunes a non-terminal serving instance and prunes a terminal one past the window",
       %{path: path} do
    server = start_server(path)

    # Live instance, old.
    {:ok, _} = SQLite.append(server, started_op("srv-live", "p1", 100))

    # Terminal instance, old.
    {:ok, _} = SQLite.append(server, started_op("srv-term", "p1", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-term",
        ts: 200,
        payload: %{reason: "destroyed"}
      })

    # now = 100 + 8 days: both rows are older than the 7-day retention, but
    # only the terminal one is eligible.
    eight_days = 8 * 24 * 60 * 60 * 1000
    {:ok, res} = SQLite.compact(server, 100 + eight_days)
    assert res.serving_instances_compacted == 1

    ids = instance_by_id(server) |> Map.keys() |> Enum.sort()
    assert ids == ["srv-live"]

    :ok = GenServer.stop(server)
  end

  test "the ops-journal marker never advances past a live serving instance's ops", %{path: path} do
    server = start_server(path, journal_horizon_ms: 0)

    # A LIVE (starting) serving instance: its start op must pin the prefix.
    {:ok, live_seq} = SQLite.append(server, started_op("srv-live", "p1", 100))

    # A standalone old audit op AFTER it (so the whole log would otherwise be
    # compactable at horizon 0).
    {:ok, _} = SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 101, payload: %{}})

    {:ok, res} = SQLite.compact(server, 10_000)

    # The marker can only reach live_seq - 1 (the live instance's start op is
    # the smallest blocked seq).
    assert res.compacted_through == live_seq - 1

    # After the instance terminates, the whole prefix becomes eligible.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-live",
        ts: 102,
        payload: %{reason: "destroyed"}
      })

    {:ok, res2} = SQLite.compact(server, 10_000)
    {:ok, max_seq} = SQLite.compacted_through(server)
    assert res2.compacted_through == max_seq
    assert max_seq >= live_seq

    :ok = GenServer.stop(server)
  end

  test "kill/restart rebuilds live and terminal serving instance state from the projection", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, started_op("srv-a", "p1", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_banked,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-a",
        ts: 200,
        payload: %{snapshot_ref: "serving/srv-a", size_bytes: 4096, generation: 1}
      })

    {:ok, _} = SQLite.append(server, started_op("srv-b", "p1", 300))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_failed,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: "srv-b",
        ts: 400,
        payload: %{reason: "readiness_timeout"}
      })

    :ok = GenServer.stop(server)

    server2 = start_server(path)
    by_id = instance_by_id(server2)

    assert by_id["srv-a"].state == "banked"
    assert by_id["srv-a"].generation == 1
    assert by_id["srv-a"].snapshot_ref == "serving/srv-a"
    assert by_id["srv-a"].ip == nil

    assert by_id["srv-b"].state == "failed"
    assert by_id["srv-b"].terminal_reason == "readiness_timeout"

    :ok = GenServer.stop(server2)
  end

  test "a fresh DB reports serving_instance_id on serving ops through read_from, NULL on serving_stats",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, started_op("srv-r", "p1", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :serving_stats,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-serving",
        serving_instance_id: nil,
        ts: 101,
        payload: %{workload: "sandbox-serving", rq_delta: 1, window_ms: 5_000}
      })

    {:ok, ops} = SQLite.read_from(server, 0)

    started = Enum.find(ops, &(&1.kind == :serving_started))
    assert started.serving_instance_id == "srv-r"
    assert started.task_id == nil
    assert started.session_id == nil

    stats = Enum.find(ops, &(&1.kind == :serving_stats))
    assert stats.serving_instance_id == nil
    assert stats.principal == "p1"

    :ok = GenServer.stop(server)
  end
end
