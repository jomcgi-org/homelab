defmodule Embervm.SyncWait do
  @moduledoc """
  Sync-submit parking: `POST /v1/workloads/{name}/tasks?wait=true` blocks the
  caller until the task reaches a terminal state (or its timeout), rather than
  returning 202 immediately. The spec requires a PARKED BEAM process, not a poll
  loop, and a per-principal cap on how many parked waiters one principal may hold
  (default 512), 429-ing beyond it (the miss-path wake-rate abuse guard).

  Two primitives, both operating from the REQUEST process so the block never
  serializes through a GenServer:

    * a `Registry` (`Embervm.TaskWaiters`, duplicate keys) the request process
      registers itself in under the `task_id`; `TaskStore` calls `notify/2` on
      every terminal transition, which `Registry.dispatch`es a message to each
      parked waiter for that task.
    * a named public ETS counter table (`:embervm_park_counts`, one row per
      principal) reserved/released around the park so the cap is enforced with a
      single atomic `:ets.update_counter`, no lock.

  This GenServer's ONLY job is to own the ETS table's lifecycle (create it on
  init, so it dies with the supervision subtree). It handles no calls on the hot
  path. The `Registry` is a sibling child in the supervision tree.
  """
  use GenServer

  @registry Embervm.TaskWaiters
  @counts :embervm_park_counts

  # -- Client API ----------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @doc """
  Reserves one park slot for `principal`, failing closed at `cap`. Returns `:ok`
  when the reservation succeeds (the caller MUST `release/1` it when done), or
  `{:error, :park_cap_exceeded}` when `principal` already holds `cap` parked
  waiters. The increment and the over-cap rollback are two atomic counter ops, so
  concurrent reservers cannot both slip past the cap.
  """
  @spec reserve(String.t(), pos_integer()) :: :ok | {:error, :park_cap_exceeded}
  def reserve(principal, cap) do
    count = :ets.update_counter(@counts, principal, {2, 1}, {principal, 0})

    if count > cap do
      # Roll our own increment back out; another reserver may sit at the cap,
      # which is correct (they hold a real slot, we do not).
      :ets.update_counter(@counts, principal, {2, -1, 0, 0})
      {:error, :park_cap_exceeded}
    else
      :ok
    end
  end

  @doc "Releases a park slot previously `reserve/2`d; clamps at zero."
  @spec release(String.t()) :: :ok
  def release(principal) do
    # The {principal, 0} default makes a release for a key that does not exist
    # yet (a release with no prior reserve) create it at 0 rather than raise;
    # combined with the {2, -1, 0, 0} clamp the count can never go negative.
    :ets.update_counter(@counts, principal, {2, -1, 0, 0}, {principal, 0})
    :ok
  end

  @doc """
  Parks the calling process until a terminal `notify/2` for `task_id` arrives or
  `timeout_ms` elapses. `already_terminal?` is a 0-arity function re-checked
  AFTER registration to close the race where the task settled between submit and
  registration: if it returns `{:terminal, state}` the parked wait is skipped.
  Always unregisters before returning so a keep-alive-reused request process does
  not leak a stale waiter registration.
  """
  @spec await(String.t(), non_neg_integer(), (-> {:terminal, atom()} | :pending)) ::
          {:terminal, atom()} | :timeout
  def await(task_id, timeout_ms, already_terminal?) do
    {:ok, _} = Registry.register(@registry, task_id, nil)

    try do
      case already_terminal?.() do
        {:terminal, state} ->
          {:terminal, state}

        :pending ->
          receive do
            {:task_terminal, ^task_id, state} -> {:terminal, state}
          after
            timeout_ms -> :timeout
          end
      end
    after
      Registry.unregister(@registry, task_id)
    end
  end

  @doc """
  Wakes every process parked on `task_id` with its terminal `state`. Called by
  `TaskStore` after a terminal write-through append; a no-op when nobody is
  parked (the common case), so it is cheap to call on every terminal transition.
  """
  @spec notify(String.t(), atom()) :: :ok
  def notify(task_id, state) do
    Registry.dispatch(@registry, task_id, fn entries ->
      for {pid, _} <- entries, do: send(pid, {:task_terminal, task_id, state})
    end)

    :ok
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(_opts) do
    # Named + public so request processes hit it directly; write_concurrency
    # because reserve/release from many request processes race on distinct keys.
    :ets.new(@counts, [:set, :public, :named_table, write_concurrency: true])
    {:ok, %{}}
  end
end
