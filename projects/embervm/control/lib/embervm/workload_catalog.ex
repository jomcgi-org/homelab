defmodule Embervm.WorkloadCatalog do
  @moduledoc """
  Pure ETS accessor for the reconciled Workload catalog: NO process of its
  own. `Embervm.WorkloadWatcher` owns the table's lifecycle (creates it on
  init, writes to it after every reconcile), and this module is just the
  read/write surface both the watcher and readers (today, `Embervm.TaskStore`)
  call against a table name, never a PID.

  Splitting the accessor from the owning GenServer this way means a reader on
  the hot path (`cfg_for/1`, called from `TaskStore`'s `:fail` handling) never
  serializes through a `GenServer.call` to the watcher; it hits the
  `read_concurrency: true` ETS table directly. It also means the watcher
  restarting (its `:rest_for_one` position guarantees it does not carry
  `TaskStore` down with it) is safe: the old table dies with its owner
  process, and `create/1` on the fresh watcher just makes a clean new one, so
  a reader mid-restart sees "table not found", not a torn read.

  The table name defaults to `@table` for the application's supervised
  singleton, but every function also takes it as an explicit parameter so
  async tests can each use an isolated, uniquely-named table without any
  shared global state.
  """

  @table :embervm_workloads

  @doc "The ETS table name every function defaults to when none is given."
  @spec table() :: atom()
  def table, do: @table

  @doc """
  Creates the catalog table. `:set` (one row per workload name), `:public`
  (readers outside the owning process, i.e. every `cfg_for/1` caller, hit it
  directly), `:named_table` (looked up by atom name, not PID),
  `read_concurrency: true` (the read:write ratio here is enormously read-
  heavy: writes only happen once per reconcile interval, reads happen on
  every task failure across every workload).
  """
  @spec create(atom()) :: atom()
  def create(table \\ @table) do
    :ets.new(table, [:set, :public, :named_table, read_concurrency: true])
  end

  @spec upsert(atom(), String.t(), map()) :: true
  def upsert(table \\ @table, name, entry) do
    :ets.insert(table, {name, entry})
  end

  @spec drop(atom(), String.t()) :: true
  def drop(table \\ @table, name) do
    :ets.delete(table, name)
  end

  @doc "All workload names currently cataloged, in no particular order."
  @spec all_names(atom()) :: [String.t()]
  def all_names(table \\ @table) do
    :ets.select(table, [{{:"$1", :_}, [], [:"$1"]}])
  end

  @doc """
  Looks up one workload's catalog entry against the default table. See
  `fetch/2` for the table-parameterized form used by tests.

  Like `retry_config/1,2` below, `fetch` is split into two explicit clauses
  (`fetch/1`, `fetch/2`) rather than one function with a leading default
  argument (`def fetch(table \\\\ @table, name)`): see the rationale on
  `retry_config/1` for why.
  """
  @spec fetch(String.t()) :: {:ok, map()} | :error
  def fetch(name), do: fetch(@table, name)

  @spec fetch(atom(), String.t()) :: {:ok, map()} | :error
  def fetch(table, name) do
    case :ets.lookup(table, name) do
      [{^name, entry}] -> {:ok, entry}
      [] -> :error
    end
  end

  @doc """
  The retry config `Embervm.TaskStore.cfg_for/1` classifies failures against.
  Falls back to `Embervm.Retry.default_config/0` when the table does not
  exist yet (the watcher has not booted, or is between a crash and its
  restart) OR the workload is not (yet, or no longer) cataloged, so a task
  failure is never blocked on the watcher's reconcile cadence.

  Defined as two explicit clauses (`retry_config/1`, `retry_config/2`) rather
  than one function with a leading default argument
  (`def retry_config(table \\\\ @table, name)`): a default value on a
  non-final argument is valid Elixir (it expands into multiple function head
  clauses under the hood), but the explicit two-clause form here is
  unambiguous by inspection and does not rely on that expansion behaving as
  expected without a compiler available to verify it in this environment.
  """
  @spec retry_config(String.t()) :: Embervm.Retry.retry_config()
  def retry_config(name), do: retry_config(@table, name)

  @spec retry_config(atom(), String.t()) :: Embervm.Retry.retry_config()
  def retry_config(table, name) do
    if :ets.whereis(table) == :undefined do
      Embervm.Retry.default_config()
    else
      case fetch(table, name) do
        {:ok, entry} -> entry.retry
        :error -> Embervm.Retry.default_config()
      end
    end
  end

  @doc "Whether permanent task failures should enter the dead-letter queue."
  @spec dead_letter_enabled?(String.t()) :: boolean()
  def dead_letter_enabled?(name), do: dead_letter_enabled?(@table, name)

  @spec dead_letter_enabled?(atom(), String.t()) :: boolean()
  def dead_letter_enabled?(table, name) do
    if :ets.whereis(table) == :undefined do
      true
    else
      case fetch(table, name) do
        {:ok, %{dead_letter_enabled: false}} -> false
        _ -> true
      end
    end
  end
end
