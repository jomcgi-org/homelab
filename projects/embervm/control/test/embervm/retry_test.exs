defmodule Embervm.RetryTest do
  @moduledoc """
  Unit tests for failure classification and full-jitter backoff. Pure
  functions, no op-log or ETS involved, so these run fast and deterministic
  (the RNG is always seeded explicitly here).
  """
  # async: false: this module mutates the process-global EMBERVM_PLACEMENT_RETRY
  # env (enabled?/0 test), which would race concurrent async modules asserting the
  # gate-off single-attempt path.
  use ExUnit.Case, async: false

  alias Embervm.Retry

  @cfg Retry.default_config()

  describe "classify/3" do
    test "guest4xx is always permanent, even on attempt 1" do
      assert Retry.classify(:guest4xx, 1, @cfg) == :fail_permanent
    end

    test "a retryable reason under max_attempts is retryable" do
      assert @cfg.max_attempts == 3
      assert Retry.classify(:transport, 1, @cfg) == :fail_retryable
      assert Retry.classify(:transport, 2, @cfg) == :fail_retryable
    end

    test "a retryable reason at max_attempts is permanent (budget exhausted)" do
      assert Retry.classify(:transport, @cfg.max_attempts, @cfg) == :fail_permanent
    end

    test "a reason not in retry_on is permanent regardless of attempt" do
      cfg = %{@cfg | retry_on: [:timeout]}
      assert Retry.classify(:transport, 1, cfg) == :fail_permanent
    end
  end

  describe "backoff_ms/3" do
    test "always within [0, min(cap, base * 2^(attempt-1))] across attempts 1..10, and varies" do
      # Deterministic seeded rand_fun: returns bound itself on odd calls, 0 on
      # even calls, so the sequence is far from constant while still staying
      # in-bounds for every call (exercises both ends of the interval).
      {:ok, counter} = Agent.start_link(fn -> 0 end)

      rand_fun = fn bound ->
        n = Agent.get_and_update(counter, fn n -> {n, n + 1} end)
        if rem(n, 2) == 0, do: bound, else: 0
      end

      results =
        for attempt <- 1..10 do
          bound = min(@cfg.backoff_cap_ms, trunc(@cfg.backoff_ms * :math.pow(2, attempt - 1)))
          value = Retry.backoff_ms(attempt, @cfg, rand_fun)
          assert value >= 0
          assert value <= bound
          {attempt, bound, value}
        end

      Agent.stop(counter)

      # Not constant across attempts: the returned value tracks the growing
      # bound (odd calls echo the bound back).
      values = Enum.map(results, fn {_a, _b, v} -> v end)
      assert Enum.uniq(values) |> length() > 1

      # The bound itself must respect the cap once 2^(attempt-1) * base
      # exceeds backoff_cap_ms.
      {_attempt, bound_at_10, _value} = List.last(results)
      assert bound_at_10 == @cfg.backoff_cap_ms
    end

    test "default_rand produces a value in range with real randomness (smoke test)" do
      for attempt <- 1..5 do
        value = Retry.backoff_ms(attempt, @cfg)
        bound = min(@cfg.backoff_cap_ms, trunc(@cfg.backoff_ms * :math.pow(2, attempt - 1)))
        assert value >= 0
        assert value <= bound
      end
    end
  end
end
