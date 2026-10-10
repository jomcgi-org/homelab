defmodule Embervm.LineageFence do
  @moduledoc """
  Per-lineage mutual exclusion between a session workspace restore and the S3
  warmth GC's delete of that lineage's last durable copy (#6736, the A1
  assumption in `specs/warmth_gc.tla`).

  `SessionManager.validate_restore_lineage/4` admits a create that restores a
  TERMINAL lineage, and the heir row that would make the lineage referenced is
  written only after the restore worker finishes. In that window the GC's
  `recheck_live/2` sees no reference, so its `delete_prefix/2` could remove the
  workspace export the worker is about to read. noded fails such a restore
  closed (`NotFound`), but the last durable copy is already gone.

  The fence is one named public ETS table holding at most one row per lineage,
  `{lineage, role, pid}` with `role` either `:restore` or `:gc`. A claim is a
  single `:ets.insert_new/2`, which is atomic per object, so exactly one side
  wins and the other backs off: the GC skips that prefix for this sweep (the
  next sweep re-plans from a fresh listing) and the create is denied with the
  retryable `:lineage_restore_in_flight` reason. No ordering argument across
  two keys is needed.

  Fail posture follows invariant 4. The GC side is enforcement, so when the
  table is absent `claim_gc/1` refuses and the GC holds the prefix. The restore
  side is warmth, so an absent table lets the restore proceed exactly as before
  the fence existed (every SessionManager test that starts no supervision tree
  keeps its behaviour).

  This GenServer's ONLY job is to own the table's lifecycle. Claims are made
  from the claimant's own process; nothing calls through the server. Both
  claimants clear their own role's rows on init, so a crashed SessionManager
  or GC never leaves a stale claim behind its restart.
  """
  use GenServer

  @table :embervm_lineage_fence

  @type lineage :: String.t() | nil
  @type role :: :restore | :gc

  # -- Client API ----------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @doc "The fence table name, for diagnostics and tests."
  @spec table() :: atom()
  def table, do: @table

  @doc """
  Claim `lineage` for a workspace restore. `:ok` when the restore may proceed
  (the caller MUST `release_restore/1` when the create finishes, success or
  failure). `{:error, :gc_in_progress}` when the GC is deleting the lineage's
  prefix right now; `{:error, :restore_in_flight}` when another restore already
  holds it. A nil or empty lineage, or an absent table, is `:ok` (fail open:
  the restore is warmth).
  """
  @spec claim_restore(lineage()) :: :ok | {:error, :gc_in_progress | :restore_in_flight}
  def claim_restore(lineage) when lineage in [nil, ""], do: :ok

  def claim_restore(lineage) when is_binary(lineage) do
    case claim(lineage, :restore) do
      :ok -> :ok
      {:error, :fence_unavailable} -> :ok
      {:error, :gc} -> {:error, :gc_in_progress}
      {:error, :restore} -> {:error, :restore_in_flight}
    end
  end

  @doc "Release a restore claim. Idempotent; a claim held by the GC is untouched."
  @spec release_restore(lineage()) :: :ok
  def release_restore(lineage), do: release(lineage, :restore)

  @doc """
  Claim `lineage` for a GC delete. `:ok` when the delete may proceed (the
  caller MUST `release_gc/1` afterwards, success or failure).
  `{:error, :restore_in_flight}` when a restore of the lineage is in flight,
  `{:error, :gc_in_progress}` when another sweep holds it, and
  `{:error, :fence_unavailable}` when the table does not exist (fail closed:
  the delete is enforcement). A nil or empty lineage is `:ok`.
  """
  @spec claim_gc(lineage()) ::
          :ok | {:error, :restore_in_flight | :gc_in_progress | :fence_unavailable}
  def claim_gc(lineage) when lineage in [nil, ""], do: :ok

  def claim_gc(lineage) when is_binary(lineage) do
    case claim(lineage, :gc) do
      :ok -> :ok
      {:error, :fence_unavailable} -> {:error, :fence_unavailable}
      {:error, :restore} -> {:error, :restore_in_flight}
      {:error, :gc} -> {:error, :gc_in_progress}
    end
  end

  @doc "Release a GC claim. Idempotent; a claim held by a restore is untouched."
  @spec release_gc(lineage()) :: :ok
  def release_gc(lineage), do: release(lineage, :gc)

  @doc "Drop every claim of `role`. Owners call this on init so a restart never inherits a stale claim."
  @spec clear(role()) :: :ok
  def clear(role) when role in [:restore, :gc] do
    if table_present?(), do: :ets.match_delete(@table, {:_, role, :_})
    :ok
  end

  @doc "Whether `lineage` is currently claimed by `role`."
  @spec held?(lineage(), role()) :: boolean()
  def held?(lineage, role) when is_binary(lineage) and role in [:restore, :gc] do
    table_present?() and :ets.match(@table, {lineage, role, :_}) != []
  end

  def held?(_lineage, _role), do: false

  # -- Internals -------------------------------------------------------------

  defp claim(lineage, role) do
    if table_present?() do
      if :ets.insert_new(@table, {lineage, role, self()}) do
        :ok
      else
        case :ets.lookup(@table, lineage) do
          [{^lineage, holder, _pid}] -> {:error, holder}
          # The holder released between insert_new and lookup: retry once; a
          # second miss means a concurrent claimant won in between.
          [] -> if :ets.insert_new(@table, {lineage, role, self()}), do: :ok, else: {:error, role}
        end
      end
    else
      {:error, :fence_unavailable}
    end
  end

  defp release(lineage, _role) when lineage in [nil, ""], do: :ok

  defp release(lineage, role) when is_binary(lineage) do
    if table_present?(), do: :ets.match_delete(@table, {lineage, role, :_})
    :ok
  end

  defp table_present?, do: :ets.whereis(@table) != :undefined

  # -- GenServer ---------------------------------------------------------------

  @impl true
  def init(_opts) do
    _ =
      :ets.new(@table, [
        :set,
        :public,
        :named_table,
        read_concurrency: true,
        write_concurrency: true
      ])

    {:ok, %{}}
  end
end
