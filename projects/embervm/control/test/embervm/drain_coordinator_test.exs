defmodule Embervm.DrainCoordinatorTest do
  # The DrainCoordinator fans out a drain edge to the four class sweepers and records
  # the edge in the op-log. Both seams (the per-class drain call and the op-log
  # append) are injected so this exercises the coordinator's orchestration without
  # real sweeper processes or a SQLite backend.
  use ExUnit.Case, async: true

  alias Embervm.DrainCoordinator

  setup do
    test = self()

    drain_fun = fn class, _server, node_id ->
      send(test, {:drained, class, node_id})
      # A distinct scripted count per class so the finished-op payload is checkable.
      %{stateful: 2, group: 1, session: 3, serving: 4}[class]
    end

    append_fun = fn _op_log, op ->
      send(test, {:op, op.kind, op.payload})
      {:ok, 1}
    end

    {:ok, pid} =
      DrainCoordinator.start_link(
        name: nil,
        drain_fun: drain_fun,
        append_fun: append_fun,
        safety_margin_ms: 15_000,
        clock: fn -> 1_700_000_000_000 end
      )

    {:ok, pid: pid}
  end

  test "a drain edge force-banks all four classes and op-logs the edge", %{pid: pid} do
    send(pid, {:node_draining, "node-4", 1_700_000_120_000})

    # Every class sweeper is asked to drain the node.
    assert_receive {:drained, :stateful, "node-4"}
    assert_receive {:drained, :group, "node-4"}
    assert_receive {:drained, :session, "node-4"}
    assert_receive {:drained, :serving, "node-4"}

    # The started op records the deadline and safety margin.
    assert_receive {:op, :node_drain_started, started}
    assert started.node_id == "node-4"
    assert started.deadline_ms == 1_700_000_120_000
    assert started.safety_margin_ms == 15_000

    # The finished op records the per-class banked counts.
    assert_receive {:op, :node_drain_finished, finished}
    assert finished.node_id == "node-4"
    assert finished.stateful == 2
    assert finished.group == 1
    assert finished.session == 3
    assert finished.serving == 4
  end

  test "an instance-scoped drain (4-tuple) records the pod_uid on the ops", %{pid: pid} do
    send(pid, {:node_draining, "node-4", "uid-old", 1_700_000_120_000})

    assert_receive {:op, :node_drain_started, started}
    assert started.node_id == "node-4"
    assert started.pod_uid == "uid-old"

    assert_receive {:op, :node_drain_finished, finished}
    assert finished.pod_uid == "uid-old"
  end

  test "a class whose drain raises is skipped, the others still drain", %{pid: _pid} do
    test = self()

    drain_fun = fn
      :session, _server, _node_id -> raise "session sweeper down"
      class, _server, node_id -> send(test, {:drained, class, node_id}) && 1
    end

    append_fun = fn _op_log, op ->
      send(test, {:op, op.kind, op.payload})
      {:ok, 1}
    end

    {:ok, pid} =
      DrainCoordinator.start_link(name: nil, drain_fun: drain_fun, append_fun: append_fun)

    send(pid, {:node_draining, "node-4", 0})

    # The finished op is the deterministic completion signal for the sequential
    # fan-out. Give only that signal a scheduler budget (#4078), then inspect the
    # messages that necessarily preceded it without further wall-clock waits.
    assert_receive {:op, :node_drain_finished, finished}, 2_000
    assert_received {:op, :node_drain_started, _started}
    assert_received {:drained, :stateful, "node-4"}
    assert_received {:drained, :group, "node-4"}
    assert_received {:drained, :serving, "node-4"}
    refute_received {:drained, :session, "node-4"}

    # The failed class is counted as 0.
    assert finished.session == 0
    # The coordinator itself did not crash.
    assert Process.alive?(pid)
  end
end
