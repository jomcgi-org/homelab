defmodule Embervm.Placement.RetryTest do
  @moduledoc """
  Exercises Embervm.Placement.Retry (ADR embervm/014 decision 3): the ONE
  reject/retry policy every NEW-placement boot path shares. Covers the gate
  (`EMBERVM_PLACEMENT_RETRY`, default off => single attempt), first-success,
  reject-then-retry-next-candidate, exhaustion after exactly max_attempts,
  terminal-error-never-retries, and the empty-list fast fail.

  Tests pass `:enabled?` explicitly rather than touching the process env, so they
  are deterministic and `async: true`.
  """
  # async: false — the enabled?/0 test mutates the process-global
  # EMBERVM_PLACEMENT_RETRY env, which would leak into other async modules'
  # gate-sensitive assertions if this ran concurrently. The core cases pass
  # :enabled? explicitly and do not touch env, but the module as a whole must be
  # serial because of that one sub-test.
  use ExUnit.Case, async: false

  alias Embervm.Placement.Retry

  defp brick(id, headroom \\ 8_000) do
    %{instance_id: id, mem_headroom_mib: headroom}
  end

  # An attempt_fun that records (into an Agent) each brick it is called on, and
  # returns a scripted result per instance_id. Missing id => a reject (so an
  # unexpected extra candidate does not silently succeed).
  defp scripted(script, recorder) do
    fn brick ->
      Agent.update(recorder, fn ids -> ids ++ [brick.instance_id] end)
      Map.get(script, brick.instance_id, {:reject, :pressure})
    end
  end

  setup do
    {:ok, rec} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(rec) end)
    %{rec: rec}
  end

  describe "gate OFF (single attempt)" do
    test "attempts only the first candidate and returns its success", %{rec: rec} do
      script = %{"a" => {:ok, :booted_a}, "b" => {:ok, :booted_b}}

      assert {:ok, :booted_a} =
               Retry.run([brick("a"), brick("b")], scripted(script, rec), enabled?: false)

      assert Agent.get(rec, & &1) == ["a"]
    end

    test "a reject from the sole attempt maps to :no_capacity, never a retry", %{rec: rec} do
      script = %{"a" => {:reject, :pressure}, "b" => {:ok, :booted_b}}

      assert {:error, :no_capacity} =
               Retry.run([brick("a"), brick("b")], scripted(script, rec), enabled?: false)

      # Gate off never touched the second brick even though it would have succeeded.
      assert Agent.get(rec, & &1) == ["a"]
    end
  end

  describe "gate ON (reject/retry)" do
    test "first brick rejects -> second brick receives the RPC and succeeds", %{rec: rec} do
      script = %{"a" => {:reject, :pressure}, "b" => {:ok, :booted_b}}

      assert {:ok, :booted_b} =
               Retry.run([brick("a"), brick("b")], scripted(script, rec), enabled?: true)

      assert Agent.get(rec, & &1) == ["a", "b"]
    end

    test "all reject -> :no_capacity after exactly max_attempts", %{rec: rec} do
      # Five candidates all reject, but max_attempts is 3: exactly three are tried.
      cands = for id <- ~w(a b c d e), do: brick(id)
      script = %{}

      assert {:error, :no_capacity} =
               Retry.run(cands, scripted(script, rec), enabled?: true, max_attempts: 3)

      assert Agent.get(rec, & &1) == ["a", "b", "c"]
    end

    test "fewer candidates than max_attempts fails cleanly when all reject", %{rec: rec} do
      script = %{}

      assert {:error, :no_capacity} =
               Retry.run([brick("a"), brick("b")], scripted(script, rec), enabled?: true, max_attempts: 3)

      assert Agent.get(rec, & &1) == ["a", "b"]
    end

    test "a terminal {:error, _} stops immediately without retrying", %{rec: rec} do
      # 'a' errors terminally; 'b' would succeed but must NOT be tried (an error is
      # not a capacity signal).
      script = %{"a" => {:error, :transport_boom}, "b" => {:ok, :booted_b}}

      assert {:error, :transport_boom} =
               Retry.run([brick("a"), brick("b")], scripted(script, rec), enabled?: true)

      assert Agent.get(rec, & &1) == ["a"]
    end

    test "an empty candidate list is an immediate :no_capacity", %{rec: rec} do
      assert {:error, :no_capacity} = Retry.run([], scripted(%{}, rec), enabled?: true)
      assert Agent.get(rec, & &1) == []
    end

    test "default max_attempts is 3" do
      {:ok, rec} = Agent.start_link(fn -> [] end)
      cands = for id <- ~w(a b c d), do: brick(id)

      assert {:error, :no_capacity} =
               Retry.run(cands, scripted(%{}, rec), enabled?: true)

      assert length(Agent.get(rec, & &1)) == 3
      Agent.stop(rec)
    end
  end

  describe "enabled?/0 reads the env gate" do
    test "unset / falsey is OFF, truthy is ON" do
      original = System.get_env("EMBERVM_PLACEMENT_RETRY")

      try do
        System.delete_env("EMBERVM_PLACEMENT_RETRY")
        refute Retry.enabled?()

        System.put_env("EMBERVM_PLACEMENT_RETRY", "0")
        refute Retry.enabled?()

        System.put_env("EMBERVM_PLACEMENT_RETRY", "1")
        assert Retry.enabled?()

        System.put_env("EMBERVM_PLACEMENT_RETRY", "true")
        assert Retry.enabled?()
      after
        case original do
          nil -> System.delete_env("EMBERVM_PLACEMENT_RETRY")
          v -> System.put_env("EMBERVM_PLACEMENT_RETRY", v)
        end
      end
    end
  end
end
