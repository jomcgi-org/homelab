defmodule Embervm.AuthTest do
  @moduledoc """
  Exercises the TokenReview caching + singleflight reviewer with an injected
  fake reviewer (no live API server), covering the properties the fc-invoke
  incident hardened: cache hits avoid the reviewer, concurrent misses for one
  token collapse to a single review, failures are never cached, the allow-list
  is enforced, and a slow miss for one token does not block a cached hit for
  another.
  """
  use ExUnit.Case, async: true

  alias Embervm.Auth
  alias Embervm.Auth.Identity

  # A reviewer whose call count is observable, so "how many times did the network
  # review actually run" is directly assertable. `map` is token ->
  # {:ok, %Identity{}} | {:error, reason}; `delay_ms` optionally stalls each call
  # to widen races.
  defp counting_reviewer(map, delay_ms \\ 0) do
    {:ok, counter} = Agent.start_link(fn -> 0 end)

    fun = fn token ->
      Agent.update(counter, &(&1 + 1))
      if delay_ms > 0, do: Process.sleep(delay_ms)
      Map.get(map, token, {:error, :unauthenticated})
    end

    {fun, counter}
  end

  defp start_auth(reviewer, opts \\ []) do
    {:ok, pid} =
      Auth.start_link(
        [name: nil, reviewer: reviewer, allowed: ["system:serviceaccount:embervm:embervm"]] ++
          opts
      )

    pid
  end

  defp identity(username, opts \\ []) do
    struct!(Identity, Keyword.put(opts, :username, username))
  end

  test "authenticates an allow-listed principal and caches it (one review for two calls)" do
    {reviewer, counter} =
      counting_reviewer(%{
        "tok" => {:ok, identity("system:serviceaccount:embervm:embervm")}
      })

    auth = start_auth(reviewer)

    assert {:ok, "system:serviceaccount:embervm:embervm"} = Auth.authenticate(auth, "tok")
    assert {:ok, "system:serviceaccount:embervm:embervm"} = Auth.authenticate(auth, "tok")
    assert Agent.get(counter, & &1) == 1
  end

  test "authenticated-but-not-allow-listed is forbidden and not cached" do
    {reviewer, counter} =
      counting_reviewer(%{
        "tok" => {:ok, identity("system:serviceaccount:other:sa")}
      })

    auth = start_auth(reviewer)

    assert {:error, {:forbidden, "system:serviceaccount:other:sa"}} = Auth.authenticate(auth, "tok")
    assert {:error, {:forbidden, "system:serviceaccount:other:sa"}} = Auth.authenticate(auth, "tok")
    # Denials are not cached: the reviewer runs each time.
    assert Agent.get(counter, & &1) == 2
  end

  test "transient review failures are never cached" do
    {reviewer, counter} = counting_reviewer(%{"tok" => {:error, :unauthenticated}})
    auth = start_auth(reviewer)

    assert {:error, :unauthenticated} = Auth.authenticate(auth, "tok")
    assert {:error, :unauthenticated} = Auth.authenticate(auth, "tok")
    assert Agent.get(counter, & &1) == 2
  end

  test "concurrent misses for the same token collapse to a single review (singleflight)" do
    {reviewer, counter} =
      counting_reviewer(
        %{"tok" => {:ok, identity("system:serviceaccount:embervm:embervm")}},
        50
      )

    auth = start_auth(reviewer)

    results =
      1..20
      |> Task.async_stream(fn _ -> Auth.authenticate(auth, "tok") end, max_concurrency: 20)
      |> Enum.map(fn {:ok, r} -> r end)

    assert Enum.all?(results, &(&1 == {:ok, "system:serviceaccount:embervm:embervm"}))
    # All 20 concurrent misses shared ONE review.
    assert Agent.get(counter, & &1) == 1
  end

  test "a slow miss for one token does not block a cached hit for another" do
    {reviewer, _counter} =
      counting_reviewer(
        %{
          "slow" => {:ok, identity("system:serviceaccount:embervm:embervm")},
          "fast" => {:ok, identity("system:serviceaccount:embervm:embervm")}
        },
        200
      )

    auth = start_auth(reviewer)

    # Prime "fast" so it is a cache hit.
    assert {:ok, _} = Auth.authenticate(auth, "fast")

    # Start a slow miss for "slow" in the background, then a hit for "fast" must
    # return well before the slow review completes.
    slow = Task.async(fn -> Auth.authenticate(auth, "slow") end)
    {micros, {:ok, _}} = :timer.tc(fn -> Auth.authenticate(auth, "fast") end)

    assert micros < 100_000, "cached hit blocked behind the slow miss (#{micros}us)"
    assert {:ok, _} = Task.await(slow)
  end

  test "cache entries expire after the TTL" do
    {reviewer, counter} =
      counting_reviewer(%{
        "tok" => {:ok, identity("system:serviceaccount:embervm:embervm")}
      })

    {:ok, clock} = Agent.start_link(fn -> 0 end)
    clock_fun = fn -> Agent.get(clock, & &1) end
    auth = start_auth(reviewer, ttl_ms: 1_000, clock: clock_fun)

    assert {:ok, _} = Auth.authenticate(auth, "tok")
    assert Agent.get(counter, & &1) == 1

    # Still inside the TTL window: served from cache.
    Agent.update(clock, fn _ -> 999 end)
    assert {:ok, _} = Auth.authenticate(auth, "tok")
    assert Agent.get(counter, & &1) == 1

    # Past the TTL: re-reviewed.
    Agent.update(clock, fn _ -> 1_001 end)
    assert {:ok, _} = Auth.authenticate(auth, "tok")
    assert Agent.get(counter, & &1) == 2
  end

  test "identity and username APIs share one cached review" do
    reviewed =
      identity("system:serviceaccount:embervm:embervm",
        pod_uid: "pod-uid-1",
        pod_name: "embervm-brick-1",
        node_name: "node-4"
      )

    {reviewer, counter} = counting_reviewer(%{"tok" => {:ok, reviewed}})
    auth = start_auth(reviewer)

    assert {:ok, ^reviewed} = Auth.authenticate_identity(auth, "tok")
    assert {:ok, "system:serviceaccount:embervm:embervm"} = Auth.authenticate(auth, "tok")
    assert Agent.get(counter, & &1) == 1
  end
end
