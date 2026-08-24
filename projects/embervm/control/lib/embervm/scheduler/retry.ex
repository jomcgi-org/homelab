defmodule Embervm.Scheduler.Retry do
  @moduledoc """
  The ONE reject/retry placement policy every NEW-placement boot path shares (ADR
  embervm/014 decision 3: "placement is reject/retry, not ledger-perfect"). A brick
  that cannot honour a boot RPC rejects it cheaply with `RESOURCE_EXHAUSTED` (the
  node-side pressure predicate, `pressure:mem` / `pressure:taps`); rather than fail
  the create, this helper drops that brick from the retry frontier (marking it
  ineligible for the rest of THIS attempt) and tries the next candidate. The
  advisory downward-headroom refresh ADR 014 decision 3 calls for ("decrement its
  cached headroom view immediately, without waiting for the next node report") is
  the caller's own concern, delegated through the optional `:on_reject` hook since
  only the caller holds that view.

  With multiple bricks per size class (ADR 013) a wrong control-plane guess costs
  one extra RPC instead of a wedge. Retries are bounded (`max_attempts`, default 3,
  ADR: "a wrong guess costs one extra RPC"); once exhausted the caller gets a fast
  explicit `{:error, :no_capacity}`, never a queue wedge.

  ## the gate

  The whole retry behaviour is behind `EMBERVM_PLACEMENT_RETRY` (default OFF). Gate
  OFF is exactly today's single-attempt behaviour: one candidate, one RPC, its
  result returned verbatim. This is the `EMBERVM_WARMTH_RETENTION_SWEEP`-style
  off-by-default posture every ADR-014 change ships behind, flipped in a later
  values-only commit once proven.

  ## why a higher-order function, not a GenServer

  The four create paths (task dispatch miss, session create, serving create,
  stateful/group create) each own their capacity read, their brick pick, and their
  boot RPC (`Prime` / `StartServing` / `StartStateful` / `StartGroupMember`). This
  module owns only the POLICY that stitches them: "call `attempt_fun` against the
  ordered candidates, treat a `RESOURCE_EXHAUSTED` as retryable, stop at the first
  success or after `max_attempts`." The caller passes:

    * `candidates` - the ordered brick list it already computed
      (`the prior class-specific query` output, or any list of maps carrying at least
      `:instance_id` and the advisory `:mem_headroom_mib`), best candidate first;
    * `attempt_fun` - a 1-arity closure over one brick that issues the caller's
      boot RPC and returns `{:ok, result}`, `{:reject, reason}` (retryable node
      pressure), or `{:error, reason}` (a terminal failure that must NOT retry,
      e.g. a transport error or a bad request);

  so the policy is one piece of code and a node-anchored path (a stateful/group
  RELIGHT that can only target the volume's own node) simply passes a
  single-element candidate list: the loop then degrades to exactly one attempt,
  which is correct (a relight cannot move nodes) and still routes through this one
  helper.

  Every function here is pure over its inputs (the candidate list and the closures
  the caller supplies); it holds no process state and reads no ETS of its own, so
  it never drifts from the capacity view its caller dispatched against.
  """

  require Logger

  @default_max_attempts 3

  @typedoc "A brick/candidate map carrying at least `:instance_id` and advisory `:mem_headroom_mib`."
  @type candidate :: map()

  @typedoc """
  What one boot attempt against a brick returns:

    * `{:ok, result}` - the boot succeeded; `result` is returned to the caller.
    * `{:reject, reason}` - retryable node-pressure rejection (RESOURCE_EXHAUSTED);
      try the next candidate.
    * `{:error, reason}` - a terminal failure; stop and return it (no retry).
  """
  @type attempt_result :: {:ok, term()} | {:reject, term()} | {:error, term()}

  @doc """
  Run the reject/retry policy over `candidates`, calling `attempt_fun` on each in
  order until one succeeds, a terminal error occurs, or the attempt budget is spent.

  Options:

    * `:on_reject` - a 2-arity `fn brick, reason -> any end` invoked once per
      rejection, for the caller's ADVISORY downward-headroom refresh (ADR 014
      decision 3: "decrement its cached headroom view immediately"): the caller
      lowers the rejected brick's headroom in its own capacity view so a later pick
      in the same create burst does not re-choose it as if it still had room. The
      retry list itself already drops the brick (the in-attempt "mark ineligible");
      this hook is for the caller's separately-held view. Default no-op. A hook that
      raises is swallowed (a caller bug never wedges a progressing placement).
    * `:max_attempts` - retry bound (default 3). One attempt is always made even
      when the fleet is larger; the bound caps how many rejections are chased.
    * `:enabled?` - the `EMBERVM_PLACEMENT_RETRY` gate. When false (the default via
      `enabled?/0`), only the FIRST candidate is attempted and its result is
      returned verbatim (today's single-attempt behaviour); a `{:reject, _}` from
      it maps to `{:error, :no_capacity}` exactly as a single-attempt miss does
      today, never a retry.

  Returns `{:ok, result}` on the first success, `{:error, reason}` on a terminal
  attempt error, or `{:error, :no_capacity}` when the candidate list is empty or
  every attempted candidate rejected within the budget.
  """
  @spec run([candidate()], (candidate() -> attempt_result()), keyword()) ::
          {:ok, term()} | {:error, term()}
  def run(candidates, attempt_fun, opts \\ []) when is_list(candidates) and is_function(attempt_fun, 1) do
    enabled? = Keyword.get(opts, :enabled?, enabled?())

    max_attempts =
      if enabled? do
        Keyword.get(opts, :max_attempts, @default_max_attempts)
      else
        # Gate OFF: exactly one attempt, today's behaviour.
        1
      end

    ctx = %{
      attempt_fun: attempt_fun,
      max: max_attempts,
      # on_reject.(brick, reason) is the caller's advisory downward-headroom
      # refresh hook (ADR 014 decision 3: "decrement its cached headroom view
      # immediately"): a create path passes a callback that lowers the rejected
      # brick's headroom in ITS view so a later pick in the same burst does not
      # re-choose it as if it still had room. Default no-op (the retry list itself
      # already drops the brick, which is the in-attempt "mark ineligible").
      on_reject: Keyword.get(opts, :on_reject, fn _brick, _reason -> :ok end)
    }

    attempt(candidates, ctx, 0)
  end

  # No candidate left, or the budget is spent: fast, explicit no-capacity. Never a
  # wedge, never a retry storm (the bound is the ceiling on chased rejections).
  defp attempt([], _ctx, _tried), do: {:error, :no_capacity}

  defp attempt(_candidates, %{max: max}, tried) when tried >= max do
    {:error, :no_capacity}
  end

  defp attempt([brick | rest], ctx, tried) do
    case ctx.attempt_fun.(brick) do
      {:ok, result} ->
        {:ok, result}

      {:error, reason} ->
        # Terminal failure (transport, bad request): do NOT retry. A non-pressure
        # error is not a capacity signal, so chasing the next brick would just
        # multiply an unrelated failure.
        {:error, reason}

      {:reject, reason} ->
        # Retryable node-pressure rejection. Marking the brick ineligible "for this
        # attempt" IS dropping it from the retry frontier (it moves out of the head
        # into `rest`, and `rest` never contains it again: candidates are unique by
        # instance_id). The advisory downward-headroom refresh is the caller's view
        # concern, delegated to on_reject.
        # Whether a further attempt will actually run: budget left AND a candidate
        # left. Under the gate-off single-attempt path (max == 1) this is false, so
        # the log must NOT claim "retrying" when the very next call stops on the
        # tried >= max guard (the historic mislead).
        next = tried + 1
        will_retry? = next < ctx.max and rest != []

        Logger.debug(fn ->
          outcome = if will_retry?, do: "retrying next candidate", else: "no capacity left within budget"

          "embervm placement: brick #{inspect(Map.get(brick, :instance_id))} rejected " <>
            "(#{inspect(reason)}); #{outcome} (attempt #{next}/#{ctx.max})"
        end)

        safe_on_reject(ctx.on_reject, brick, reason)
        attempt(rest, ctx, next)
    end
  end

  # The caller's advisory-refresh hook must never crash the retry loop: a bad
  # callback is a caller bug, not a reason to fail an otherwise-progressing
  # placement. Swallow and continue to the next candidate.
  defp safe_on_reject(on_reject, brick, reason) do
    on_reject.(brick, reason)
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  @doc """
  Whether the reject/retry policy is enabled, from `EMBERVM_PLACEMENT_RETRY`
  (default OFF). UNSET or "0"/"false"/"" keeps today's single-attempt behaviour;
  "1"/"true" turns on multi-candidate retry. Mirrors the env-gate convention of
  the other ADR-014 changes: the chart key `dispatcher.placementRetry`
  (chart/values.yaml) renders this variable in deployment.yaml, so it flips via
  a values-only deploy, no code change.
  """
  @spec enabled?() :: boolean()
  def enabled? do
    case System.get_env("EMBERVM_PLACEMENT_RETRY") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end
end
