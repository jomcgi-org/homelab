defmodule Embervm.AsyncWriterTest do
  @moduledoc """
  AsyncWriter (ADR embervm/014 decision 2): the off-hot-path oplog append queue.

  Assertions are COUNTER/order based, never identity based (the repo's hash-fragile
  test lesson from PR 2): we record the ORDER of applied op kinds and count appends,
  rather than matching on a specific op-log seq or pid.
  """
  use ExUnit.Case, async: true

  alias Embervm.AsyncWriter
  alias Embervm.OpLog.Op

  # A recording op-log backend. `append/2` pushes {kind, task_id} onto a shared
  # Agent list (in apply order) and returns a monotonically increasing seq, matching
  # the @behaviour Embervm.OpLog append/2 contract the writer calls. Two behaviours
  # keyed off the op's payload keep every stub in one module (no defmodule-in-a-test):
  #   * payload[:block] = waiter  -> announce {:appending, ...} and wait for :go,
  #     so a test can observe pending?/2 with an append held in flight.
  #   * payload[:fail] = true     -> return {:error, :disk_full} without recording.
  defmodule RecordingOpLog do
    def start, do: Agent.start_link(fn -> %{seq: 0, applied: []} end)

    def applied(agent), do: Agent.get(agent, & &1.applied) |> Enum.reverse()

    def count(agent), do: Agent.get(agent, &length(&1.applied))

    def append(_agent, %Op{payload: %{fail: true}}), do: {:error, :disk_full}

    def append(agent, %Op{payload: %{block: waiter}} = op) when is_pid(waiter) do
      send(waiter, {:appending, op.task_id, self()})

      receive do
        :go -> :ok
      end

      record(agent, op)
    end

    def append(agent, %Op{} = op), do: record(agent, op)

    defp record(agent, op) do
      seq =
        Agent.get_and_update(agent, fn s ->
          {s.seq + 1, %{s | seq: s.seq + 1, applied: [{op.kind, op.task_id} | s.applied]}}
        end)

      {:ok, seq}
    end
  end

  defp op(kind, task_id, payload \\ %{}) do
    %Op{kind: kind, tenant: "t", task_id: task_id, ts: 0, payload: payload}
  end

  defp entry(agent, kind, task_id, vm_id \\ nil, payload \\ %{}) do
    %{op: op(kind, task_id, payload), op_log_mod: RecordingOpLog, op_log: agent, vm_id: vm_id}
  end

  test "ops for one instance apply in submission order" do
    {:ok, agent} = RecordingOpLog.start()
    {:ok, writer} = AsyncWriter.start_link(name: nil)

    AsyncWriter.enqueue(writer, entry(agent, :assigned, "task-1"))
    AsyncWriter.enqueue(writer, entry(agent, :started, "task-1"))
    :ok = AsyncWriter.drain(writer)

    assert RecordingOpLog.applied(agent) == [{:assigned, "task-1"}, {:started, "task-1"}]
  end

  test "interleaved instances each keep their own submission order" do
    {:ok, agent} = RecordingOpLog.start()
    {:ok, writer} = AsyncWriter.start_link(name: nil)

    AsyncWriter.enqueue(writer, entry(agent, :assigned, "a"))
    AsyncWriter.enqueue(writer, entry(agent, :assigned, "b"))
    AsyncWriter.enqueue(writer, entry(agent, :started, "a"))
    AsyncWriter.enqueue(writer, entry(agent, :started, "b"))
    :ok = AsyncWriter.drain(writer)

    applied = RecordingOpLog.applied(agent)
    a_order = for {k, "a"} <- applied, do: k
    b_order = for {k, "b"} <- applied, do: k

    assert a_order == [:assigned, :started]
    assert b_order == [:assigned, :started]
    assert RecordingOpLog.count(agent) == 4
  end

  test "pending?/2 is true while a vm's append is in flight, false once applied" do
    {:ok, agent} = RecordingOpLog.start()
    {:ok, writer} = AsyncWriter.start_link(name: nil)

    AsyncWriter.enqueue(writer, entry(agent, :started, "task-vm", "vm-1", %{block: self()}))

    # The writer has entered the (blocked) append: it must report the vm pending.
    assert_receive {:appending, "task-vm", appender}
    assert AsyncWriter.pending?(writer, "vm-1")
    refute AsyncWriter.pending?(writer, "vm-2")

    # Release the append; after drain the vm is no longer pending.
    send(appender, :go)
    :ok = AsyncWriter.drain(writer)

    refute AsyncWriter.pending?(writer, "vm-1")
    assert RecordingOpLog.count(agent) == 1
  end

  test "two in-flight appends for one vm keep it pending until both apply" do
    {:ok, agent} = RecordingOpLog.start()
    {:ok, writer} = AsyncWriter.start_link(name: nil)

    # assigned then started, both for vm-1, both blocking: the vm must stay pending
    # across the first's completion, cleared only after the second applies (the
    # count, not set, semantics).
    AsyncWriter.enqueue(writer, entry(agent, :assigned, "task-vm", "vm-1", %{block: self()}))
    AsyncWriter.enqueue(writer, entry(agent, :started, "task-vm", "vm-1", %{block: self()}))

    assert_receive {:appending, "task-vm", first}
    assert AsyncWriter.pending?(writer, "vm-1")
    send(first, :go)

    assert_receive {:appending, "task-vm", second}
    # First applied, second in flight: still pending (two appends were tracked).
    assert AsyncWriter.pending?(writer, "vm-1")
    send(second, :go)

    :ok = AsyncWriter.drain(writer)
    refute AsyncWriter.pending?(writer, "vm-1")
    assert RecordingOpLog.count(agent) == 2
  end

  test "a crash before the append loses the op (documented: repaired by reconcile)" do
    {:ok, agent} = RecordingOpLog.start()
    {:ok, writer} = AsyncWriter.start_link(name: nil)

    # An op enqueued but never given a chance to apply (we kill the writer with the
    # cast still in its mailbox) leaves NOTHING durable. This encodes the ADR's
    # accepted crash-loss window; the adoption backfill (session_manager) is the
    # repair, exercised in session_manager_test.
    :ok = GenServer.cast(writer, {:enqueue, entry(agent, :assigned, "doomed")})
    Process.exit(writer, :kill)
    ref = Process.monitor(writer)
    assert_receive {:DOWN, ^ref, :process, ^writer, :killed}

    assert RecordingOpLog.count(agent) == 0
  end

  test "graceful shutdown drains queued appends (terminate/2 flush)" do
    {:ok, agent} = RecordingOpLog.start()
    {:ok, writer} = AsyncWriter.start_link(name: nil)

    # Enqueue several and immediately stop gracefully: terminate/2 must flush the
    # whole mailbox, so a normal CP roll loses none of them.
    for i <- 1..5, do: AsyncWriter.enqueue(writer, entry(agent, :assigned, "task-#{i}"))
    :ok = GenServer.stop(writer, :normal)

    assert RecordingOpLog.count(agent) == 5
  end

  test "append failure does not take the writer or other appends down" do
    {:ok, agent} = RecordingOpLog.start()
    {:ok, writer} = AsyncWriter.start_link(name: nil)

    AsyncWriter.enqueue(writer, entry(agent, :assigned, "boom", "vm-boom", %{fail: true}))
    AsyncWriter.enqueue(writer, entry(agent, :assigned, "ok"))
    :ok = AsyncWriter.drain(writer)

    assert Process.alive?(writer)
    # The good append still landed; the failed one did not; the failed vm is no
    # longer pending (untracked even on failure, so the reconciler is not misled).
    assert RecordingOpLog.applied(agent) == [{:assigned, "ok"}]
    refute AsyncWriter.pending?(writer, "vm-boom")
  end
end
