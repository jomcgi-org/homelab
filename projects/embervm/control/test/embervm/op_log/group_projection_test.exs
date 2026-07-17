defmodule Embervm.OpLog.GroupProjectionTest do
  @moduledoc """
  Exercises the R5 composite-group instance and member records, the bundle-set
  audit, and retention discipline on the SQLite op-log backend directly,
  mirroring stateful_projection_test.exs (each test opens its own GenServer over a
  fresh temp file, so it can stop/restart to simulate crash recovery). Covers:

    * projection rebuild from a scripted group op sequence reproduces exact
      group_instances + group_members state across a full lifecycle
      (create -> net_created -> member_started -> running -> published -> banked ->
      set_evicted -> fresh_booted -> destroyed);
    * group_banked stamps the WHOLE member set atomically in one append (decision 3);
    * group_fresh_booted / group_set_evicted carry their reason for full log
      reconstructability, and clear the banked set_id;
    * group_degraded flips one member unhealthy while the group stays live;
    * group_stats bills PER MEMBER (a 3-member group bills 3 VMs' worth) into
      vcpu_seconds/gb_seconds, task/request untouched;
    * retention never prunes a non-terminal group, prunes a terminal one past the
      7-day window, and takes its member rows with it;
    * the ops-journal prefix marker never advances past a LIVE group's ops;
    * a kill/restart rebuilds group instance AND member state exactly.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Op
  alias Embervm.OpLog.SQLite

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_oplog_group_test_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp start_server(path, extra_opts \\ []) do
    opts = Keyword.merge([path: path, name: nil], extra_opts)
    {:ok, pid} = SQLite.start_link(opts)
    pid
  end

  defp created_op(instance_id, ts, extra \\ %{}) do
    %Op{
      kind: :group_created,
      tenant: "t1",
      principal: "p1",
      workload: "demo-group",
      group_instance_id: instance_id,
      ts: ts,
      payload:
        Map.merge(
          %{node_id: "node-4", entry_member: "leader", entry_port: 8080, listen_port: 5410},
          extra
        )
    }
  end

  defp member_started_op(instance_id, ts, member_name, index, extra \\ %{}) do
    %Op{
      kind: :group_member_started,
      tenant: "t1",
      principal: "p1",
      workload: "demo-group",
      group_instance_id: instance_id,
      ts: ts,
      payload:
        Map.merge(
          %{member_name: member_name, member_index: index, vm_id: "vm-#{member_name}", ip: "10.80.0.#{index + 2}"},
          extra
        )
    }
  end

  defp instance_by_id(server) do
    {:ok, instances} = SQLite.load_group_instances(server)
    Map.new(instances, &{&1.instance_id, &1})
  end

  defp members_by_name(server, instance_id) do
    {:ok, members} = SQLite.load_group_members(server)

    members
    |> Enum.filter(&(&1.instance_id == instance_id))
    |> Map.new(&{&1.member_name, &1})
  end

  test "a scripted create -> members -> running -> published -> banked -> set_evicted -> fresh_booted sequence projects exact state",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("g-1", 100))

    i0 = instance_by_id(server)["g-1"]
    assert i0.state == "starting"
    assert i0.entry_member == "leader"
    assert i0.entry_port == 8080
    assert i0.listen_port == 5410
    assert i0.set_id == nil

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_net_created,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-1",
        ts: 110,
        payload: %{subnet_cidr: "10.80.0.0/29"}
      })

    assert instance_by_id(server)["g-1"].subnet_cidr == "10.80.0.0/29"

    # Three members come up (leader, worker-0, worker-1): the first multi-row
    # projection. All start "starting" and unhealthy.
    {:ok, _} = SQLite.append(server, member_started_op("g-1", 120, "leader", 0))
    {:ok, _} = SQLite.append(server, member_started_op("g-1", 121, "worker-0", 1))
    {:ok, _} = SQLite.append(server, member_started_op("g-1", 122, "worker-1", 2))

    members = members_by_name(server, "g-1")
    assert Map.keys(members) |> Enum.sort() == ["leader", "worker-0", "worker-1"]
    assert members["leader"].state == "starting"
    assert members["leader"].healthy == false
    assert members["worker-1"].vm_id == "vm-worker-1"

    # The whole-group readiness edge: instance -> running, every member healthy.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_running,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-1",
        ts: 130,
        payload: %{}
      })

    running = instance_by_id(server)["g-1"]
    assert running.state == "running"
    assert running.last_active_at == 130
    assert Enum.all?(members_by_name(server, "g-1"), fn {_n, m} -> m.healthy end)

    # The entry endpoint is published (audit; stays running).
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_published,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-1",
        ts: 140,
        payload: %{listen_port: 5410}
      })

    assert instance_by_id(server)["g-1"].state == "running"

    # Bank stamps the WHOLE set atomically in ONE append (decision 3): set_id on the
    # instance, each member's snapshot_ref, member VMs cleared.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_banked,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-1",
        ts: 150,
        payload: %{
          set_id: "group/demo-group/set-1",
          members: [
            %{name: "leader", snapshot_ref: "group/demo-group/set-1/leader"},
            %{name: "worker-0", snapshot_ref: "group/demo-group/set-1/worker-0"},
            %{name: "worker-1", snapshot_ref: "group/demo-group/set-1/worker-1"}
          ]
        }
      })

    banked = instance_by_id(server)["g-1"]
    assert banked.state == "banked"
    assert banked.set_id == "group/demo-group/set-1"

    banked_members = members_by_name(server, "g-1")
    assert banked_members["leader"].snapshot_ref == "group/demo-group/set-1/leader"
    assert banked_members["worker-1"].snapshot_ref == "group/demo-group/set-1/worker-1"
    assert banked_members["leader"].vm_id == nil
    assert banked_members["leader"].state == "banked"

    # A set eviction discards the banked warmth (its reason rides the payload) and
    # clears set_id; the instance stays live.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_set_evicted,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-1",
        ts: 160,
        payload: %{reason: "set_unreadable", members: ["leader", "worker-0", "worker-1"]}
      })

    assert instance_by_id(server)["g-1"].set_id == nil

    # A fresh boot (discarded warmth) returns to "starting" and records its reason.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_fresh_booted,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-1",
        ts: 170,
        payload: %{reason: "set_unreadable"}
      })

    fresh = instance_by_id(server)["g-1"]
    assert fresh.state == "starting"
    assert fresh.set_id == nil

    # The fresh-boot reason is reconstructable from the op-log alone (string keys on
    # the durable payload_json round-trip).
    {:ok, ops} = SQLite.read_from(server, 0)
    fb = Enum.find(ops, &(&1.kind == :group_fresh_booted))
    assert fb.payload["reason"] == "set_unreadable"
    assert fb.group_instance_id == "g-1"

    :ok = GenServer.stop(server)
  end

  test "group_degraded flips one member unhealthy while the group stays live", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("g-d", 100))
    {:ok, _} = SQLite.append(server, member_started_op("g-d", 110, "leader", 0))
    {:ok, _} = SQLite.append(server, member_started_op("g-d", 111, "worker-0", 1))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_running,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-d",
        ts: 120,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_degraded,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-d",
        ts: 130,
        payload: %{member_name: "worker-0", reason: "health_probe_failed"}
      })

    assert instance_by_id(server)["g-d"].state == "degraded"

    members = members_by_name(server, "g-d")
    assert members["leader"].healthy == true
    assert members["worker-0"].healthy == false

    :ok = GenServer.stop(server)
  end

  test "each terminal kind projects its terminal state and reason (expired rides destroyed)", %{path: path} do
    server = start_server(path)

    for {iid, kind, reason, expected_state} <- [
          {"g-des", :group_destroyed, "destroyed", "destroyed"},
          {"g-exp", :group_destroyed, "expired", "destroyed"},
          {"g-fai", :group_failed, "failed", "failed"}
        ] do
      {:ok, _} = SQLite.append(server, created_op(iid, 100))

      {:ok, _} =
        SQLite.append(server, %Op{
          kind: kind,
          tenant: "t1",
          principal: "p1",
          workload: "demo-group",
          group_instance_id: iid,
          ts: 500,
          payload: %{reason: reason}
        })

      i = instance_by_id(server)[iid]
      assert i.state == expected_state
      assert i.terminal_reason == reason
    end

    :ok = GenServer.stop(server)
  end

  test "group_stats bills per member into vcpu/gb seconds, task/request untouched", %{path: path} do
    server = start_server(path)

    day5 = 5 * 86_400_000

    # One 3-member group billing 2.0 vcpu-seconds / 4.0 gb-seconds per member for one
    # window: the projection multiplies by member_count, so 3 members = 6.0 / 12.0.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_stats,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: nil,
        ts: day5 + 1,
        payload: %{member_count: 3, usage: %{vcpu_seconds: 2.0, gb_seconds: 4.0}, window_ms: 5_000}
      })

    {:ok, page} = SQLite.list_usage(server, since_day: 0)
    row = Enum.find(page.items, &(&1.principal == "p1"))

    assert row.vcpu_seconds == 6.0
    assert row.gb_seconds == 12.0
    assert row.task_count == 0
    assert row.request_count == 0

    :ok = GenServer.stop(server)
  end

  test "retention prunes a terminal group and its members, never a live one", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("g-live", 100))
    {:ok, _} = SQLite.append(server, member_started_op("g-live", 101, "leader", 0))
    {:ok, _} = SQLite.append(server, created_op("g-term", 100))
    {:ok, _} = SQLite.append(server, member_started_op("g-term", 101, "leader", 0))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-term",
        ts: 200,
        payload: %{reason: "forced_roll"}
      })

    eight_days = 8 * 24 * 60 * 60 * 1000
    {:ok, res} = SQLite.compact(server, 100 + eight_days)
    assert res.group_instances_compacted == 1

    ids = instance_by_id(server) |> Map.keys() |> Enum.sort()
    assert ids == ["g-live"]

    # The pruned group's members went with it; the live group's members remain.
    assert members_by_name(server, "g-term") == %{}
    assert Map.has_key?(members_by_name(server, "g-live"), "leader")

    :ok = GenServer.stop(server)
  end

  test "the ops-journal marker never advances past a live group's ops", %{path: path} do
    server = start_server(path, journal_horizon_ms: 0)

    {:ok, live_seq} = SQLite.append(server, created_op("g-live", 100))
    {:ok, _} = SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 101, payload: %{}})

    {:ok, res} = SQLite.compact(server, 10_000)
    assert res.compacted_through == live_seq - 1

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-live",
        ts: 102,
        payload: %{reason: "destroyed"}
      })

    {:ok, res2} = SQLite.compact(server, 10_000)
    {:ok, max_seq} = SQLite.compacted_through(server)
    assert res2.compacted_through == max_seq
    assert max_seq >= live_seq

    :ok = GenServer.stop(server)
  end

  test "kill/restart rebuilds group instance and member state from the projection", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("g-a", 100))
    {:ok, _} = SQLite.append(server, member_started_op("g-a", 110, "leader", 0))
    {:ok, _} = SQLite.append(server, member_started_op("g-a", 111, "worker-0", 1))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_banked,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: "g-a",
        ts: 200,
        payload: %{
          set_id: "group/demo-group/set-a",
          members: [
            %{name: "leader", snapshot_ref: "group/demo-group/set-a/leader"},
            %{name: "worker-0", snapshot_ref: "group/demo-group/set-a/worker-0"}
          ]
        }
      })

    :ok = GenServer.stop(server)

    server2 = start_server(path)
    by_id = instance_by_id(server2)

    assert by_id["g-a"].state == "banked"
    assert by_id["g-a"].set_id == "group/demo-group/set-a"

    members = members_by_name(server2, "g-a")
    assert members["leader"].snapshot_ref == "group/demo-group/set-a/leader"
    assert members["worker-0"].snapshot_ref == "group/demo-group/set-a/worker-0"
    assert members["leader"].vm_id == nil

    :ok = GenServer.stop(server2)
  end

  test "a fresh DB reports group_instance_id on group ops through read_from, NULL on stats",
       %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("g-r", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :group_stats,
        tenant: "t1",
        principal: "p1",
        workload: "demo-group",
        group_instance_id: nil,
        ts: 101,
        payload: %{member_count: 2, usage: %{vcpu_seconds: 1.0, gb_seconds: 1.0}}
      })

    {:ok, ops} = SQLite.read_from(server, 0)

    created = Enum.find(ops, &(&1.kind == :group_created))
    assert created.group_instance_id == "g-r"
    assert created.task_id == nil
    assert created.session_id == nil
    assert created.serving_instance_id == nil
    assert created.stateful_instance_id == nil

    stats = Enum.find(ops, &(&1.kind == :group_stats))
    assert stats.group_instance_id == nil
    assert stats.principal == "p1"

    :ok = GenServer.stop(server)
  end
end
