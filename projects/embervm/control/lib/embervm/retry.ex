defmodule Embervm.Retry do
  @moduledoc """
  Failure classification and backoff, as pure functions. `Embervm.TaskStore`
  is the only caller: on `fail/2` it asks `classify/3` whether the failure is
  retryable given the task's current attempt count and workload retry config,
  and if so how long to wait via `backoff_ms/3` before the dispatcher (a later
  task) is allowed to call `retry/2` again.

  Pure and RNG-injected on purpose: the distribution test seeds `rand_fun` to
  assert the full-jitter bound deterministically without flaking on real
  randomness, and `classify/3`/`retryable?/2` never touch the op-log or ETS so
  they're trivially unit-testable in isolation from `TaskStore`.
  """

  @default_max_attempts 3
  @default_backoff_ms 1_000
  @default_backoff_cap_ms 60_000
  @default_retry_on [:transport, :timeout, :guest5xx]

  @type reason :: atom()
  @type retry_config :: %{
          max_attempts: pos_integer(),
          backoff_ms: pos_integer(),
          backoff_cap_ms: pos_integer(),
          retry_on: [reason()]
        }

  @spec default_config() :: retry_config()
  def default_config do
    %{
      max_attempts: @default_max_attempts,
      backoff_ms: @default_backoff_ms,
      backoff_cap_ms: @default_backoff_cap_ms,
      retry_on: @default_retry_on
    }
  end

  # :guest4xx (and any client-error class) means the guest answered with a
  # well-formed HTTP error: the request itself is wrong, so retrying it would
  # just reproduce the same 4xx. That's always permanent regardless of what
  # retry_on says, which is why this check comes before the retry_on lookup.
  @spec retryable?(reason(), retry_config()) :: boolean()
  def retryable?(:guest4xx, _retry_config), do: false
  def retryable?(reason, retry_config), do: reason in retry_config.retry_on

  # attempt is the CURRENT attempt count (1-based: the first try is attempt
  # 1). A failure on the attempt that reaches max_attempts has exhausted its
  # budget and is permanent even if the reason class is otherwise retryable.
  @spec classify(reason(), pos_integer(), retry_config()) :: :fail_retryable | :fail_permanent
  def classify(reason, attempt, retry_config) do
    if retryable?(reason, retry_config) and attempt < retry_config.max_attempts do
      :fail_retryable
    else
      :fail_permanent
    end
  end

  # Full jitter (AWS's "Exponential Backoff And Jitter" algorithm): the bound
  # doubles with each attempt up to backoff_cap_ms, and the actual delay is
  # sampled uniformly from [0, bound] rather than always sleeping the full
  # bound. This spreads a thundering herd of simultaneously-failed tasks
  # across the whole window instead of retrying them all in lockstep.
  @spec backoff_ms(pos_integer(), retry_config(), (non_neg_integer() -> non_neg_integer())) ::
          non_neg_integer()
  def backoff_ms(attempt, retry_config, rand_fun \\ &default_rand/1) do
    bound =
      min(
        retry_config.backoff_cap_ms,
        trunc(retry_config.backoff_ms * :math.pow(2, attempt - 1))
      )

    rand_fun.(bound)
  end

  # Uniform over 0..bound inclusive. :rand.uniform/1 requires a positive
  # integer and returns 1..N, so bound 0 is special-cased (nothing to sample)
  # and the general case shifts by one to include both endpoints.
  defp default_rand(0), do: 0
  defp default_rand(bound), do: :rand.uniform(bound + 1) - 1
end
