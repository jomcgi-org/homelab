defmodule Embervm.OpLogPayloadsTest do
  use ExUnit.Case, async: true

  alias Embervm.OpLog.SQLite
  alias Embervm.PrimedOp
  alias Embervm.SessionStore
  alias Embervm.TaskStore

  setup do
    path = Path.join(System.tmp_dir!(), "embervm_op_payloads_#{System.unique_integer([:positive, :monotonic])}.db")
    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp clock do
    {:ok, agent} = Agent.start_link(fn -> 1_000 end)
    fn -> Agent.get_and_update(agent, fn value -> {value, value + 1} end) end
  end

  defp read_kind(op_log, kind) do
    {:ok, ops} = SQLite.read_from(op_log, 0)
    Enum.find(ops, &(&1.kind == kind))
  end

  test "sync assigned and started payloads use the integer task attempt", %{path: path} do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)

    {:ok, store} =
      TaskStore.start_link(
        op_log: op_log,
        name: nil,
        clock: clock(),
        on_queued: fn _ -> :ok end,
        async_lifecycle_writes: false
      )

    {:ok, :created, task_id} = TaskStore.submit(store, %{tenant: "t", principal: "p", workload: "w"})
    {:ok, _} = TaskStore.assign(store, task_id, "vm-1", "node-1")
    {:ok, _} = TaskStore.start(store, task_id, "vm-1", "node-1")

    for kind <- [:assigned, :started] do
      op = read_kind(op_log, kind)
      assert is_integer(op.payload["epoch"])
      assert op.payload["vm_id"] == "vm-1"
      assert op.payload["node_id"] == "node-1"
      refute op.payload["epoch"] == :task_attempt
    end
  end

  test "session_created payload contains vm_id and node_id", %{path: path} do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    {:ok, store} = SessionStore.start_link(op_log: op_log, name: nil, clock: clock(), async_lifecycle_writes: false)

    {:ok, created} =
      SessionStore.create(store, %{
        tenant: "t",
        principal: "p",
        workload: "w",
        vm_id: "vm-1",
        node_id: "node-1"
      })

    op = read_kind(op_log, :session_created)
    assert op.session_id == created.session_id
    assert op.payload["vm_id"] == "vm-1"
    assert op.payload["node_id"] == "node-1"
  end

  test "sync session_relit preserves residency and relight metadata", %{path: path} do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    {:ok, store} = SessionStore.start_link(op_log: op_log, name: nil, clock: clock(), async_lifecycle_writes: false)

    {:ok, created} =
      SessionStore.create(store, %{
        tenant: "t",
        principal: "p",
        workload: "w",
        vm_id: "vm-1",
        node_id: "node-1"
      })

    {:ok, _} = SessionStore.mark(store, created.session_id, :bank)
    {:ok, _} = SessionStore.transition(store, created.session_id, :bank_ready, :session_banked, %{snapshot_ref: "snap-1", generation: 3}, %{})
    {:ok, _} = SessionStore.mark(store, created.session_id, :relight)

    {:ok, _} =
      SessionStore.transition_lifecycle(
        store,
        created.session_id,
        :relight_ready,
        :session_relit,
        %{snapshot_ref: "snap-2", generation: 4, relight_ms: 27, vm_id: "vm-2", node_id: "node-2"},
        %{snapshot_ref: "snap-2", generation: 4, vm_id: "vm-2", node_id: "node-2"},
        "vm-2"
      )

    op = read_kind(op_log, :session_relit)
    assert op.payload["vm_id"] == "vm-2"
    assert op.payload["node_id"] == "node-2"
    assert op.payload["snapshot_ref"] == "snap-2"
    assert op.payload["generation"] == 4
    assert op.payload["relight_ms"] == 27
  end

  test "PrimedOp.build produces consistent payload shape across lanes" do
    task = PrimedOp.build("homelab", "wl", "vm-task", "node-4", :task)
    session = PrimedOp.build("homelab", "wl", "vm-session", "node-4", :session)

    assert MapSet.new(Map.keys(task.payload)) == MapSet.new(Map.keys(session.payload))
    assert MapSet.new(Map.keys(task.payload)) == MapSet.new([:lane, :workload, :vm_id, :node_id])
  end

  # ts is taken inside the builder on purpose (see Embervm.PrimedOp), because
  # the two calling modules spell a monotonic and a wall clock the same way.
  # A monotonic value has an arbitrary origin, so this asserts the op carries a
  # plausible epoch-millisecond timestamp rather than an interval counter.
  test "PrimedOp.build stamps a wall-clock timestamp, not a monotonic one" do
    op = PrimedOp.build("homelab", "wl", "vm-1", "node-4", :task)
    now = System.system_time(:millisecond)

    assert is_integer(op.ts)
    assert op.ts > 1_700_000_000_000, "ts is not a plausible epoch millisecond"
    assert abs(now - op.ts) < 60_000
  end
end
