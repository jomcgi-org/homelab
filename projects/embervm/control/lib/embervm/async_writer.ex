defmodule Embervm.AsyncWriter do
  @moduledoc """
  Off-hot-path durable oplog appends (ADR embervm/014 decision 2), gated by
  `EMBERVM_ASYNC_LIFECYCLE_WRITES`.

  Today the boot/wake paths write the `:assigned`/`:started` (task dispatch) and
  `:session_created`/`:session_relit` (session boot/wake) ops write-through: the
  durable oplog append lands BEFORE the instance is handed to the caller, so a
  Postgres/SQLite round trip sits in front of every boot and wake. ADR 014
  decision 2 takes that append off the hot path: the instance becomes interactive
  first (RPC success + in-memory state advanced), and the durable append runs
  asynchronously afterward through this writer.

  ## What is and is NOT moved

  Only the four lifecycle appends above move. Explicitly synchronous and never
  routed here:

    * `:submitted` (the quota audit trail: a task must be durable before it can be
      charged),
    * every metering/usage op (fail-closed: the quota charge fires inside the
      terminal op's own write-through transaction, so it can never precede that
      op's durable append),
    * every destruction op (PR 1: destroyed is node-confirmed and synchronous),
    * every bank op (a snapshot's identity must be durable before eviction can act
      on it; bank is not a hot path).

  ## Ordering and the ETS/append split

  The in-memory (ETS) projection is advanced SYNCHRONOUSLY by the owning store
  before an op is enqueued here (the store's ETS tables are `:private`, so only
  the store process may mutate them, and the FSM's terminal transitions -
  `:succeeded`/`:session_relit` etc. - must see the instance already in
  `:assigned`/`:running`/`:relighting`). This writer therefore carries ONLY the
  deferred durable append: it calls `op_log_mod.append(op_log, op)` and nothing
  else. That is a deliberate divergence from an "append-then-mutate-ETS" reading
  of the ADR: mutating ETS here is both impossible (private tables) and wrong
  (a terminal transition would race a not-yet-applied assign). The trade is that
  the durable log can lag the in-memory view by one append; a CP crash in that
  window loses the op, which the adoption reconcile repairs (below).

  Appends are applied strictly FIFO from this GenServer's mailbox, so ops for one
  instance land in submission order (the mailbox gives global order, which is
  stronger than the per-instance order the ADR requires; a per-scheduler pool is
  deliberately YAGNI until CI perf shows single-writer contention).

  ## The pending registry (a public ETS table)

  In-flight appends are tracked by `vm_id` in a PUBLIC ETS counter table this
  process owns, NOT in the GenServer state. That is deliberate: an append can
  block inside `handle_cast/2` (a slow op-log), and a `pending?/2` reader must
  still get an answer while that append is in flight (a GenServer `call` would sit
  behind the blocked cast in the mailbox and deadlock). The count is incremented
  the moment an op is enqueued (before it can apply) and decremented after it
  applies, so a reader sees a VM as pending for exactly the enqueue..applied
  window.

  ## Crash-loss and its repair

  A crash between `enqueue/2` and the append inside `handle_cast/2` loses that op:
  the instance is running (or its VM is live on the node) but its durable row is
  absent. This is documented and intended. The repair is the adoption reconcile's
  adopt-and-backfill path (PR 1's Direction-2 orphan pass, extended in PR 3): a
  node reports the live VM, the CP has no row, but a pending async write (tracked
  here via `pending?/2`) OR an existing task/session record references the
  `vm_id`, so the instance is ADOPTED and its missing ops backfilled, never
  destroyed.

  On a GRACEFUL shutdown (a normal CP roll), `terminate/2` drains every queued
  append before the process exits, so a planned rollout loses nothing; only an
  abnormal exit (SIGKILL, a crash) can drop the in-flight queue, and only that
  narrow window relies on the reconcile repair.
  """
  use GenServer

  require Logger

  alias Embervm.OpLog.Op

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    name = Keyword.get(opts, :name, __MODULE__)
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  @doc """
  Enqueue one deferred durable append.

  `entry` is `%{op: %Op{}, op_log_mod: module, op_log: server, vm_id: binary | nil}`.
  The append is `op_log_mod.append(op_log, op)`; `vm_id` (when set) registers this
  op as an in-flight write so `pending?/2` can answer the adopt-and-backfill
  discriminator until the append lands. Fire-and-forget: a cast, so the caller
  (the dispatch/create hot path) never blocks on the durable write. The pending
  count is bumped SYNCHRONOUSLY here (from the caller), before the cast, so a
  reader can never observe a gap between enqueue and the vm being pending.
  """
  @spec enqueue(GenServer.server(), map()) :: :ok
  def enqueue(server \\ __MODULE__, entry) when is_map(entry) do
    table = table_for(server)
    inc_pending(table, Map.get(entry, :vm_id))
    GenServer.cast(server, {:enqueue, entry, table})
  end

  @doc """
  Does an as-yet-unapplied durable append reference `vm_id`? The pending half of
  the reconciler's adopt-vs-destroy discriminator: a node reports a live VM the CP
  has no row for, but if a write for that VM is still in flight here it is a young
  async-write race to adopt, not an orphan to destroy. Reads the public ETS
  counter directly (no GenServer call), so it answers even while an append is
  blocked in flight.
  """
  @spec pending?(GenServer.server(), binary()) :: boolean()
  def pending?(server \\ __MODULE__, vm_id) when is_binary(vm_id) do
    table = table_for(server)

    case :ets.lookup(table, vm_id) do
      [{^vm_id, n}] when n > 0 -> true
      _ -> false
    end
  end

  @doc """
  Block until every queued append has been applied. Test/checkpoint helper: a
  synchronous call that returns only after the mailbox ahead of it has drained
  (mailbox FIFO), so a test can assert the durable effect without sleeping.
  """
  @spec drain(GenServer.server()) :: :ok
  def drain(server \\ __MODULE__) do
    GenServer.call(server, :drain, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    Process.flag(:trap_exit, true)
    name = Keyword.get(opts, :name, __MODULE__)
    # A public counter table (vm_id -> in-flight append count) this process owns, so
    # it dies with the writer. Named off the registered name when there is one (the
    # supervised singleton), else an anonymous table whose id we stash in
    # persistent_term keyed by our pid (unnamed test writers). pending?/2 and
    # enqueue/2 resolve the table without a GenServer round trip.
    table =
      if is_atom(name) and not is_nil(name) do
        :ets.new(pending_table_name(name), [:set, :public, :named_table])
      else
        tid = :ets.new(:embervm_async_writer_pending, [:set, :public])
        :persistent_term.put({__MODULE__, :table, self()}, tid)
        tid
      end

    {:ok, %{table: table}}
  end

  @impl true
  def handle_cast({:enqueue, entry, table}, state) do
    _ = apply_append(entry)
    dec_pending(table, Map.get(entry, :vm_id))
    {:noreply, state}
  end

  # A no-op reply whose ONLY purpose is to have travelled behind every enqueue cast
  # already in the mailbox: once this returns, those casts have been handled.
  @impl true
  def handle_call(:drain, _from, state) do
    {:reply, :ok, state}
  end

  # Graceful shutdown: process the remaining {:enqueue, _, _} casts still in the
  # mailbox so a normal CP roll flushes every pending append. Only reached on a
  # supervised/normal stop (trap_exit is set); a SIGKILL cannot run terminate/2, so
  # that (and only that) window depends on the adoption backfill repair.
  @impl true
  def terminate(_reason, _state) do
    flush_mailbox()
    :ok
  end

  # -- internals -------------------------------------------------------------

  defp apply_append(%{op: %Op{} = op, op_log_mod: mod, op_log: server}) do
    case mod.append(server, op) do
      {:ok, _seq} ->
        :ok

      {:error, reason} ->
        # A lost async append is exactly the crash-window case the reconcile
        # repairs: log it for telemetry and drop it rather than crash the writer
        # and take every other pending append down with it.
        Logger.warning("embervm async_writer append failed",
          kind: op.kind,
          task_id: op.task_id,
          session_id: op.session_id,
          reason: inspect(reason)
        )

        :ok
    end
  rescue
    e ->
      Logger.warning("embervm async_writer append crashed", kind: op.kind, error: inspect(e))
      :ok
  end

  # -- pending registry (public ETS counter) ---------------------------------

  defp inc_pending(_table, nil), do: :ok

  defp inc_pending(table, vm_id) do
    :ets.update_counter(table, vm_id, {2, 1}, {vm_id, 0})
    :ok
  end

  defp dec_pending(_table, nil), do: :ok

  defp dec_pending(table, vm_id) do
    # Decrement; drop the row once it reaches zero so the table does not grow
    # unbounded with settled vms. update_counter with a floor of 0 keeps it safe if
    # a decrement ever races ahead (it cannot today: one inc per enqueue, one dec per
    # apply).
    case :ets.update_counter(table, vm_id, {2, -1, 0, 0}, {vm_id, 0}) do
      0 -> :ets.delete(table, vm_id)
      _ -> :ok
    end

    :ok
  end

  defp table_for(server) when is_atom(server) and not is_nil(server) do
    pending_table_name(server)
  end

  defp table_for(server) do
    pid = GenServer.whereis(server)
    :persistent_term.get({__MODULE__, :table, pid})
  end

  defp pending_table_name(name), do: :"#{name}.Pending"

  # Drain every {:enqueue, _, _} cast still in the mailbox (terminate/2). Selective
  # receive with a zero timeout: pull enqueues until none remain, applying each and
  # settling its pending counter.
  defp flush_mailbox do
    receive do
      {:"$gen_cast", {:enqueue, entry, table}} ->
        _ = apply_append(entry)
        dec_pending(table, Map.get(entry, :vm_id))
        flush_mailbox()
    after
      0 -> :ok
    end
  end
end
