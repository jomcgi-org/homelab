defmodule Embervm.Scheduler.Inflight do
  @moduledoc """
  The control plane's in-flight placement ledger: memory a placer has committed
  to a brick at ASSUME time (before the boot RPC) that the brick's own
  `NodeStatus` does not yet reflect.

  `NodeCapacity` only changes when a daemon reports, every couple of seconds or
  at the end of a boot. Every placer reads that same row, so without this ledger
  a burst of creates scored on one stale snapshot herds onto the same head brick
  (the 2026-10-10 whole-brick OOMKill). Every new-VM placement therefore charges
  its declared memory here first, and `Embervm.Scheduler.place/1` subtracts
  `pending_mib/2` from each candidate brick before it filters and scores. This is
  Kubernetes' "assume" step, scoped to the window between the claim and the
  daemon's next report that already counts the VM.

  ## lifecycle of one claim

    * `claim(dial_id, ref, mem_mib)`: the placer committed `ref` to the brick
      dialled as `dial_id`. Made synchronously by the placing process, before the
      boot RPC, so the next placement that process (or any other) makes sees it.
    * `release(dial_id, ref)`: the boot RPC failed or the brick refused it. The debit drops
      at once, so a refused brick is placeable again immediately.
    * `settle(dial_id, ref)`: the boot RPC succeeded. The debit is kept for a short hold
      (default 5_000 ms) so the daemon's next `NodeStatus`, which already counts
      the VM, lands before the debit drops. Dropping it at once would reopen the
      herd window for one report interval.

  Every row carries an ABSOLUTE expiry (default 200_000 ms, above the
  create worker timeout) so a claimer that crashes before it releases cannot
  leak a debit: expired rows are ignored on read and swept on every claim and by
  `sweep/1`. `settle/3` only ever shortens a row's expiry.

  ## shape and ownership

  A public ETS `:set` keyed `{dial_id, ref}` with rows
  `{{dial_id, ref}, mem_mib, expires_at_ms}`, where `dial_id` is
  `Embervm.Brick.dial_id/1` (the instance id, `"node/pod_uid"`). It is a pure
  accessor module with no process, like `Embervm.NodeCapacity`:
  `Embervm.NodeRegistry` creates the shared table next to the capacity table and
  drops a brick's rows (`drop/2`) when it forgets the brick (expiry). A brick
  that is only retracted for a while (a stream blip, a drain) keeps its debits,
  so it never comes back looking emptier than the boots still racing it.
  Every function takes `table:` in its options so tests use a private table, and
  `now:` (monotonic ms) so expiry is deterministic.

  ## guarantees, and what it is not

  Serial placements inside one process never herd: the claim lands before that
  process places again. Placements racing in two processes can both read the
  same pending sum in the microseconds between read and claim; the daemon's own
  admission (its `mem_admittable_mib`, which counts its in-flight boots) is the
  authority that refuses the loser, and a refusal is retried elsewhere or
  later, never an overcommit. Reads and writes never raise: a missing table (the registry is
  restarting) reads as no pending memory and claims become no-ops, the same
  fail-open posture as the shadow `Embervm.Scheduler.Reservation`, while the
  capacity table those reads depend on is itself empty in that window.
  """

  @table :embervm_inflight
  @default_ttl_ms 200_000
  @default_hold_ms 5_000

  @doc "The shared table name every function defaults to."
  @spec table() :: atom()
  def table, do: @table

  @doc "The absolute expiry, in ms, of an unsettled claim."
  @spec default_ttl_ms() :: pos_integer()
  def default_ttl_ms, do: @default_ttl_ms

  @doc "How long, in ms, a settled claim keeps its debit."
  @spec default_hold_ms() :: pos_integer()
  def default_hold_ms, do: @default_hold_ms

  @doc """
  Creates the ledger table. Idempotent: a table that already exists is shared,
  and ownership stays with whichever process created it first.
  """
  @spec create(atom()) :: atom()
  def create(table \\ @table) do
    case :ets.whereis(table) do
      :undefined ->
        :ets.new(table, [:set, :public, :named_table, read_concurrency: true, write_concurrency: true])

      _ ->
        table
    end
  end

  @doc """
  Debits `mem_mib` against `dial_id` for `ref` until it is released, settled and
  held, or expired (`ttl_ms`, default #{@default_ttl_ms}). Re-claiming the same
  `{dial_id, ref}` replaces the row. Also sweeps expired rows.
  """
  @spec claim(String.t(), term(), integer(), keyword()) :: :ok
  def claim(dial_id, ref, mem_mib, opts \\ []) when is_integer(mem_mib) do
    table = table(opts)
    now = now(opts)
    ttl = Keyword.get(opts, :ttl_ms, @default_ttl_ms)

    safe(fn ->
      sweep_expired(table, now)
      :ets.insert(table, {{dial_id, ref}, max(mem_mib, 0), now + ttl})
    end)

    :ok
  end

  @doc "Drops `ref`'s debit on `dial_id` at once: the boot RPC failed or was refused."
  @spec release(String.t(), term(), keyword()) :: :ok
  def release(dial_id, ref, opts \\ []) do
    safe(fn -> :ets.delete(table(opts), {dial_id, ref}) end)
    :ok
  end

  @doc """
  The boot RPC succeeded: keep the debit only for `hold_ms` (default
  #{@default_hold_ms}) more, so the daemon's next report counts the VM before
  the debit drops. Never extends a row's expiry, and is a no-op for a row that
  no longer exists.
  """
  @spec settle(String.t(), term(), keyword()) :: :ok
  def settle(dial_id, ref, opts \\ []) do
    table = table(opts)
    hold_until = now(opts) + Keyword.get(opts, :hold_ms, @default_hold_ms)

    safe(fn ->
      case :ets.lookup(table, {dial_id, ref}) do
        [{key, mem_mib, expires_at}] -> :ets.insert(table, {key, mem_mib, min(expires_at, hold_until)})
        [] -> true
      end
    end)

    :ok
  end

  @doc "Drops `ref`'s debit on every brick: a claimer that died mid-flight."
  @spec release_ref(term(), keyword()) :: :ok
  def release_ref(ref, opts \\ []) do
    safe(fn -> :ets.match_delete(table(opts), {{:_, ref}, :_, :_}) end)
    :ok
  end

  @doc "Drops every debit on `dial_id`: the registry forgot the brick."
  @spec drop(String.t(), keyword()) :: :ok
  def drop(dial_id, opts \\ []) do
    safe(fn -> :ets.match_delete(table(opts), {{dial_id, :_}, :_, :_}) end)
    :ok
  end

  @doc """
  The unexpired memory claimed against `dial_id`: unsettled claims, and settled
  ones still inside their hold. 0 when the table does not exist.
  """
  @spec pending_mib(String.t(), keyword()) :: non_neg_integer()
  def pending_mib(dial_id, opts \\ []) do
    now = now(opts)
    spec = [{{{dial_id, :_}, :"$1", :"$2"}, [{:>, :"$2", now}], [:"$1"]}]

    case safe(fn -> :ets.select(table(opts), spec) end) do
      list when is_list(list) -> Enum.sum(list)
      _ -> 0
    end
  end

  @doc "Every unexpired row as `{dial_id, ref, mem_mib, expires_at_ms}`, for diagnostics and tests."
  @spec entries(keyword()) :: [{String.t(), term(), non_neg_integer(), integer()}]
  def entries(opts \\ []) do
    now = now(opts)
    spec = [{{{:"$1", :"$2"}, :"$3", :"$4"}, [{:>, :"$4", now}], [{{:"$1", :"$2", :"$3", :"$4"}}]}]

    case safe(fn -> :ets.select(table(opts), spec) end) do
      list when is_list(list) -> list
      _ -> []
    end
  end

  @doc "Deletes expired rows and returns how many it removed."
  @spec sweep(keyword()) :: non_neg_integer()
  def sweep(opts \\ []) do
    case safe(fn -> sweep_expired(table(opts), now(opts)) end) do
      n when is_integer(n) -> n
      _ -> 0
    end
  end

  defp sweep_expired(table, now) do
    :ets.select_delete(table, [{{:_, :_, :"$1"}, [{:"=<", :"$1", now}], [true]}])
  end

  defp table(opts), do: Keyword.get(opts, :table) || @table
  defp now(opts), do: Keyword.get_lazy(opts, :now, fn -> System.monotonic_time(:millisecond) end)

  # A missing table raises ArgumentError on every ETS call. The ledger is
  # advisory, so that reads as empty rather than crashing a placer.
  defp safe(fun) do
    fun.()
  rescue
    ArgumentError -> :error
  end
end
