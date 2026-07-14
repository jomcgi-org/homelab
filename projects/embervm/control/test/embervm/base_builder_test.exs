defmodule Embervm.BaseBuilderTest do
  @moduledoc """
  Task 10 acceptance: `Embervm.BaseBuilder` drives the node daemon's `BuildBase`
  RPC on Workload admission and reconciles the result into CR status.

  A FAKE DAEMON is injected the same way `Embervm.NodeRegistry`'s tests inject a
  `watch_fun`: `connect_fun`/`disconnect_fun` return a sentinel channel and
  `build_fun` stands in for `NodeService.Stub.build_base/2`, returning `{:ok,
  %BuildBaseResponse{}}` on success or `{:error, %GRPC.RPCError{}}` on failure
  (FAILED_PRECONDITION for an unknown image is every image in R0). The build
  runs in a real spawned worker, so observations are made through
  `assert_eventually`/`assert_receive` rather than assuming synchronous timing.

  Each test uses an unnamed (`name: nil`) builder so tests run async with no
  shared state and never touch the application's own supervised BaseBuilder.
  """
  use ExUnit.Case, async: true

  alias Embervm.BaseBuilder
  alias Embervm.Node.V1.BuildBaseResponse

  @node %{id: "node-4", address: "node-4:9090"}

  # -- helpers ----------------------------------------------------------------

  defp start_recorder do
    {:ok, agent} = Agent.start_link(fn -> [] end)
    agent
  end

  defp recorded(agent), do: Agent.get(agent, & &1)

  defp recording_status_writer(agent) do
    fn namespace, name, status_map ->
      Agent.update(agent, fn calls -> calls ++ [{namespace, name, status_map}] end)
      :ok
    end
  end

  # Newest status write for a workload name.
  defp latest(agent, name) do
    recorded(agent)
    |> Enum.filter(fn {_ns, n, _} -> n == name end)
    |> List.last()
    |> case do
      nil -> nil
      {_ns, _n, status_map} -> status_map
    end
  end

  defp condition(status_map, type) do
    (status_map["conditions"] || []) |> Enum.find(&(&1["type"] == type))
  end

  defp desc(overrides \\ %{}) do
    Map.merge(
      %{
        name: "w",
        namespace: "embervm",
        generation: 1,
        image_ref: "imgA",
        guest_port: 8080,
        ready_path: "/shim/ready",
        vcpus: 1,
        mem_mib: 256,
        init_env: %{}
      },
      overrides
    )
  end

  defp resp(snapshot_ref, digest \\ "sha256:deadbeef") do
    %BuildBaseResponse{snapshot_ref: snapshot_ref, image_digest: digest, base_size_bytes: 1, arch: "amd64"}
  end

  defp start_builder(opts) do
    {:ok, pid} =
      BaseBuilder.start_link(
        [
          name: nil,
          nodes: Keyword.get(opts, :nodes, [@node]),
          connect_fun: fn _addr -> {:ok, :fake_channel} end,
          disconnect_fun: fn :fake_channel -> :ok end
        ] ++ opts
      )

    pid
  end

  defp assert_eventually(fun, timeout_ms \\ 2_000, interval_ms \\ 10) do
    deadline = System.monotonic_time(:millisecond) + timeout_ms
    do_eventually(fun, deadline, interval_ms)
  end

  defp do_eventually(fun, deadline, interval_ms) do
    cond do
      fun.() ->
        :ok

      System.monotonic_time(:millisecond) >= deadline ->
        flunk("condition was not met within the timeout")

      true ->
        Process.sleep(interval_ms)
        do_eventually(fun, deadline, interval_ms)
    end
  end

  # -- admission -> BuildBase -> status write ---------------------------------

  test "a valid admission drives BuildBase and writes a built base into status" do
    agent = start_recorder()

    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1", "sha256:abc")} end

    builder =
      start_builder(status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc())

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> s["snapshotRef"] == "snap1"
      end
    end)

    status_map = latest(agent, "w")
    assert status_map["snapshotRef"] == "snap1"
    assert status_map["snapshotDigest"] == "sha256:abc"
    assert %{"status" => "True", "reason" => "BaseReady"} = condition(status_map, "Ready")
    assert %{"status" => "True", "reason" => "BaseBuilt"} = condition(status_map, "BaseBuilt")
  end

  test "an already-built base is not rebuilt on a redundant reconcile (idempotent)" do
    agent = start_recorder()
    {:ok, count} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      Agent.update(count, &(&1 + 1))
      {:ok, resp("snap1")}
    end

    builder = start_builder(status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)

    # Same spec again: no new build (the signature is unchanged and a base exists).
    :ok = BaseBuilder.reconcile(builder, desc())
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))
    Process.sleep(50)

    assert Agent.get(count, & &1) == 1
  end

  # -- failure -> BaseBuilt=False + daemon error + backoff --------------------

  test "a failed build reports the daemon error and retries with backoff, never becoming Ready" do
    agent = start_recorder()
    {:ok, attempts} = Agent.start_link(fn -> 0 end)

    # FAILED_PRECONDITION is what the real daemon returns for an unknown image
    # (every image in R0). The first two attempts fail; the third succeeds, to
    # prove the backoff retry actually re-drives the build.
    build_fun = fn :fake_channel, _req ->
      n = Agent.get_and_update(attempts, fn n -> {n + 1, n + 1} end)

      if n < 3 do
        {:error, %GRPC.RPCError{status: 9, message: "no image source for imgA"}}
      else
        {:ok, resp("snap1")}
      end
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        base_backoff_ms: 5,
        max_backoff_ms: 20
      )

    :ok = BaseBuilder.reconcile(builder, desc())

    # The first failure surfaces the daemon message and keeps the workload
    # un-Ready (no base exists).
    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> match?(%{"reason" => "BuildFailed"}, condition(s, "BaseBuilt"))
      end
    end)

    failed = latest(agent, "w")
    assert %{"status" => "False", "message" => "no image source for imgA"} = condition(failed, "BaseBuilt")
    assert %{"status" => "False", "reason" => "BaseNotBuilt"} = condition(failed, "Ready")

    # Backoff retried until the (scripted) success, proving the retry loop runs.
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)
    assert Agent.get(attempts, & &1) >= 3
  end

  # -- serialization ----------------------------------------------------------

  test "builds are serialized per node: a second admission waits for the first" do
    agent = start_recorder()
    test_pid = self()

    # Each build announces itself and its worker pid, then blocks until the test
    # releases it. If builds were concurrent, both would announce immediately.
    build_fun = fn :fake_channel, req ->
      send(test_pid, {:building, req.trace.workload, self()})

      receive do
        {:go, result} -> result
      end
    end

    builder = start_builder(status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc(%{name: "a"}))
    :ok = BaseBuilder.reconcile(builder, desc(%{name: "b"}))

    # "a" starts; "b" must NOT start while "a" is in flight.
    assert_receive {:building, "a", worker_a}, 1_000
    refute_receive {:building, "b", _}, 200

    # Releasing "a" lets "b" start: proof of serial-per-node execution.
    send(worker_a, {:go, {:ok, resp("snap-a")}})
    assert_receive {:building, "b", worker_b}, 1_000
    send(worker_b, {:go, {:ok, resp("snap-b")}})

    assert_eventually(fn -> match?(%{"snapshotRef" => "snap-b"}, latest(agent, "b")) end)
  end

  # -- digest-change turnover (the acceptance property) -----------------------

  test "a digest change rebuilds and turns over the base with no zero-base window" do
    agent = start_recorder()

    build_fun = fn :fake_channel, req ->
      case req.image_ref do
        "imgA" -> {:ok, resp("snap1", "sha256:aaa")}
        "imgB" -> {:ok, resp("snap2", "sha256:bbb")}
      end
    end

    builder = start_builder(status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc(%{image_ref: "imgA"}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)

    first_success_count = length(recorded(agent))

    # New image ref (a "digest change") triggers a rebuild to snap2.
    :ok = BaseBuilder.reconcile(builder, desc(%{image_ref: "imgB", generation: 2}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap2"}, latest(agent, "w")) end)

    # PROPERTY: from the first built base onward, every status write keeps the
    # workload Ready (a restorable base always exists) and never clears
    # snapshotRef to "". The old base is never dropped before the new one lands.
    writes_after_first = Enum.drop(recorded(agent), first_success_count - 1)

    for {_ns, "w", status_map} <- writes_after_first do
      assert %{"status" => "True"} = condition(status_map, "Ready")

      if Map.has_key?(status_map, "snapshotRef") do
        assert status_map["snapshotRef"] in ["snap1", "snap2"]
      end
    end

    # The old base is recorded for the pool to turn over (Task 11 seam), and the
    # current snapshot has advanced to the new base.
    st = BaseBuilder.status(builder)
    assert st.workloads["w"].snapshot_ref == "snap2"
    assert st.workloads["w"].superseded_refs == ["snap1"]
  end

  # -- no node configured -----------------------------------------------------

  test "with no node configured, no build is attempted and status reports NoNodeAvailable" do
    agent = start_recorder()
    {:ok, count} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      Agent.update(count, &(&1 + 1))
      {:ok, resp("snap1")}
    end

    builder = start_builder(nodes: [], status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc())

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> match?(%{"reason" => "NoNodeAvailable"}, condition(s, "BaseBuilt"))
      end
    end)

    assert %{"status" => "Unknown"} = condition(latest(agent, "w"), "BaseBuilt")
    assert Agent.get(count, & &1) == 0
  end

  test "a stale retry message while a build is in flight does not enqueue a second build" do
    agent = start_recorder()
    test_pid = self()
    {:ok, count} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, req ->
      Agent.update(count, &(&1 + 1))
      send(test_pid, {:building, req.trace.workload, self()})

      receive do
        {:go, result} -> result
      end
    end

    builder = start_builder(status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_receive {:building, "w", worker}, 1_000

    # Simulate a stale backoff timer firing (its {:retry, name} message racing a
    # build already in flight for the current signature). It must be a no-op, not
    # a second enqueue.
    send(builder, {:retry, "w"})
    Process.sleep(30)

    send(worker, {:go, {:ok, resp("snap1")}})
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)

    # Exactly one build ran despite the stale retry.
    Process.sleep(30)
    assert Agent.get(count, & &1) == 1
  end

  # -- forget -----------------------------------------------------------------

  test "forgetting a workload mid-queue drops it without building" do
    agent = start_recorder()
    test_pid = self()

    build_fun = fn :fake_channel, req ->
      send(test_pid, {:building, req.trace.workload, self()})

      receive do
        {:go, result} -> result
      end
    end

    builder = start_builder(status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc(%{name: "a"}))
    :ok = BaseBuilder.reconcile(builder, desc(%{name: "b"}))

    assert_receive {:building, "a", worker_a}, 1_000

    # Forget "b" while it is queued behind "a": it must never build.
    :ok = BaseBuilder.forget(builder, "b")
    send(worker_a, {:go, {:ok, resp("snap-a")}})

    assert_eventually(fn -> match?(%{"snapshotRef" => "snap-a"}, latest(agent, "a")) end)
    refute_receive {:building, "b", _}, 200

    st = BaseBuilder.status(builder)
    refute Map.has_key?(st.workloads, "b")
  end
end
