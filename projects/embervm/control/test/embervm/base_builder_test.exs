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
  # async: false: these poll for real base-build state transitions (a spawned
  # build worker reaching a failed/ready condition), which starve under async
  # scheduler contention as the suite grows and flake the wait. Serial keeps the
  # timing deterministic regardless of suite size.
  use ExUnit.Case, async: false
  import ExUnit.CaptureLog

  alias Embervm.BaseBuilder
  alias Embervm.NodeCapacity
  alias Embervm.Node.V1.BuildBaseResponse

  @node %{id: "node-4", address: "node-4:9090"}

  # A no-op op-log: swallows every append so a build's export audit never touches
  # the real SQLite in tests that do not assert on the op-log.
  defmodule DiscardOpLog do
    def append(_op_log, _op), do: :ok
  end

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
    nodes = Keyword.get(opts, :nodes, [@node])

    opts =
      if Keyword.has_key?(opts, :capacity_table) do
        opts
      else
        table = new_cap_table()
        Enum.each(nodes, &put_instance_fact(table, &1.id))
        Keyword.put(opts, :capacity_table, table)
      end

    {:ok, pid} =
      BaseBuilder.start_link(
        [
          name: nil,
          nodes: nodes,
          connect_fun: Keyword.get(opts, :connect_fun, fn _addr -> {:ok, :fake_channel} end),
          disconnect_fun: fn :fake_channel -> :ok end,
          # Base-durability PR-1 defaults for tests that do not exercise export:
          # a no-op export seam (so a build's immediate export never dials the real
          # stub against the fake channel) and a disabled reconcile timer (so no
          # background sweep perturbs timing). Export-specific tests override both.
          export_fun: Keyword.get(opts, :export_fun, fn :fake_channel, _req -> {:ok, %{}} end),
          export_reconcile_interval_ms: Keyword.get(opts, :export_reconcile_interval_ms, 0),
          # Base-durability PR-3: disable the retention-sweep timer by default so no
          # background sweep perturbs the other tests' timing; the sweep-specific
          # tests drive :retention_sweep_now synchronously and opt the gate on/off.
          retention_sweep_interval_ms: Keyword.get(opts, :retention_sweep_interval_ms, 0),
          # Default the op-log to a discarding fake so a build's export audit never
          # touches the real SQLite in tests that do not assert on it. The
          # op-log-specific test overrides both to observe the append.
          op_log: Keyword.get(opts, :op_log, :discard),
          op_log_mod: Keyword.get(opts, :op_log_mod, DiscardOpLog)
        ] ++
          Keyword.drop(opts, [
            :export_fun,
            :connect_fun,
            :export_reconcile_interval_ms,
            :retention_sweep_interval_ms,
            :op_log,
            :op_log_mod
          ])
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

    # The retry is deliberately only 5ms later. It can publish BaseBuilding or
    # BaseBuilt before this test process gets scheduled again, so latest/2 does
    # not identify the failure that satisfied the wait above. Read the recorded
    # failure event itself, preserving the assertion about the daemon error
    # rather than racing the retry state.
    failed =
      recorded(agent)
      |> Enum.reverse()
      |> Enum.find_value(fn {_namespace, name, status_map} ->
        if name == "w" and
             match?(%{"reason" => "BuildFailed"}, condition(status_map, "BaseBuilt")),
           do: status_map
      end)

    refute is_nil(failed), "the failed build status was not recorded"
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

  # -- zip lane (R1, ADR embervm/002) -----------------------------------------

  alias Embervm.Node.V1.ZipSource

  # A zip-lane descriptor: image_ref nil, a zip block, EMBER_HANDLER init_env
  # (exactly what the watcher's build_desc hands us for a zip CR).
  defp zip_desc(overrides \\ %{}) do
    zip = Map.merge(%{runtime: "python312", code_uri: "http://filer/z.zip", sha256: "sha-1", handler: "app.handle"}, Map.get(overrides, :zip, %{}))

    desc(
      Map.merge(
        %{image_ref: nil, zip: zip, init_env: %{"EMBER_HANDLER" => zip.handler}},
        Map.delete(overrides, :zip)
      )
    )
  end

  @runtime_images %{"python312" => "ghcr.io/runtimes/python:pinned"}

  test "a zip source maps to a ZipSource (resolved runtime ref, url, sha256) and builds" do
    agent = start_recorder()
    test_pid = self()

    build_fun = fn :fake_channel, req ->
      send(test_pid, {:req, req})
      {:ok, resp("zsnap1", "sha256:zzz")}
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        runtime_images: @runtime_images
      )

    :ok = BaseBuilder.reconcile(builder, zip_desc())

    assert_receive {:req, req}, 1_000
    # The image lane's top-level image_ref is empty; source is the ZipSource.
    assert req.image_ref in [nil, ""]
    assert {:zip, %ZipSource{} = zip} = req.source
    assert zip.runtime_image_ref == "ghcr.io/runtimes/python:pinned"
    assert zip.archive_url == "http://filer/z.zip"
    assert zip.archive_sha256 == "sha-1"
    # EMBER_HANDLER rides init_env through to the guest.
    assert req.init_env == %{"EMBER_HANDLER" => "app.handle"}

    assert_eventually(fn -> match?(%{"snapshotRef" => "zsnap1"}, latest(agent, "w")) end)
  end

  test "a serving-class zip base sets serving: true so noded writes the handler artifact (D-R3.11.2)" do
    test_pid = self()

    build_fun = fn :fake_channel, req ->
      send(test_pid, {:req, req})
      {:ok, resp("zsnap1", "sha256:zzz")}
    end

    builder = start_builder(build_fun: build_fun, runtime_images: @runtime_images)

    # A serving-class zip workload marks the BuildBase serving; a task-class one does not.
    :ok = BaseBuilder.reconcile(builder, zip_desc(%{name: "srv", class: "serving"}))
    assert_receive {:req, serving_req}, 1_000
    assert serving_req.serving == true

    :ok = BaseBuilder.reconcile(builder, zip_desc(%{name: "tsk", class: "task"}))
    assert_receive {:req, task_req}, 1_000
    assert task_req.serving == false
  end

  test "a serving-class image base sets serving: true so noded registers the rootfs inventory entry (ADR embervm/038)" do
    test_pid = self()

    build_fun = fn :fake_channel, req ->
      send(test_pid, {:req, req})
      {:ok, resp("snap1", "sha256:aaa")}
    end

    builder = start_builder(build_fun: build_fun)

    # A serving-class image workload marks the BuildBase serving (handler-less
    # rootfs entry); a task-class one does not. Regression: the image lane
    # omitted the flag entirely, so the inventory never populated and every
    # serving wake failed with "no serving image provisioned".
    :ok = BaseBuilder.reconcile(builder, desc(%{name: "srv-img", class: "serving"}))
    assert_receive {:req, serving_req}, 1_000
    assert serving_req.serving == true
    assert serving_req.image_ref == "imgA"

    :ok = BaseBuilder.reconcile(builder, desc(%{name: "tsk-img", class: "task"}))
    assert_receive {:req, task_req}, 1_000
    assert task_req.serving == false
  end

  test "a new zip sha256 (same name) rebuilds the base: sha256 is in the change-detect signature" do
    agent = start_recorder()

    build_fun = fn :fake_channel, req ->
      {:zip, zip} = req.source

      case zip.archive_sha256 do
        "sha-1" -> {:ok, resp("zsnap1", "sha256:aaa")}
        "sha-2" -> {:ok, resp("zsnap2", "sha256:bbb")}
      end
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        runtime_images: @runtime_images
      )

    :ok = BaseBuilder.reconcile(builder, zip_desc())
    assert_eventually(fn -> match?(%{"snapshotRef" => "zsnap1"}, latest(agent, "w")) end)

    # Same workload, new archive bytes (new sha256), everything else identical:
    # the signature differs only by sha256, which must be enough to rebuild.
    :ok = BaseBuilder.reconcile(builder, zip_desc(%{generation: 2, zip: %{sha256: "sha-2"}}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "zsnap2"}, latest(agent, "w")) end)

    st = BaseBuilder.status(builder)
    assert st.workloads["w"].snapshot_ref == "zsnap2"
    assert st.workloads["w"].superseded_refs == ["zsnap1"]
  end

  test "a zip source with an unresolvable runtime reports Ready=False and never builds" do
    agent = start_recorder()
    {:ok, count} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      Agent.update(count, &(&1 + 1))
      {:ok, resp("zsnap1")}
    end

    # runtime_images empty: python312 does not resolve.
    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        runtime_images: %{}
      )

    :ok = BaseBuilder.reconcile(builder, zip_desc())

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> match?(%{"status" => "False", "reason" => "BuildFailed"}, condition(s, "BaseBuilt"))
      end
    end)

    Process.sleep(50)
    assert Agent.get(count, & &1) == 0
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

  # A retry timer that fires for a workload with no node placement used to kill the
  # whole builder: `already_targeting?/4` did `state.nodes[node_id].building`, and
  # `remove_node_from_state/2` nils a workload's node_id when its node goes away, so
  # the deref raised `KeyError: key :building not found in: nil`. Each crash wiped
  # every snapshot_ref and built_signature, re-placing and rebuilding the whole fleet
  # on a ~30s cycle (issue #4105).
  #
  # `worker_signature/2` below the fix carries the same guard. It is defensive only:
  # `remove_node_from_state/2` nils the workload's node_id rather than leaving it
  # pointing at a departed node, so a stale non-nil node_id is not reachable through
  # the public API, and there is no separate test for it.
  test "a retry for a workload whose node went away keeps the builder alive" do
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end
    builder = start_builder(build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc())

    assert_eventually(fn ->
      BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1"
    end)

    :ok = BaseBuilder.remove_node(builder, @node.id)
    send(builder, {:retry, "w"})

    # status/1 is a call, so it cannot be served until the {:retry, "w"} ahead of it
    # in the mailbox has been handled. That orders the assertion after the crash
    # would have happened, without a sleep that would make this test racy.
    assert BaseBuilder.status(builder).workloads["w"].node_id == nil
    assert Process.alive?(builder)
  end

  # The sibling of the test above, for the branch that actually crashed in
  # embervm-dev (#5082): the #4105 guard only covers already_targeting?/4, so a
  # retry whose FIRST build failed (built_signature nil) still fell through to
  # enqueue(w.node_id, name) with node_id nil after remove_node_from_state/2
  # unpinned it, and update_in(state.nodes[nil].queue, ...) raised BadMapError.
  # The old comment claimed a stale pin is unreachable through the public API;
  # the nil pin is exactly what remove_node_from_state/2 itself produces.
  #
  # The fix re-places through placement/3 like reconcile_desc/2: no eligible
  # node holds the workload at {:pending, :no_node} (the add_node re-drive test
  # below proves recovery), and an eligible node gets the rebuild enqueued.
  test "a retry firing after its node was removed re-places instead of crashing" do
    agent = start_recorder()
    {:ok, attempts} = Agent.start_link(fn -> 0 end)

    # First build FAILS so the retry arms with nothing built (built_signature:
    # nil), the exact sandbox-rust shape from embervm-dev. A long backoff keeps
    # the real timer quiet; the test fires {:retry, "w"} by hand.
    build_fun = fn :fake_channel, _req ->
      n = Agent.get_and_update(attempts, fn n -> {n + 1, n + 1} end)

      if n < 2 do
        {:error, %GRPC.RPCError{status: 9, message: "no image source for imgA"}}
      else
        {:ok, resp("snap1")}
      end
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        base_backoff_ms: 30_000,
        max_backoff_ms: 60_000
      )

    :ok = BaseBuilder.reconcile(builder, desc())

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> match?(%{"reason" => "BuildFailed"}, condition(s, "BaseBuilt"))
      end
    end)

    # The brick rolls mid-backoff: node leaves the placement set, workload is
    # unpinned, timer stays armed.
    :ok = BaseBuilder.remove_node(builder, @node.id)
    send(builder, {:retry, "w"})

    # Before the fix this crashed the GenServer (BadMapError in enqueue/3);
    # status/1 orders this assertion after the {:retry, "w"} message. Held at
    # {:pending, :no_node}, mirroring reconcile_desc's nil-placement branch.
    assert Process.alive?(builder)

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> match?(%{"reason" => "NoNodeAvailable"}, condition(s, "BaseBuilt"))
      end
    end)

    # Recovery: the brick comes back and add_node re-drives the held workload,
    # which now builds (attempt 2 succeeds).
    :ok = BaseBuilder.add_node(builder, @node.id, @node.address)
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)
    assert Agent.get(attempts, & &1) >= 2
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

  # -- base refcounting + eviction (R2) ---------------------------------------

  # Drives a base turnover (snap1 -> snap2) and then feeds refcounts against the
  # superseded snap1 via report_base_refs/3, asserting that EvictArtifact fires
  # exactly once, and only when BOTH primed and session refcounts are zero.
  defp turnover_to_snap2(builder, agent) do
    # gen1 -> snap1.
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 1, image_ref: "imgA"}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)

    # gen2 with a DIFFERENT image -> a new signature -> snap2, superseding snap1.
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2, image_ref: "imgB"}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap2"}, latest(agent, "w")) end)
  end

  test "a superseded base is not evicted while sessions reference it, and evicts on drain" do
    agent = start_recorder()
    test_pid = self()

    # Each build returns a snapshot ref derived from the image so gen1/gen2 differ.
    build_fun = fn :fake_channel, req ->
      ref = if req.image_ref == "imgA", do: "snap1", else: "snap2"
      {:ok, resp(ref)}
    end

    evict_fun = fn :fake_channel, _workload, ref ->
      send(test_pid, {:evicted, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        evict_fun: evict_fun
      )

    turnover_to_snap2(builder, agent)

    # snap1 is now a tracked superseded base with unknown refcounts.
    st = BaseBuilder.status(builder)
    assert Map.has_key?(st.workloads["w"].base_refs, "snap1")

    # Report primed:0 alone: sessions still unknown (nil) -> NOT evictable.
    :ok = BaseBuilder.report_base_refs(builder, "snap1", primed: 0)
    refute_receive {:evicted, "snap1"}, 100

    # Report sessions:2 -> still referenced, NOT evicted.
    :ok = BaseBuilder.report_base_refs(builder, "snap1", sessions: 2)
    refute_receive {:evicted, "snap1"}, 100

    # Sessions drain to 0 -> now both counts are known-and-zero -> evict fires.
    :ok = BaseBuilder.report_base_refs(builder, "snap1", sessions: 0)
    assert_receive {:evicted, "snap1"}, 1_000

    # The ref is marked evicted and dropped from the turnover list.
    st2 = BaseBuilder.status(builder)
    assert st2.workloads["w"].base_refs["snap1"].evicted == true
    refute "snap1" in st2.workloads["w"].superseded_refs
  end

  test "eviction fires only once even under repeated zero reports" do
    agent = start_recorder()
    test_pid = self()

    build_fun = fn :fake_channel, req ->
      ref = if req.image_ref == "imgA", do: "snap1", else: "snap2"
      {:ok, resp(ref)}
    end

    evict_fun = fn :fake_channel, _workload, ref ->
      send(test_pid, {:evicted, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        evict_fun: evict_fun
      )

    turnover_to_snap2(builder, agent)

    :ok = BaseBuilder.report_base_refs(builder, "snap1", primed: 0, sessions: 0)
    assert_receive {:evicted, "snap1"}, 1_000

    # A duplicate zero report after eviction does not re-evict (evicted guard).
    :ok = BaseBuilder.report_base_refs(builder, "snap1", primed: 0, sessions: 0)
    refute_receive {:evicted, "snap1"}, 100
  end

  test "reporting refs for an unknown ref is a harmless no-op" do
    agent = start_recorder()
    builder = start_builder(status_writer: recording_status_writer(agent))

    # No turnover has happened; snapX is not tracked. Must not crash or evict.
    assert :ok = BaseBuilder.report_base_refs(builder, "snapX", primed: 0, sessions: 0)
    assert Process.alive?(builder)
  end

  # R3: the refcount seam accepts a :serving count (a future ServingStore,
  # Task 9, would report it), but eviction is NOT yet gated on it: nothing
  # reports :serving today, so requiring it would silently wedge eviction for
  # every workload (task and session included), not just serving ones. This
  # test is the no-regression proof: primed:0, sessions:0 alone still evicts,
  # with or without a :serving count ever reported.
  test "a :serving refcount is accepted but not yet required for eviction (D-R3.3.1 precursor)" do
    agent = start_recorder()
    test_pid = self()

    build_fun = fn :fake_channel, req ->
      ref = if req.image_ref == "imgA", do: "snap1", else: "snap2"
      {:ok, resp(ref)}
    end

    evict_fun = fn :fake_channel, _workload, ref ->
      send(test_pid, {:evicted, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        evict_fun: evict_fun
      )

    turnover_to_snap2(builder, agent)

    # A :serving count arrives alongside primed, before sessions: still NOT
    # evictable (sessions unknown), proving :serving does not shortcut the
    # existing guard.
    :ok = BaseBuilder.report_base_refs(builder, "snap1", primed: 0, serving: 3)
    refute_receive {:evicted, "snap1"}, 100

    # primed:0, sessions:0 alone evicts, exactly as before R3, even though
    # :serving was reported nonzero and is never reported as 0: the guard is
    # unchanged, so a stray or nonzero :serving count cannot block eviction
    # either.
    :ok = BaseBuilder.report_base_refs(builder, "snap1", sessions: 0)
    assert_receive {:evicted, "snap1"}, 1_000
  end

  # -- reconciled base-retention sweep (base-durability PR-3) ------------------

  # Put a node-4 capacity fact carrying a current-base WorkloadCapacity fact (with
  # its exported flag) and a full local_bases inventory, so the retention sweep can
  # reconcile the reported local set against its desired set. instance_id is "node-4"
  # so find_capacity_fact(table, w.node_id) matches (the workload places on @node).
  defp put_local_bases_fact(table, workload, current_ref, exported?, local_bases) do
    NodeCapacity.put(table, {"node-4", "ds"}, %{
      node_id: "node-4",
      configured_id: "node-4",
      instance_id: "node-4",
      workloads: %{
        workload => %{
          snapshot_ref: current_ref,
          base_state: :BASE_BUILD_STATE_READY,
          exported: exported?
        }
      },
      local_bases: local_bases,
      updated_at: 0
    })
  end

  defp put_node_local_bases_fact(table, node_id, pod_uid, workload, current_ref, exported?, local_bases) do
    existing =
      case :ets.lookup(table, {node_id, pod_uid}) do
        [{_key, facts}] -> facts
        [] -> %{node_id: node_id, configured_id: node_id, instance_id: "#{node_id}/#{pod_uid}"}
      end

    workloads =
      Map.put(Map.get(existing, :workloads, %{}), workload, %{
        snapshot_ref: current_ref,
        base_state: :BASE_BUILD_STATE_READY,
        exported: exported?
      })

    NodeCapacity.put(
      table,
      {node_id, pod_uid},
      Map.merge(existing, %{workloads: workloads, local_bases: local_bases, updated_at: 0})
    )
  end

  defp put_unknown_local_bases_fact(table, node_id, pod_uid, local_bases) do
    NodeCapacity.put(table, {node_id, pod_uid}, %{
      node_id: node_id,
      configured_id: node_id,
      instance_id: "#{node_id}/#{pod_uid}",
      workloads: %{},
      local_bases: local_bases,
      updated_at: 0
    })
  end

  defp ready_base(ref, workload, bytes) do
    %{ref: ref, workload: workload, size_bytes: bytes, base_state: :BASE_BUILD_STATE_READY,
      created_at_unix_ms: System.system_time(:millisecond) - 3_601_000,
      snapshot_path: "/var/lib/embervm/scratch/embervm-noded/snapshots/bases/#{ref}"}
  end

  test "unknown base age is treated as new and is not a retention candidate" do
    table = new_cap_table()
    base = Map.merge(ready_base("deleted__unknown-age", "deleted", 512), %{created_at_unix_ms: 0})
    put_unknown_local_bases_fact(table, "node-4", "ds", [base])
    builder = start_builder(capacity_table: table, retention_sweep_enabled: true, retention_disk_driven_enabled: true)

    :sys.replace_state(builder, fn state -> %{state | workload_sync_done: true} end)
    assert BaseBuilder.retention_sweep_now(builder) == []
  end

  # An unregistered / .tmp-orphan on-disk base dir: noded reports it with
  # base_state UNSPECIFIED. The sweep must treat it as a candidate (it is neither
  # current nor BUILDING), so the reclaim drains orphans too.
  defp orphan_base(ref, workload, bytes) do
    %{ref: ref, workload: workload, size_bytes: bytes, base_state: :BASE_BUILD_STATE_UNSPECIFIED,
      created_at_unix_ms: System.system_time(:millisecond) - 3_601_000,
      snapshot_path: "/var/lib/embervm/scratch/embervm-noded/snapshots/bases/#{ref}"}
  end

  # Drive one build so the CP has a placed, current snapshot_ref for "w" on node-4.
  # The builder's own build_fun (set by the caller) returns `ref`.
  # Seed a capacity fact for "node-4" so placement succeeds. The fact includes
  # the workload so it can be in the fact from the start (retention tests read it).
  defp put_node_capacity_fact(table, workload, current_ref, exported?) do
    NodeCapacity.put(table, {"node-4", "ds"}, %{
      node_id: "node-4",
      configured_id: "node-4",
      instance_id: "node-4",
      cpu_vendor: "amd",
      size_class: "8gi",
      mem_budget_mib: 8_192,
      mem_headroom_mib: 8_000,
      live_vms: 0,
      max_live_vms: 8,
      workloads: %{
        workload => %{
          snapshot_ref: current_ref,
          base_state: :BASE_BUILD_STATE_READY,
          exported: exported?
        }
      },
      updated_at: 0
    })
  end

  defp build_current(builder, agent, ref) do
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 1, image_ref: "imgA"}))
    assert_eventually(fn -> match?(%{"snapshotRef" => ^ref}, latest(agent, "w")) end)
  end

  test "retention sweep evicts superseded local bases outside the desired set (gate on)" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__current")} end

    evict_fun = fn :fake_channel, workload, ref ->
      send(test_pid, {:evicted, workload, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        evict_fun: evict_fun,
        retention_sweep_enabled: true,
        retention_disk_driven_enabled: true
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")

    # The node reports its full on-disk inventory: the current base, a READY
    # superseded version the CP never tracked, AND an unregistered .tmp orphan
    # (base_state UNSPECIFIED). Both non-current dirs must evict.
    put_local_bases_fact(table, "w", "w__current", true, [
      ready_base("w__current", "w", 512),
      ready_base("w__superseded", "w", 2_048),
      orphan_base("w__tmporphan", "w", 1_024)
    ])

    plan = BaseBuilder.retention_sweep_now(builder)

    entry = Enum.find(plan, &(&1.workload == "w"))
    assert entry.skipped_unexported == false
    assert Enum.sort(entry.evict_refs) == ["w__superseded", "w__tmporphan"]
    assert entry.evict_bytes == 3_072
    assert entry.retention_accounting.bases_kept_current_unverified == 0

    # Gate on: both the superseded READY base and the .tmp orphan are evicted;
    # the current base is never touched.
    assert_receive {:evicted, "w", "w__superseded"}, 1_000
    assert_receive {:evicted, "w", "w__tmporphan"}, 1_000
    refute_receive {:evicted, "w", "w__current"}, 100
  end

  test "retention sweep holds candidates when the desired local base is unverified" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__phantom")} end,
        evict_fun: fn :fake_channel, workload, ref ->
          send(test_pid, {:evicted, workload, ref})
          {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
        end,
        retention_disk_driven_enabled: true
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__phantom")
    put_local_bases_fact(table, "w", "w__phantom", true, [ready_base("w__old", "w", 2_048)])

    log =
      capture_log(fn ->
        send(test_pid, {:plan, BaseBuilder.retention_sweep_now(builder)})
      end)

    assert_receive {:plan, [entry]}
    assert entry.evict_refs == []
    assert entry.retention_accounting.bases_kept_current_unverified == 1
    assert log =~ "retention held: current base unverified"
    assert log =~ "workload w on node node-4"
    assert log =~ "w__phantom"
    assert log =~ "w__old"
    refute_receive {:evicted, "w", "w__old"}, 200
  end

  test "retention sweep holds candidates when the desired local base is still building" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__current")} end,
        evict_fun: fn :fake_channel, workload, ref ->
          send(test_pid, {:evicted, workload, ref})
          {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
        end,
        retention_disk_driven_enabled: true
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")

    building =
      ready_base("w__current", "w", 512)
      |> Map.put(:base_state, :BASE_BUILD_STATE_BUILDING)

    put_local_bases_fact(table, "w", "w__current", true, [
      building,
      ready_base("w__old", "w", 2_048)
    ])

    log =
      capture_log(fn ->
        send(test_pid, {:plan, BaseBuilder.retention_sweep_now(builder)})
      end)

    assert_receive {:plan, [entry]}
    assert entry.evict_refs == []
    assert entry.retention_accounting.bases_kept_current_unverified == 1
    assert log =~ "retention held: current base unverified"
    assert log =~ "w__old"
    refute_receive {:evicted, "w", "w__old"}, 200
  end

  test "retention sweep evicts an unknown workload once the watcher is synced and coalesces bricks" do
    test_pid = self()
    table = new_cap_table()
    orphan = ready_base("deleted__old", "deleted", 2_048)

    put_node_local_bases_fact(table, "node-4", "large", "deleted", "deleted__current", true, [orphan])
    put_node_local_bases_fact(table, "node-4", "small", "deleted", "deleted__current", true, [orphan])

    builder = start_builder(
      capacity_table: table,
      evict_fun: fn :fake_channel, workload, ref ->
        send(test_pid, {:evicted, workload, ref})
        {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
      end,
      retention_sweep_enabled: true,
      retention_disk_driven_enabled: true
    )

    :sys.replace_state(builder, fn state -> %{state | workload_sync_done: true} end)
    [entry] = BaseBuilder.retention_sweep_now(builder)
    assert entry.workload == "deleted"
    assert entry.evict_refs == ["deleted__old"]
    assert entry.evict_bytes == 2_048
    assert_receive {:evicted, "deleted", "deleted__old"}, 1_000
    refute_receive {:evicted, "deleted", "deleted__old"}, 100
  end

  test "retention sweep does not evict unknown workloads before the watcher sync" do
    table = new_cap_table()
    put_node_local_bases_fact(table, "node-4", "ds", "deleted", "deleted__current", true, [ready_base("deleted__old", "deleted", 2_048)])
    builder = start_builder(capacity_table: table, retention_sweep_enabled: true, retention_disk_driven_enabled: true)

    plan = BaseBuilder.retention_sweep_now(builder)
    assert plan == []
  end

  test "retention sweep evicts a deleted workload without requiring an exported current base" do
    test_pid = self()
    table = new_cap_table()
    put_node_local_bases_fact(table, "node-4", "ds", "deleted", "deleted__current", false, [ready_base("deleted__old", "deleted", 512)])

    builder = start_builder(
      capacity_table: table,
      evict_fun: fn :fake_channel, workload, ref ->
        send(test_pid, {:evicted, workload, ref})
        {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
      end,
      retention_sweep_enabled: true,
      retention_disk_driven_enabled: true
    )

    :sys.replace_state(builder, fn state -> %{state | workload_sync_done: true} end)
    plan = BaseBuilder.retention_sweep_now(builder)
    assert [%{evict_refs: ["deleted__old"]}] = plan
    assert_receive {:evicted, "deleted", "deleted__old"}, 1_000
  end

  test "retention sweep does not evict a BUILDING base for an unknown workload" do
    table = new_cap_table()
    base = Map.put(ready_base("deleted__building", "deleted", 512), :base_state, :BASE_BUILD_STATE_BUILDING)
    put_unknown_local_bases_fact(table, "node-4", "ds", [base])
    builder = start_builder(capacity_table: table, retention_sweep_enabled: true, retention_disk_driven_enabled: true)

    :sys.replace_state(builder, fn state -> %{state | workload_sync_done: true} end)
    assert BaseBuilder.retention_sweep_now(builder) == []
  end

  test "retention sweep protects an unknown base still present in base_refs" do
    agent = start_recorder()
    table = new_cap_table()
    put_unknown_local_bases_fact(table, "node-4", "ds", [ready_base("deleted__held", "deleted", 512)])
    builder = start_builder(capacity_table: table, status_writer: recording_status_writer(agent))

    :ok = BaseBuilder.reconcile(builder, desc())
    :sys.replace_state(builder, fn state ->
      w = state.workloads["w"]
      w = %{w | base_refs: %{"deleted__held" => %{primed: 1, sessions: 0, evicted: false}}}
      %{state | workload_sync_done: true, workloads: %{"w" => w}}
    end)

    assert BaseBuilder.retention_sweep_now(builder) == []
  end

  test "retention sweep is a dry-run no-op with the gate OFF (default): plans but evicts nothing" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__current")} end

    evict_fun = fn :fake_channel, workload, ref ->
      send(test_pid, {:evicted, workload, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    # No retention_sweep_enabled opt => default false (what this PR ships).
    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        evict_fun: evict_fun
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")

    put_local_bases_fact(table, "w", "w__current", true, [
      ready_base("w__current", "w", 512),
      ready_base("w__orphan1", "w", 2_048)
    ])

    plan = BaseBuilder.retention_sweep_now(builder)

    # The plan still identifies the candidate (observability), but nothing is evicted.
    entry = Enum.find(plan, &(&1.workload == "w"))
    assert entry.evict_refs == ["w__orphan1"]
    assert entry.evict_bytes == 2_048
    refute_receive {:evicted, "w", "w__orphan1"}, 200
  end

  test "disk-driven gate OFF with legacy retention_sweep_enabled ON keeps production dry-run" do
    test_pid = self()
    table = new_cap_table()
    agent = start_recorder()

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__current")} end,
        retention_sweep_enabled: true,
        retention_disk_driven_enabled: false,
        evict_fun: fn :fake_channel, workload, ref ->
          send(test_pid, {:evicted, workload, ref})
          {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
        end
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")
    put_local_bases_fact(table, "w", "w__current", true, [ready_base("w__current", "w", 1), ready_base("w__disk-old", "w", 2_048)])

    log =
      capture_log(fn ->
        send(test_pid, {:plan, BaseBuilder.retention_sweep_now(builder)})
      end)

    assert_receive {:plan, [%{evict_refs: ["w__disk-old"], candidates: [_]}]}
    refute_receive {:evicted, "w", "w__disk-old"}, 200
    refute log =~ "ARMED"
    refute log =~ "DELETING"
    assert log =~ "DRY RUN"
  end

  test "disk-driven gate ON evicts disk-enumerated candidates up to the sweep cap" do
    test_pid = self()
    table = new_cap_table()
    agent = start_recorder()
    bases = [ready_base("w__current", "w", 1) | (for n <- 1..21, do: ready_base("w__disk-old#{n}", "w", n))]

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__current")} end,
        retention_disk_driven_enabled: true,
        evict_fun: fn :fake_channel, workload, ref ->
          send(test_pid, {:evicted, workload, ref})
          {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
        end
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")
    put_local_bases_fact(table, "w", "w__current", true, bases)
    [entry] = BaseBuilder.retention_sweep_now(builder)

    assert length(entry.evict_refs) == 21
    for _ <- 1..20, do: assert_receive({:evicted, "w", _}, 1_000)
    refute_receive {:evicted, "w", _}, 200
  end

  test "disk-driven retention log wording is DRY RUN when its gate is off" do
    table = new_cap_table()
    builder = start_builder(capacity_table: table, retention_disk_driven_enabled: false)
    :sys.replace_state(builder, fn state -> %{state | workload_sync_done: true} end)
    put_node_local_bases_fact(table, "node-4", "ds", "deleted", "deleted__current", true, [ready_base("deleted__old", "deleted", 512)])

    log = capture_log(fn -> BaseBuilder.retention_sweep_now(builder) end)
    assert log =~ "DRY RUN"
    refute log =~ "ARMED"
    refute log =~ "DELETING"
  end

  test "retention sweep reclaims an orphan when the current base is not exported" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__current")} end

    evict_fun = fn :fake_channel, workload, ref ->
      send(test_pid, {:evicted, workload, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        evict_fun: evict_fun,
        retention_sweep_enabled: true,
        retention_disk_driven_enabled: true
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")

    # The current base is present but NOT yet exported. That must not prevent
    # reclaiming an aged, superseded base that nothing references.
    put_local_bases_fact(table, "w", "w__current", false, [
      ready_base("w__current", "w", 512),
      ready_base("w__orphan1", "w", 2_048)
    ])

    plan = BaseBuilder.retention_sweep_now(builder)

    entry = Enum.find(plan, &(&1.workload == "w"))
    assert entry.skipped_unexported == false
    assert entry.evict_refs == ["w__orphan1"]
    assert_receive {:evicted, "w", "w__orphan1"}, 1_000
  end

  test "retention filters protect every non-current base safety rail when current is unexported" do
    agent = start_recorder()
    table = new_cap_table()

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__current")} end,
        retention_sweep_enabled: true,
        retention_disk_driven_enabled: true
      )

    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")

    :sys.replace_state(builder, fn state ->
      w = state.workloads["w"]
      w = %{w | base_refs: %{"w__refcounted" => %{primed: 1, sessions: 0, evicted: true}}}
      %{state | workloads: %{"w" => w}}
    end)

    young = Map.put(ready_base("w__young", "w", 4), :created_at_unix_ms, System.system_time(:millisecond) - 100_000)
    building = Map.put(ready_base("w__building", "w", 8), :base_state, :BASE_BUILD_STATE_BUILDING)

    put_local_bases_fact(table, "w", "w__current", false, [
      ready_base("w__current", "w", 512),
      ready_base("w__refcounted", "w", 1),
      building,
      young,
      ready_base("w__orphan", "w", 2_048)
    ])

    [entry] = Enum.filter(BaseBuilder.retention_sweep_now(builder), &(&1.workload == "w"))
    assert entry.evict_refs == ["w__orphan"]
  end

  test "retention sweep and export per node when fleet has mixed vendors" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    put_brick(table, "node-1", "intel", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    put_brick(table, "node-4", "amd", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    put_node_local_bases_fact(table, "node-1", "intel", "w", "w__intel", true, [
      ready_base("w__intel", "w", 512),
      ready_base("w__old_intel", "w", 2_048)
    ])

    put_node_local_bases_fact(table, "node-4", "amd", "w", "w__amd", false, [
      ready_base("w__amd", "w", 512),
      ready_base("w__old_amd", "w", 4_096)
    ])

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
      send(test_pid, {:exported, ref.ref})
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
    end

    connect_fun = fn address ->
      send(test_pid, {:connected, address})
      {:ok, :fake_channel}
    end

    builder =
      start_builder(
        nodes: [
          %{id: "node-1/intel", address: "node-1/intel"},
          %{id: "node-4/amd", address: "node-4/amd"}
        ],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__intel")} end,
        connect_fun: connect_fun,
        export_fun: export_fun,
        retention_sweep_enabled: true,
        retention_disk_driven_enabled: true
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "w__intel" end)

    assert_receive {:connected, "node-4/amd"}, 1_000
    assert_receive {:exported, "w__amd"}, 1_000
    refute_receive {:exported, "w__intel"}, 100
    refute_receive {:connected, "node-4/amd"}, 100

    plan = BaseBuilder.retention_sweep_now(builder)
    assert Enum.count(plan, &(&1.workload == "w")) == 2

    intel = Enum.find(plan, &(&1.node_id == "node-1/intel"))
    assert intel.skipped_unexported == false
    assert intel.evict_refs == ["w__old_intel"]
    assert intel.evict_bytes == 2_048

    amd = Enum.find(plan, &(&1.node_id == "node-4/amd"))
    assert amd.skipped_unexported == false
    assert amd.evict_refs == ["w__old_amd"]
    assert amd.evict_bytes == 4_096
  end

  test "retention and export run once for two bricks sharing a node's base inventory" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    put_brick(table, "node-4", "large", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    put_brick(table, "node-4", "small", size_class: "2gi", mem_budget: 2_048, mem_headroom: 2_000)

    local_bases = [
      ready_base("w__current", "w", 512),
      ready_base("w__old", "w", 2_048)
    ]

    put_node_local_bases_fact(table, "node-4", "large", "w", "w__current", false, local_bases)
    put_node_local_bases_fact(table, "node-4", "small", "w", "w__current", false, local_bases)

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
      send(test_pid, {:exported, ref.ref})
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
    end

    evict_fun = fn :fake_channel, workload, ref ->
      send(test_pid, {:evicted, workload, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    builder =
      start_builder(
        nodes: [
          %{id: "node-4/large", address: "large"},
          %{id: "node-4/small", address: "small"}
        ],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__current")} end,
        export_fun: export_fun,
        evict_fun: evict_fun,
        retention_sweep_enabled: true,
        retention_disk_driven_enabled: true
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "w__current" end)

    assert_receive {:exported, "w__current"}, 1_000
    refute_receive {:exported, "w__current"}, 100

    # Export coalescing needs the unexported facts above. Retention should plan the
    # shared inventory once per node regardless of export state.
    put_node_local_bases_fact(table, "node-4", "large", "w", "w__current", true, local_bases)
    put_node_local_bases_fact(table, "node-4", "small", "w", "w__current", true, local_bases)

    plan = BaseBuilder.retention_sweep_now(builder)
    entries = Enum.filter(plan, &(&1.workload == "w"))
    assert length(entries) == 1
    assert hd(entries).evict_refs == ["w__old"]
    assert hd(entries).evict_bytes == 2_048

    assert_receive {:evicted, "w", "w__old"}, 1_000
    refute_receive {:evicted, "w", "w__old"}, 100
  end


  test "export skips targets without node_addr entries and logs warning" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    # Set up a capacity fact for a node that has NO entry in state.node_addr
    put_node_local_bases_fact(table, "node-99", "unknown", "w", "w__unknown", false, [
      ready_base("w__unknown", "w", 512)
    ])

    # The builder's own node needs a fact of its own for placement now that a
    # fact-less instance is excluded rather than a fail-open wildcard.
    put_instance_fact(table, "node-1/intel")

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
      send(test_pid, {:exported, ref.ref})
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
    end

    connect_fun = fn address ->
      send(test_pid, {:connected, address})
      {:ok, :fake_channel}
    end

    # Builder has no entry for "node-99/unknown" in nodes list, so it has no address
    builder =
      start_builder(
        nodes: [%{id: "node-1/intel", address: "node-1/intel"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__intel")} end,
        connect_fun: connect_fun,
        export_fun: export_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "w__intel" end)

    # Export should target node-99/unknown, but the nil-address guard skips it
    # (it logs a warning instead of trying to dial nil)
    refute_receive {:connected, "node-99/unknown"}, 500
    refute_receive {:exported, "w__unknown"}, 500
  end


  test "retention sweep never evicts a still-refcounted superseded ref" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    # Turnover so the CP tracks snap-old as a superseded ref, then hold it
    # refcounted (a primed VM still rides it: primed reported, sessions unknown).
    build_fun = fn :fake_channel, req ->
      ref = if req.image_ref == "imgA", do: "snap-old", else: "snap-new"
      {:ok, resp(ref)}
    end

    evict_fun = fn :fake_channel, workload, ref ->
      send(test_pid, {:evicted, workload, ref})
      {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
    end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        evict_fun: evict_fun,
        retention_sweep_enabled: true,
        retention_disk_driven_enabled: true
      )

    # An explicit capacity_table skips start_builder's default seeding, and a
    # fact-less instance is no longer placeable, so seed node-4 ourselves.
    put_instance_fact(table, "node-4")

    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 1, image_ref: "imgA"}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap-old"}, latest(agent, "w")) end)
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2, image_ref: "imgB"}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap-new"}, latest(agent, "w")) end)

    # snap-old is still refcounted (a primed VM rides it): sessions unknown (nil)
    # keeps it un-evictable in the R2 path AND desired in the sweep.
    :ok = BaseBuilder.report_base_refs(builder, "snap-old", primed: 1)

    put_local_bases_fact(table, "w", "snap-new", true, [
      ready_base("snap-new", "w", 512),
      ready_base("snap-old", "w", 2_048)
    ])

    plan = BaseBuilder.retention_sweep_now(builder)

    entry = Enum.find(plan, &(&1.workload == "w"))
    # snap-old is refcounted, so it is in the desired set: nothing to evict.
    assert entry == nil or entry.evict_refs == []
    refute_receive {:evicted, "w", "snap-old"}, 200
  end

  test "retention sweep emits an on-disk manifest and honors the minimum age" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()
    builder = start_builder(
      capacity_table: table,
      status_writer: recording_status_writer(agent),
      build_fun: fn :fake_channel, _req -> {:ok, resp("w__current")} end,
      evict_fun: fn :fake_channel, workload, ref ->
        send(test_pid, {:evicted, workload, ref})
        {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
      end
    )
    put_node_capacity_fact(table, "w", "placeholder", true)
    build_current(builder, agent, "w__current")

    young = Map.put(ready_base("w__young", "w", 10), :created_at_unix_ms, System.system_time(:millisecond) - 100_000)
    old = Map.put(ready_base("w__old", "w", 20), :created_at_unix_ms, System.system_time(:millisecond) - 4_000_000)
    unknown = Map.put(ready_base("w__unknown", "w", 30), :created_at_unix_ms, 0)
    put_local_bases_fact(table, "w", "w__current", true, [ready_base("w__current", "w", 1), young, old, unknown])

    [entry] = BaseBuilder.retention_sweep_now(builder)
    assert entry.evict_refs == ["w__old"]
    # Verify the manifest contains the old base with correct path and age
    assert [candidate] = entry.candidates
    assert candidate.path == "/var/lib/embervm/scratch/embervm-noded/snapshots/bases/w__old"
    assert candidate.size_bytes == 20
    assert candidate.age_seconds >= 3_600
    # Verify young base is filtered by age and unknown-age base is treated as new (not a candidate)
    assert candidate.workload == "w"
    assert Enum.all?(entry.candidates, &(&1.age_seconds > 0))
    refute Enum.any?(entry.candidates, &(&1.path =~ "w__unknown"))
    refute_receive {:evicted, "w", "w__old"}, 100
  end

  test "retention sweep caps armed evictions at twenty bases but keeps the full plan" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()
    bases = [ready_base("w__current", "w", 1) | (for n <- 1..21, do: ready_base("w__old#{n}", "w", n))]

    put_local_bases_fact(table, "w", "w__current", true, bases)
    builder = start_builder(
      capacity_table: table,
      status_writer: recording_status_writer(agent),
      build_fun: fn :fake_channel, _req -> {:ok, resp("w__current")} end,
      retention_sweep_enabled: true,
      retention_disk_driven_enabled: true,
      evict_fun: fn :fake_channel, workload, ref ->
        send(test_pid, {:evicted, workload, ref})
        {:ok, %Embervm.Node.V1.EvictArtifactResponse{}}
      end
    )
    # No put_node_capacity_fact here: it writes the SAME {"node-4", "ds"} key as
    # the put_local_bases_fact above and would wipe the 21-base inventory this
    # test sweeps. The bases fact itself is wildcard-shaped (no size_class), so
    # placement works from it directly.
    build_current(builder, agent, "w__current")

    plan = BaseBuilder.retention_sweep_now(builder)
    assert [%{evict_refs: refs}] = Enum.filter(plan, &(&1.workload == "w"))
    assert length(refs) == 21
    for(_ <- 1..20, do: assert_receive({:evicted, "w", _ref}, 1_000))
    refute_receive {:evicted, "w", _}, 100
  end

  # -- discovery-fed node set (artifact-decoupling PR-C, C4) -------------------

  test "add_node/3 re-drives a workload held with no node so its base finally builds" do
    # Under EndpointSlice discovery the builder is SEEDED EMPTY at boot (it cannot
    # touch Finch at construction), so a workload admitted before any node is
    # discovered is held {:pending, :no_node}. When NodeRegistry's discovery later
    # calls add_node, the held workload must re-drive and build.
    agent = start_recorder()
    table = new_cap_table()
    put_instance_fact(table, "node-4")
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap-late", "sha256:late")} end

    builder =
      start_builder(
        nodes: [],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    # Admitted with NO node: held pending, no build, Ready=False no_node.
    :ok = BaseBuilder.reconcile(builder, desc())

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> match?(%{"status" => "False"}, condition(s, "Ready")) and s["snapshotRef"] == nil
      end
    end)

    # A node is discovered: the held workload re-drives and its base builds.
    :ok = BaseBuilder.add_node(builder, "node-4", "node-4:9090")

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> s["snapshotRef"] == "snap-late"
      end
    end)

    st = BaseBuilder.status(builder)
    assert st.workloads["w"].node_id == "node-4"
    assert st.workloads["w"].built
  end

  test "remove_node/2 unpins a workload placed on the vanished node" do
    agent = start_recorder()
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    builder =
      start_builder(nodes: [@node], status_writer: recording_status_writer(agent), build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc())

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        s -> s["snapshotRef"] == "snap1"
      end
    end)

    # The node vanishes from discovery: the workload is unpinned (node_id -> nil)
    # so a later add re-places it, and the node is dropped from the placement set.
    :ok = BaseBuilder.remove_node(builder, "node-4")

    assert_eventually(fn ->
      st = BaseBuilder.status(builder)
      st.workloads["w"].node_id == nil and not Map.has_key?(st.nodes, "node-4")
    end)
  end

  # -- size-aware placement (Step 5, brick co-location) -----------------------

  # An isolated capacity table with per-instance brick facts.
  defp new_cap_table do
    table = :"bb_cap_#{System.unique_integer([:positive])}"
    NodeCapacity.create(table)
    table
  end

  defp put_brick(table, node_id, pod_uid, opts) do
    NodeCapacity.put(table, {node_id, pod_uid}, %{
      node_id: node_id,
      configured_id: node_id,
      instance_id: "#{node_id}/#{pod_uid}",
      cpu_vendor: Keyword.get(opts, :cpu_vendor, "amd"),
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_budget_mib: Keyword.get(opts, :mem_budget, 8_192),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom, 8_000),
      live_vms: 0,
      max_live_vms: 8,
      updated_at: 0
    })
  end

  defp put_instance_fact(table, instance_id) do
    [node_id | pod_uid] = String.split(instance_id, "/", parts: 2)
    pod_uid = List.first(pod_uid) || "test"

    # Deliberately a REPORTED wildcard (blank class and vendor, zero budget):
    # wildcard? keeps it build-eligible, so default-table tests place exactly as
    # before the fact-less exclusion landed, while the blank vendor keeps it out
    # of fleet_vendors and off the vendor-keyed tests' subject matter. Tests
    # that assert on budgets or vendors seed their own explicit facts.
    NodeCapacity.put(table, {node_id, pod_uid}, %{
      node_id: node_id,
      configured_id: node_id,
      instance_id: instance_id,
      cpu_vendor: "",
      size_class: "",
      mem_budget_mib: 0,
      mem_headroom_mib: 0,
      live_vms: 0,
      max_live_vms: 8,
      updated_at: 0
    })
  end


  test "fact-less instances are excluded from build placement" do
    test_pid = self()
    table = new_cap_table()
    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    build_fun = fn :fake_channel, req ->
      send(test_pid, {:build_started, req.trace.workload})
      {:ok, resp("snap-facted")}
    end

    builder =
      start_builder(
        nodes: [
          %{id: "node-5/missing", address: "missing"},
          %{id: "node-4/big", address: "facted"}
        ],
        capacity_table: table,
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{name: "facted"}))
    assert_receive {:build_started, "facted"}, 1_000
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["facted"].built end)
    assert BaseBuilder.status(builder).workloads["facted"].node_id == "node-4/big"

    empty_table = new_cap_table()

    empty_builder =
      start_builder(
        nodes: [%{id: "node-5/missing", address: "missing"}],
        capacity_table: empty_table,
        build_fun: fn :fake_channel, _req ->
          send(test_pid, :unexpected_build)
          {:ok, resp("unexpected")}
        end
      )

    :ok = BaseBuilder.reconcile(empty_builder, desc(%{name: "pending"}))
    refute_receive :unexpected_build, 100

    pending = :sys.get_state(empty_builder)
    assert pending.workloads["pending"].node_id == nil
    assert pending.workloads["pending"].snapshot_ref == nil
    assert pending.workers == %{}
  end

  defp start_departure_builder(test_pid) do
    table = new_cap_table()
    put_brick(table, "node-4", "departing", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    put_brick(table, "node-5", "survivor", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)
    {:ok, attempts} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      attempt = Agent.get_and_update(attempts, fn n -> {n + 1, n + 1} end)
      send(test_pid, {:departure_build_started, attempt, self()})

      if attempt == 1 do
        receive do
          :unreachable_release -> {:ok, resp("departed")}
        end
      else
        {:ok, resp("survivor")}
      end
    end

    builder =
      start_builder(
        nodes: [
          %{id: "node-4/departing", address: "departing"},
          %{id: "node-5/survivor", address: "survivor"}
        ],
        capacity_table: table,
        build_fun: build_fun,
        base_backoff_ms: 30_000,
        max_backoff_ms: 60_000
      )

    {builder, attempts}
  end

  test "remove_node kills an in-flight build and reconcile places its retry on a survivor" do
    {builder, attempts} = start_departure_builder(self())

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive {:departure_build_started, 1, worker}, 1_000

    :ok = BaseBuilder.remove_node(builder, "node-4/departing")
    assert_eventually(fn -> not Process.alive?(worker) end)

    assert_eventually(fn ->
      :sys.get_state(builder).workloads["w"].retry_timer != nil
    end)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive {:departure_build_started, 2, _worker}, 1_000

    assert_eventually(fn ->
      workload = :sys.get_state(builder).workloads["w"]
      workload.node_id == "node-5/survivor" and workload.snapshot_ref == "survivor"
    end)

    assert Agent.get(attempts, & &1) == 2
  end

  test "finish_build removes the retained entry for a departed node" do
    {builder, _attempts} = start_departure_builder(self())

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive {:departure_build_started, 1, worker}, 1_000

    :ok = BaseBuilder.remove_node(builder, "node-4/departing")
    assert_eventually(fn -> not Process.alive?(worker) end)

    assert_eventually(fn ->
      state = :sys.get_state(builder)

      state.workloads["w"].retry_timer != nil and
        Map.get(state.nodes, "node-4/departing") == nil
    end)
  end

  test "a changed build does not dial a departed node without an address" do
    test_pid = self()
    agent = start_recorder()
    table = new_cap_table()

    put_brick(table, "node-a", "departing",
      size_class: "16gi",
      mem_budget: 16_384,
      mem_headroom: 16_000
    )

    connect_fun = fn
      nil ->
        send(test_pid, {:connected, nil})
        raise FunctionClauseError

      address ->
        send(test_pid, {:connected, address})
        {:ok, :fake_channel}
    end

    build_fun = fn :fake_channel, req ->
      if req.image_ref == "imgA" do
        send(test_pid, {:departing_build_started, self()})

        receive do
          :release -> {:ok, resp("stale")}
        end
      else
        send(test_pid, {:survivor_build_started, self()})
        {:ok, resp("survivor")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-a/departing", address: "node-a:9090"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        connect_fun: connect_fun,
        build_fun: build_fun,
        base_backoff_ms: 30_000,
        max_backoff_ms: 60_000
      )

    :ok =
      BaseBuilder.reconcile(
        builder,
        desc(%{generation: 1, image_ref: "imgA", mem_mib: 4_000})
      )

    assert_receive {:connected, "node-a:9090"}, 1_000
    assert_receive {:departing_build_started, worker}, 1_000

    :ok =
      BaseBuilder.reconcile(
        builder,
        desc(%{generation: 2, image_ref: "imgB", mem_mib: 4_000})
      )

    _ = :sys.get_state(builder)
    writes_before_departure = length(recorded(agent))

    log =
      capture_log(fn ->
        :ok = BaseBuilder.remove_node(builder, "node-a/departing")
        assert_eventually(fn -> not Process.alive?(worker) end)

        assert_eventually(fn ->
          state = :sys.get_state(builder)

          state.workloads["w"].retry_timer != nil and state.workers == %{} and
            Map.get(state.nodes, "node-a/departing") == nil
        end)
      end)

    assert Process.alive?(builder)
    refute_receive {:connected, nil}, 100
    assert length(Regex.scan(~r/BuildBase for embervm\/w failed/, log)) == 1
    assert log =~ "no_address"
    assert log =~ "node-a/departing"
    refute log =~ "FunctionClauseError"

    writes_after_departure = Enum.drop(recorded(agent), writes_before_departure)

    refute Enum.any?(writes_after_departure, fn {_namespace, "w", status_map} ->
             match?(%{"reason" => "BaseBuilding"}, condition(status_map, "BaseBuilt"))
           end)

    assert Enum.any?(writes_after_departure, fn {_namespace, "w", status_map} ->
             match?(%{"reason" => "BuildFailed"}, condition(status_map, "BaseBuilt"))
           end)

    put_brick(table, "node-b", "healthy",
      size_class: "16gi",
      mem_budget: 16_384,
      mem_headroom: 16_000
    )

    :ok = BaseBuilder.add_node(builder, "node-b/healthy", "node-b:9090")

    assert_receive {:connected, "node-b:9090"}, 1_000
    assert_receive {:survivor_build_started, _worker}, 1_000

    assert_eventually(fn ->
      workload = :sys.get_state(builder).workloads["w"]
      workload.node_id == "node-b/healthy" and workload.snapshot_ref == "survivor"
    end)

    refute_receive {:connected, nil}, 100
  end

  test "placement picks the LARGEST-budget eligible instance, never List.first" do
    # Three co-located bricks on node-4; the first-registered is the 2Gi one (the
    # old List.first bug pinned everything to it). The base must land on the 16Gi
    # brick (largest budget) for a 4Gi-need workload.
    table = new_cap_table()
    put_brick(table, "node-4", "small", size_class: "2gi", mem_budget: 2_048, mem_headroom: 100)
    put_brick(table, "node-4", "mid", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)
    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    agent = start_recorder()
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap-big")} end

    builder =
      start_builder(
        # node_ids ARE instance_ids in production; registration order puts small first.
        nodes: [
          %{id: "node-4/small", address: "a"},
          %{id: "node-4/mid", address: "a"},
          %{id: "node-4/big", address: "a"}
        ],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))

    assert_eventually(fn ->
      match?(%{"snapshotRef" => "snap-big"}, latest(agent, "w"))
    end)

    st = BaseBuilder.status(builder)
    assert st.workloads["w"].node_id == "node-4/big"
  end

  test "placement never captures a base on a too-small instance" do
    # A 2Gi brick and an 8Gi brick; a 4Gi-need workload must NOT land on the 2Gi one.
    table = new_cap_table()
    put_brick(table, "node-4", "small", size_class: "2gi", mem_budget: 2_048, mem_headroom: 100)
    put_brick(table, "node-4", "ok", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)

    agent = start_recorder()
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap-ok")} end

    builder =
      start_builder(
        nodes: [%{id: "node-4/small", address: "a"}, %{id: "node-4/ok", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))

    assert_eventually(fn -> match?(%{"snapshotRef" => "snap-ok"}, latest(agent, "w")) end)
    assert BaseBuilder.status(builder).workloads["w"].node_id == "node-4/ok"
  end

  test "placement re-places a workload when a LARGER eligible instance registers" do
    # Start with only an 8Gi brick; the base pins there. A 16Gi brick then registers,
    # and a reconcile re-places the workload onto it (a bigger eligible instance).
    table = new_cap_table()
    put_brick(table, "node-4", "mid", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)

    agent = start_recorder()
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    builder =
      start_builder(
        nodes: [%{id: "node-4/mid", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].node_id == "node-4/mid" end)

    # A larger brick appears on the node and registers.
    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    :ok = BaseBuilder.add_node(builder, "node-4/big", "a")
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))

    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].node_id == "node-4/big" end)
  end

  test "placement re-places when its chosen instance deregisters" do
    table = new_cap_table()
    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    put_brick(table, "node-4", "mid", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)

    agent = start_recorder()
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a"}, %{id: "node-4/mid", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].node_id == "node-4/big" end)

    # The chosen instance vanishes: remove_node unpins it (node_id -> nil), and a
    # re-add/reconcile re-places onto the remaining eligible 8Gi brick.
    :ok = BaseBuilder.remove_node(builder, "node-4/big")
    NodeCapacity.drop(table, {"node-4", "big"})
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))

    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].node_id == "node-4/mid" end)
  end

  # -- base export durability (base-durability PR-1) --------------------------

  # A fake op-log module: append/2 forwards the op to the test pid so a test can
  # assert the :artifact_exported audit entry was written. Matches the
  # {op_log, op} arg order the real Embervm.OpLog.SQLite.append/2 takes.
  defmodule FakeOpLog do
    def append(pid, op) when is_pid(pid) do
      send(pid, {:op_appended, op})
      :ok
    end
  end

  test "a successful build exports the current base to the store on its node" do
    agent = start_recorder()
    test_pid = self()

    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} = req ->
      send(test_pid, {:exported, ref.kind, ref.workload, ref.ref, req.trace.workload})
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 123, skipped: false, generation: 0}}
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_fun: export_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{}))
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)

    # Export fires for the freshly-built current base, kind BASE, on its node.
    assert_receive {:exported, :ARTIFACT_KIND_BASE, "w", "snap1", "w"}, 1_000
  end

  test "a successful export appends the :artifact_exported op-log audit entry" do
    agent = start_recorder()
    test_pid = self()

    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    export_fun = fn :fake_channel, _req ->
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 42, skipped: false, generation: 0}}
    end

    builder =
      start_builder(
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_fun: export_fun,
        op_log: test_pid,
        op_log_mod: FakeOpLog
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{}))

    assert_receive {:op_appended, op}, 1_000
    assert op.kind == :artifact_exported
    assert op.workload == "w"
    assert op.principal == "system:base:w"
    assert op.payload.kind == "base"
    assert op.payload.ref == "snap1"
    assert op.payload.bytes_moved == 42
  end

  test "the export reconcile re-exports a current base reported present-but-unexported" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
      send(test_pid, {:exported, ref.ref})
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_fun: export_fun,
        # A tight reconcile cadence so the sweep fires within the test window.
        export_reconcile_interval_ms: 20
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    # Drain the immediate post-build export so the reconcile-driven one is the
    # signal under test.
    assert_receive {:exported, "snap1"}, 1_000

    # The node now reports the current base READY but NOT exported: the periodic
    # reconcile must re-issue ExportArtifact for it.
    put_base_fact(table, "node-4", "big", "w", "snap1", :BASE_BUILD_STATE_READY, false)
    assert_receive {:exported, "snap1"}, 1_000
  end

  test "the export reconcile does not re-export a base already reported exported" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
      send(test_pid, {:exported, ref.ref})
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_fun: export_fun,
        export_reconcile_interval_ms: 20
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    # Drain the immediate post-build export.
    assert_receive {:exported, "snap1"}, 1_000

    # The node reports the current base as ALREADY exported: the reconcile must
    # NOT fire another export for it.
    put_base_fact(table, "node-4", "big", "w", "snap1", :BASE_BUILD_STATE_READY, true)
    refute_receive {:exported, "snap1"}, 200
  end

  test "export reconcile resolves a stale placement instance_id to the current node-mate (pod-restart churn)" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
      send(test_pid, {:exported, ref.ref})
      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
    end

    builder =
      start_builder(
        # Both the build-time pod (big) and the post-restart pod (big2) are
        # registered with addresses; only big is a brick, so the build places there.
        nodes: [%{id: "node-4/big", address: "a"}, %{id: "node-4/big2", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_fun: export_fun,
        export_reconcile_interval_ms: 20
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)
    assert_receive {:exported, "snap1"}, 1_000

    # The placed pod (big) restarts as a new pod (big2) on the SAME node. Only big2
    # now reports the base READY-but-unexported; the build-time instance_id (big) is
    # stale and no longer in the capacity table's workload facts. The reconcile must
    # resolve big -> big2 (same node) and re-export via big2's address. Before the
    # node-name remap this missed on the churned instance_id and the export parked.
    put_base_fact(table, "node-4", "big2", "w", "snap1", :BASE_BUILD_STATE_READY, false)
    assert_receive {:exported, "snap1"}, 1_000
  end

  test "the reconcile does not fire a second export RPC for a ref already in flight, and re-issues once it settles" do
    # fast-durability-export fix: the CP-side in-flight set trims reconcile RPC churn
    # (noded's exportDedupe is what serializes the actual multi-minute upload). While
    # an ExportArtifact call for a ref is in flight, the reconcile must NOT spawn a
    # second export RPC for it, and MUST re-issue once the in-flight one settles (so a
    # failed export is not skipped forever). export_fun blocks until released, holding
    # the call in flight; this fake conflates the RPC and the upload, which is what the
    # CP set keys on (the spawn is skipped, not the upload).
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, _req -> {:ok, resp("snap1")} end

    export_fun = fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
      # Report this worker's pid so the test can release it, and that the export
      # started, then block until released (standing in for a slow upload).
      send(test_pid, {:export_started, ref.ref, self()})

      receive do
        :release -> :ok
      after
        2_000 -> :ok
      end

      {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_fun: export_fun,
        # Timer disabled: the test drives :export_reconcile explicitly so dedup is
        # asserted deterministically, not on a racing cadence.
        export_reconcile_interval_ms: 0
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    # The immediate post-build export starts and is now blocked (in flight).
    assert_receive {:export_started, "snap1", worker1}, 1_000

    # The node still reports the base present-but-unexported (the slow upload has
    # not landed). Two reconcile sweeps while the export is in flight must NOT spawn
    # a second export for the same ref.
    put_base_fact(table, "node-4", "big", "w", "snap1", :BASE_BUILD_STATE_READY, false)
    send(builder, :export_reconcile)
    send(builder, :export_reconcile)
    refute_receive {:export_started, "snap1", _}, 200

    # Release the in-flight upload; its worker sends :export_done, clearing tracking.
    send(worker1, :release)

    # A fresh reconcile re-issues the export (still reported unexported) exactly
    # because the prior attempt settled. assert_receive waits for the new worker,
    # which cannot appear until :export_done cleared the in-flight key.
    assert_eventually(fn ->
      send(builder, :export_reconcile)

      receive do
        {:export_started, "snap1", worker2} ->
          send(worker2, :release)
          true
      after
        50 -> false
      end
    end)
  end

  # Seed one node's per-workload base fact (snapshot_ref, base_state, exported)
  # into an existing brick capacity row so the export reconcile can read it.
  # Merges into the row put_brick/4 already wrote, keyed the same way.
  # -- restore-first (hydrate-on-miss, base-durability PR-2) -------------------

  # Bring a workload to a recorded-and-built state (snapshot_ref set,
  # built_signature == signature) WITH a confirmed store copy (the node reports
  # the base READY + exported, and an export-reconcile tick lands it in the
  # store_confirmed ledger), then flip the node fact so the base reads
  # AFFIRMATIVELY absent (BASE_BUILD_STATE_NONE): the recorded-but-absent
  # trigger. Returns {builder, agent, table}. The initial export is drained so
  # it never perturbs later assertions. Since #4893, restore-first requires
  # positive store-fetchability evidence, so the confirmation step here is what
  # makes the later reconcile classify :hydrate rather than :unfetchable.
  defp build_then_report_base_absent(opts) do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()
    build_fun = Keyword.get(opts, :build_fun, fn :fake_channel, _req -> {:ok, resp("snap1")} end)

    builder =
      start_builder(
        [
          nodes: [%{id: "node-4/big", address: "a"}],
          capacity_table: table,
          status_writer: recording_status_writer(agent),
          build_fun: build_fun,
          # Signal each export so the immediate post-build one can be drained.
          export_fun: fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
            send(test_pid, {:exported, ref.ref})

            {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
          end
        ] ++
          Keyword.take(opts, [
            :connect_fun,
            :restore_fun,
            :op_log,
            :op_log_mod,
            :hydrate_poll_interval_ms,
            :hydrate_poll_max
          ])
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)
    assert_receive {:exported, "snap1"}, 1_000

    # The upload LANDS on the node: the fact flips to exported=true, and one
    # reconcile tick records that as store-fetchability evidence for snap1.
    put_base_fact(table, "node-4", "big", "w", "snap1", :BASE_BUILD_STATE_READY, true)
    send(builder, :export_reconcile)
    assert_eventually(fn ->
      :sys.get_state(builder).workloads["w"].store_confirmed["amd"].ref == "snap1"
    end)

    # The node now AFFIRMATIVELY reports the recorded base absent (a cold-start /
    # node replacement / scratch loss), the sole restore-first trigger.
    put_base_fact(table, "node-4", "big", "w", "", :BASE_BUILD_STATE_NONE, false)

    {builder, agent, table}
  end

  test "restore-first: a recorded-but-absent base is HYDRATED, not rebuilt" do
    test_pid = self()

    # A restore that fast-ACKs accepted=true (noded's async download path).
    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    build_calls = start_recorder()

    {builder, _agent, table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50,
        # Count any SECOND build: a hydrate must not rebuild.
        build_fun: fn :fake_channel, _req ->
          Agent.update(build_calls, &[:build | &1])
          {:ok, resp("snap1")}
        end
      )

    # Re-reconcile with the SAME (unchanged) spec: restore-first must trigger.
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive {:restore_called, "snap1"}, 1_000

    # The async download "lands": flip the node fact back to READY so the poll
    # sees the base present and the hydrate completes.
    put_base_fact(table, "node-4", "big", "w", "snap1", :BASE_BUILD_STATE_READY, true)

    # Give the poll a moment; exactly ONE build (the initial one) ever ran.
    Process.sleep(80)
    assert Agent.get(build_calls, &length(&1)) == 1
  end

  test "hydrate fallback disproves the ledger and forces a rebuild" do
    test_pid = self()
    {:ok, calls} = Agent.start_link(fn -> 0 end)
    # noded reports the ref is not in the store: a fast, distinguishable miss.
    restore_fun = fn :fake_channel, _req ->
      {:error, %GRPC.RPCError{status: 9, message: "not present in store"}}
    end

    {builder, agent, _table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50,
        build_fun: fn :fake_channel, _req ->
          call = Agent.get_and_update(calls, fn n -> {n + 1, n + 1} end)

          if call == 2 do
            send(test_pid, {:rebuilt, self()})

            receive do
              :release_rebuild -> {:ok, resp("snap1")}
            end
          else
            {:ok, resp("snap1")}
          end
        end
      )

    # Re-reconcile: restore-first tries, gets FAILED_PRECONDITION, falls back to a
    # rebuild AT ONCE (no poll wait).
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive {:rebuilt, rebuild_worker}, 1_000
    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        status ->
          base_built = condition(status, "BaseBuilt")
          base_built["status"] == "False" and base_built["reason"] == "BaseBuilding"
      end
    end)
    refute Map.has_key?(latest(agent, "w"), "snapshotRef")
    send(rebuild_worker, :release_rebuild)
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap1"}, latest(agent, "w")) end)
    assert Agent.get(calls, & &1) == 2
  end

  test "hydrate fallback with a changed signature uses plain reconcile semantics" do
    test_pid = self()
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    restore_fun = fn :fake_channel, _req ->
      send(test_pid, {:restore_started, self()})

      receive do
        :release_restore ->
          {:error, %GRPC.RPCError{status: 9, message: "not present in store"}}
      end
    end

    build_fun = fn :fake_channel, req ->
      call = Agent.get_and_update(calls, fn n -> {n + 1, n + 1} end)

      case call do
        1 -> {:ok, resp("snap1")}
        2 ->
          send(test_pid, {:rebuild_started, req.image_ref, self()})

          receive do
            {:release_rebuild, result} -> result
          end
      end
    end

    {builder, agent, _table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 2,
        hydrate_poll_max: 2,
        build_fun: build_fun
      )

    desc_arg = desc(%{mem_mib: 4_000})
    :ok = BaseBuilder.reconcile(builder, desc_arg)
    assert_receive {:restore_started, restore_worker}, 1_000
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000, image_ref: "imgB"}))
    assert_receive {:rebuild_started, "imgB", rebuild_worker}, 1_000
    send(restore_worker, :release_restore)
    Process.sleep(50)
    assert BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1"
    send(rebuild_worker, {:release_rebuild, {:ok, resp("snap2")}})
    assert_eventually(fn -> match?(%{"snapshotRef" => "snap2"}, latest(agent, "w")) end)

    # The fallback's start signature is stale. It must not clear the newer ledger
    # or enqueue a duplicate build for the already-recorded current signature.
    assert Agent.get(calls, & &1) == 2
  end

  test "hydrate fallback does not duplicate a build already targeting the signature" do
    test_pid = self()
    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      call = Agent.get_and_update(calls, fn n -> {n + 1, n + 1} end)

      case call do
        1 -> {:ok, resp("snap1")}
        2 ->
          send(test_pid, {:other_started, self()})

          receive do
            {:release_other, result} -> result
          end

        3 ->
          send(test_pid, {:rebuild_started, self()})

          receive do
            {:release_rebuild, result} -> result
          end
      end
    end

    {builder, _agent, _table} =
      build_then_report_base_absent(build_fun: build_fun)

    :ok = BaseBuilder.reconcile(builder, desc(%{name: "other", mem_mib: 4_000}))
    assert_receive {:other_started, other_worker}, 1_000

    # Derive the signature tuple from the same desc the builder reconciled, in
    # signature/1's field order, so a future field addition fails loudly here
    # rather than silently flipping this test into the mismatch branch.
    d = desc(%{mem_mib: 4_000})
    signature_at_start = {d.image_ref, nil, d.vcpus, d.mem_mib, d.guest_port, d.ready_path, d.init_env}
    GenServer.cast(builder, {:reconcile_force_rebuild, "w", signature_at_start})

    # A second force message while the first build is queued must honor the
    # already_targeting? guard and leave the build count unchanged.
    GenServer.cast(builder, {:reconcile_force_rebuild, "w", signature_at_start})
    Process.sleep(50)
    assert Agent.get(calls, & &1) == 2

    send(other_worker, {:release_other, {:ok, resp("snap-other")}})
    assert_receive {:rebuild_started, rebuild_worker}, 1_000
    send(rebuild_worker, {:release_rebuild, {:ok, resp("snap2")}})
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap2" end)
    assert Agent.get(calls, & &1) == 3
  end

  test "force rebuild drops only the anchor vendor's completed entry" do
    test_pid = self()
    agent = start_recorder()
    table = new_cap_table()
    {:ok, calls} = Agent.start_link(fn -> 0 end)
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn count -> {count + 1, count + 1} end) do
        1 ->
          {:ok, resp("amd-a")}

        2 ->
          {:ok, resp("intel-i")}

        3 ->
          send(test_pid, {:forced_rebuild_started, self()})

          receive do
            :finish_forced_rebuild -> {:ok, resp("intel-i2")}
          end
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{image_ref: "imgA"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-a" end)

    :ok = BaseBuilder.remove_node(builder, "node-4/amd")
    NodeCapacity.drop(table, {"node-4", "amd"})
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2, image_ref: "imgI"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "intel-i" end)

    before_force = :sys.get_state(builder).workloads["w"]
    assert Map.keys(before_force.vendor_built) |> Enum.sort() == ["amd", "intel"]

    GenServer.cast(
      builder,
      {:reconcile_force_rebuild, "w", before_force.built_signature, "node-1"}
    )

    assert_receive {:forced_rebuild_started, worker}, 1_000
    during_force = :sys.get_state(builder).workloads["w"]
    assert Map.keys(during_force.vendor_built) == ["amd"]
    assert during_force.vendor_built["amd"].ref == "amd-a"
    assert during_force.snapshot_ref == nil

    send(worker, :finish_forced_rebuild)
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "intel-i2" end)
  end

  test "hydrate fallback clears ledger fields before the building status write" do
    test_pid = self()
    {:ok, calls} = Agent.start_link(fn -> 0 end)
    restore_fun = fn :fake_channel, _req ->
      {:error, %GRPC.RPCError{status: 9, message: "not present in store"}}
    end

    build_fun = fn :fake_channel, _req ->
      call = Agent.get_and_update(calls, fn n -> {n + 1, n + 1} end)

      if call == 2 do
        send(test_pid, {:rebuild_started, self()})

        receive do
          {:release_rebuild, result} -> result
        end
      else
        {:ok, resp("snap1")}
      end
    end

    {builder, agent, _table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 2,
        hydrate_poll_max: 2,
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive {:rebuild_started, rebuild_worker}, 1_000

    assert_eventually(fn ->
      case latest(agent, "w") do
        nil -> false
        status ->
          base_built = condition(status, "BaseBuilt")

          not Map.has_key?(status, "snapshotRef") and base_built["status"] == "False" and
            base_built["reason"] == "BaseBuilding"
      end
    end)

    assert BaseBuilder.status(builder).workloads["w"].snapshot_ref == nil
    send(rebuild_worker, {:release_rebuild, {:ok, resp("snap2")}})
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap2" end)
  end

  test "restore-first falls back to BuildBase when the hydrate never lands (poll timeout)" do
    test_pid = self()

    # The restore is accepted but the base NEVER becomes READY (node died
    # mid-download, S3 flaked): the poll must time out and rebuild.
    restore_fun = fn :fake_channel, _req ->
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {builder, _agent, _table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        # A tiny poll budget so the timeout fallback is fast and deterministic.
        hydrate_poll_interval_ms: 2,
        hydrate_poll_max: 3,
        build_fun: fn :fake_channel, _req ->
          send(test_pid, :rebuilt)
          {:ok, resp("snap1")}
        end
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive :rebuilt, 1_000
  end

  test "restore-first does NOT fire on a not-yet-reported base (post-build race guard)" do
    # After a build, before the node re-reports the base READY, there is a window
    # where the workload has a recorded ref but NO node fact. A reconcile in that
    # window must NOT hydrate (it would be a spurious restore of a base that is
    # actually present); it must idempotent-no-op instead.
    test_pid = self()
    agent = start_recorder()
    table = new_cap_table()

    restore_fun = fn :fake_channel, _req ->
      send(test_pid, :restore_called)
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("snap1")} end,
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    # No base fact is ever written for "w" (the node has not re-reported). A
    # reconcile here must NOT hydrate.
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    refute_receive :restore_called, 200
  end

  # -- recorded vs fetchable (#4893 site 1) ------------------------------------

  test "#4893 site 1: a recorded-but-unexported base that vanishes rebuilds directly, never hydrating" do
    # The #4865 failure shape: the base was built and NEVER exported (no store
    # copy anywhere), then its node lost it. The recorded scalar must not be
    # read as "fetchable": no RestoreArtifact may fire, and the builder must
    # converge straight to BuildBase instead of looping on a doomed restore.
    test_pid = self()
    agent = start_recorder()
    table = new_cap_table()

    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      call = Agent.get_and_update(calls, fn n -> {n + 1, n + 1} end)

      if call == 1, do: {:ok, resp("snap1")}, else: {:ok, resp("snap2")}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    # The export never landed: the node still reports exported == false, so the
    # store_confirmed ledger holds NO evidence for snap1. Scratch loss makes the
    # node report the base affirmatively absent.
    refute Map.has_key?(:sys.get_state(builder).workloads["w"].store_confirmed, "amd")
    put_base_fact(table, "node-4", "big", "w", "", :BASE_BUILD_STATE_NONE, false)

    capture_log(fn ->
      :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    end)

    # No RestoreArtifact was ever attempted: absence of store evidence refuses
    # the hydrate outright.
    refute_receive {:restore_called, _}, 200

    # And the workload CONVERGES: BuildBase runs immediately, without a hydrate
    # round trip through FAILED_PRECONDITION first.
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap2" end)
    assert Agent.get(calls, & &1) == 2
  end

  test "#4893 sites 1+2: a base still held on another node is exported first, then hydrates once confirmed" do
    # Ref exists locally (on a NON-anchor node) but is gone from the store: the
    # doomed hydrate is refused AND the pointless rebuild is skipped, because
    # the reconcile export fan-out reaches the node actually holding the bytes.
    # Once that upload lands (exported == true), the same recorded ref becomes
    # legitimately hydratable: the vice-versa half of the failure mode.
    test_pid = self()
    table = new_cap_table()

    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    connect_fun = fn addr ->
      send(test_pid, {:dialed, addr})
      {:ok, :fake_channel}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a4"}, %{id: "node-5/hold", address: "a5"}],
        capacity_table: table,
        connect_fun: connect_fun,
        build_fun: fn :fake_channel, _req -> {:ok, resp("snap1")} end,
        export_fun: fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
          send(test_pid, {:exported, ref.ref})
          {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
        end,
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    put_brick(table, "node-5", "hold", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    # Drain the dial-home-window export (no node advertises the workload yet).
    assert_receive {:dialed, "a4"}, 1_000
    assert_receive {:exported, "snap1"}, 1_000

    # node-4 (the pin) loses its scratch; node-5 still holds the SAME current
    # ref locally, unexported.
    put_base_fact(table, "node-4", "big", "w", "", :BASE_BUILD_STATE_NONE, false)
    put_base_fact(table, "node-5", "hold", "w", "snap1", :BASE_BUILD_STATE_READY, false)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))

    # Neither a hydrate (nothing confirmed in the store) nor a rebuild (the
    # artifact survives next door) may fire.
    refute_receive {:restore_called, _}, 200

    # The reconcile export fan-out dials the HOLDING node and ships the copy.
    send(builder, :export_reconcile)
    assert_receive {:dialed, "a5"}, 1_000
    assert_receive {:exported, "snap1"}, 1_000

    # The upload lands: the confirmation arms restore-first for this exact ref.
    put_base_fact(table, "node-5", "hold", "w", "snap1", :BASE_BUILD_STATE_READY, true)
    send(builder, :export_reconcile)
    assert_eventually(fn ->
      :sys.get_state(builder).workloads["w"].store_confirmed["amd"].ref == "snap1"
    end)

    # Now node-5 loses it too: the only remaining source IS the store, so the
    # next wake/reconcile hydrates.
    put_base_fact(table, "node-5", "hold", "w", "", :BASE_BUILD_STATE_NONE, false)
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive {:restore_called, "snap1"}, 1_000
  end

  test "the export reconcile never ships a superseded ref a lagging node still reports" do
    # The widened sweep stays bounded: a node lagging a turnover reports the OLD
    # ref; that ref has left the CP's current set (scalar advanced, per-vendor
    # record replaced), so PR-1's "never ship superseded refs" bound still holds.
    test_pid = self()
    table = new_cap_table()

    build_fun = fn :fake_channel, req ->
      if req.image_ref == "imgA", do: {:ok, resp("r1")}, else: {:ok, resp("r2")}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a4"}, %{id: "node-5/lag", address: "a5"}],
        capacity_table: table,
        build_fun: build_fun,
        export_fun: fn :fake_channel, %Embervm.Node.V1.ExportArtifactRequest{artifact: ref} ->
          send(test_pid, {:exported, ref.ref})
          {:ok, %Embervm.Node.V1.ExportArtifactResponse{bytes_moved: 0, skipped: true, generation: 0}}
        end,
        export_reconcile_interval_ms: 0
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    put_brick(table, "node-5", "lag", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "r1" end)
    assert_receive {:exported, "r1"}, 1_000

    # Spec change: a real rebuild turns the base over to r2 on the pin node.
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000, image_ref: "imgB"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "r2" end)
    assert_receive {:exported, "r2"}, 1_000

    # node-5 lags the turnover: it still reports r1 as its READY current base,
    # unexported. node-4 reports the new r2 unexported.
    put_base_fact(table, "node-4", "big", "w", "r2", :BASE_BUILD_STATE_READY, false)
    put_base_fact(table, "node-5", "lag", "w", "r1", :BASE_BUILD_STATE_READY, false)

    send(builder, :export_reconcile)
    assert_receive {:exported, "r2"}, 1_000
    refute_receive {:exported, "r1"}, 200
  end

  test "a successful remote eviction drops the matching store-fetchability ledger entry" do
    agent = start_recorder()
    table = new_cap_table()

    builder =
      start_builder(
        nodes: [%{id: "node-4/big", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("snap1")} end
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    put_base_fact(table, "node-4", "big", "w", "snap1", :BASE_BUILD_STATE_READY, true)
    send(builder, :export_reconcile)
    assert_eventually(fn ->
      :sys.get_state(builder).workloads["w"].store_confirmed["amd"].ref == "snap1"
    end)

    # The object leaves the bucket: the ledger must stop vouching for it, or a
    # later hydrate would aim at a deleted object.
    send(builder, {:store_evicted, "w", "amd", "snap1"})
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].store_confirmed == %{} end)
  end

  # Seed one node's per-workload base fact (snapshot_ref, base_state, exported)
  # into an existing brick capacity row so the export reconcile can read it.
  # Merges into the row put_brick/4 already wrote, keyed the same way.

  defp put_base_fact(table, node_id, pod_uid, workload, ref, base_state, exported) do
    existing =
      case :ets.lookup(table, {node_id, pod_uid}) do
        [{_key, facts}] -> facts
        [] -> %{node_id: node_id, configured_id: node_id, instance_id: "#{node_id}/#{pod_uid}"}
      end

    workloads =
      Map.put(Map.get(existing, :workloads, %{}), workload, %{
        free_primed_slots: 0,
        snapshot_ref: ref,
        serving_image_ref: "",
        base_state: base_state,
        exported: exported,
        primed_vm_ids: []
      })

    NodeCapacity.put(table, {node_id, pod_uid}, Map.put(existing, :workloads, workloads))
  end

  # -- per-vendor base refs in status (#4061) ---------------------------------

  # A capacity fact carrying the node's CPU vendor alongside its OWN reported base
  # ref for `workload`. pod_uid nil keeps instance_id the bare node name, which is
  # what find_capacity_fact/2 matches for the builder's placement node ("node-4").
  defp put_vendor_fact(table, node_id, pod_uid, workload, ref, vendor) do
    instance_id = if pod_uid in [nil, ""], do: node_id, else: "#{node_id}/#{pod_uid}"

    NodeCapacity.put(table, {node_id, pod_uid || "ds"}, %{
      node_id: node_id,
      configured_id: node_id,
      instance_id: instance_id,
      cpu_vendor: vendor,
      workloads: %{
        workload => %{
          snapshot_ref: ref,
          base_state: :BASE_BUILD_STATE_READY,
          exported: true
        }
      },
      local_bases: [],
      updated_at: 0
    })
  end

  test "a successful build records its vendor, signature, ref, and digest" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: " amd ")

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("snap-amd", "sha256:amd")} end
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap-amd" end)

    workload = :sys.get_state(builder).workloads["w"]

    assert workload.vendor_built == %{
             "amd" => %{
               signature: workload.built_signature,
               ref: "snap-amd",
               digest: "sha256:amd"
             }
           }

    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))
    assert :sys.get_state(builder).workloads["w"].vendor_built == workload.vendor_built
  end

  test "a build without a capacity fact stays pending" do
    agent = start_recorder()
    table = new_cap_table()
    test_pid = self()

    build_fun = fn :fake_channel, _req ->
      send(test_pid, :unexpected_factless_build)
      {:ok, resp("unexpected")}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-9/brick", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    refute_receive :unexpected_factless_build, 100

    workload = :sys.get_state(builder).workloads["w"]
    assert workload.node_id == nil
    assert workload.vendor_built == %{}
    assert workload.snapshot_ref == nil
    assert workload.retry_timer == nil
    assert %{"status" => "Unknown", "reason" => "NoNodeAvailable"} =
             condition(latest(agent, "w"), "BaseBuilt")
  end

  test "a build with a blank CPU vendor records no vendor" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "blank", cpu_vendor: "   ")

    builder =
      start_builder(
        nodes: [%{id: "node-4/blank", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("snap1", "sha256:one")} end
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)

    workload = :sys.get_state(builder).workloads["w"]
    assert workload.vendor_built == %{}
    assert workload.snapshot_ref == "snap1"
    assert workload.snapshot_digest == "sha256:one"
    assert workload.built_signature != nil
  end

  test "a blank-vendor scalar base is superseded when that node later reports amd" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "brick", cpu_vendor: "   ")

    build_fun = fn :fake_channel, req ->
      case req.image_ref do
        "imgA" -> {:ok, resp("blank-a")}
        "imgB" -> {:ok, resp("amd-b")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/brick", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{image_ref: "imgA"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "blank-a" end)
    assert :sys.get_state(builder).workloads["w"].scalar_vendor == ""

    put_brick(table, "node-4", "brick", cpu_vendor: "amd")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2, image_ref: "imgB"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-b" end)

    workload = :sys.get_state(builder).workloads["w"]
    assert workload.scalar_vendor == "amd"
    assert workload.superseded_refs == ["blank-a"]
    assert Map.has_key?(workload.base_refs, "blank-a")
  end

  test "an intel build does not supersede the current amd scalar base" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "brick", cpu_vendor: "amd")

    build_fun = fn :fake_channel, req ->
      case req.image_ref do
        "imgA" -> {:ok, resp("amd-a")}
        "imgI" -> {:ok, resp("intel-i")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/brick", address: "a"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{image_ref: "imgA"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-a" end)

    put_brick(table, "node-4", "brick", cpu_vendor: "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2, image_ref: "imgI"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "intel-i" end)

    workload = :sys.get_state(builder).workloads["w"]
    assert workload.scalar_vendor == "intel"
    assert workload.superseded_refs == []
    assert workload.base_refs == %{}
  end

  test "turnover supersedes only the previous ref for the build vendor" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    build_fun = fn :fake_channel, req ->
      case req.image_ref do
        "imgA" -> {:ok, resp("amd-a")}
        "imgI" -> {:ok, resp("intel-i")}
        "imgA2" -> {:ok, resp("amd-a2")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{image_ref: "imgA"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-a" end)

    :ok = BaseBuilder.remove_node(builder, "node-4/amd")
    NodeCapacity.drop(table, {"node-4", "amd"})
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2, image_ref: "imgI"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "intel-i" end)

    after_intel = :sys.get_state(builder).workloads["w"]
    assert after_intel.superseded_refs == []
    assert after_intel.base_refs == %{}
    assert after_intel.vendor_built["amd"].ref == "amd-a"
    assert after_intel.vendor_built["intel"].ref == "intel-i"

    :ok = BaseBuilder.remove_node(builder, "node-1/intel")
    NodeCapacity.drop(table, {"node-1", "intel"})
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")
    :ok = BaseBuilder.add_node(builder, "node-4/amd", "amd")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 3, image_ref: "imgA2"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-a2" end)

    after_amd = :sys.get_state(builder).workloads["w"]
    assert after_amd.superseded_refs == ["amd-a"]
    assert Map.has_key?(after_amd.base_refs, "amd-a")
    refute Map.has_key?(after_amd.base_refs, "intel-i")
  end

  test "coverage reports a registered vendor with no build while Ready stays true" do
    agent = start_recorder()
    table = new_cap_table()
    {:ok, builds} = Agent.start_link(fn -> 0 end)

    put_brick(table, "node-4", "amd",
      cpu_vendor: "amd",
      size_class: "16gi",
      mem_budget: 16_384
    )

    put_brick(table, "node-1", "intel",
      cpu_vendor: "intel",
      size_class: "8gi",
      mem_budget: 8_192
    )

    build_fun = fn :fake_channel, _req ->
      Agent.update(builds, &(&1 + 1))
      {:ok, resp("amd-base")}
    end

    builder =
      start_builder(
        nodes: [
          %{id: "node-4/amd", address: "amd"},
          %{id: "node-1/intel", address: "intel"}
        ],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    status = latest(agent, "w")
    coverage = condition(status, "BaseVendorCoverage")
    assert %{"status" => "False", "reason" => "VendorCoverageIncomplete"} = coverage
    assert coverage["message"] =~ "missing vendors: intel"
    assert coverage["message"] =~ "stale vendors: none"
    assert %{"status" => "True"} = condition(status, "Ready")

    built_and_later =
      recorded(agent)
      |> Enum.drop_while(fn {_namespace, _name, status_map} -> status_map["snapshotRef"] != "amd-base" end)

    assert built_and_later != []

    for {_namespace, "w", status_map} <- built_and_later do
      assert %{"status" => "True"} = condition(status_map, "Ready")
    end

    assert Agent.get(builds, & &1) == 1
  end

  test "a registered instance without a capacity fact is not a missing vendor" do
    agent = start_recorder()
    table = new_cap_table()
    {:ok, builds} = Agent.start_link(fn -> 0 end)

    put_brick(table, "node-4", "amd",
      cpu_vendor: "amd",
      size_class: "",
      mem_budget: 16_384
    )

    build_fun = fn :fake_channel, _req ->
      Agent.update(builds, &(&1 + 1))
      {:ok, resp("amd-base")}
    end

    builder =
      start_builder(
        nodes: [
          %{id: "node-4/amd", address: "amd"},
          %{id: "node-1/intel", address: "intel"}
        ],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    assert %{"status" => "True"} = condition(latest(agent, "w"), "BaseVendorCoverage")

    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))
    Process.sleep(50)
    assert Agent.get(builds, & &1) == 1
  end

  test "coverage reports an old vendor signature as stale rather than missing" do
    agent = start_recorder()
    test_pid = self()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    build_fun = fn :fake_channel, req ->
      case req.image_ref do
        "imgA" ->
          {:ok, resp("amd-v1")}

        "imgB" ->
          send(test_pid, {:building_new_signature, self()})

          receive do
            :finish -> {:ok, resp("amd-v2")}
          end
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{image_ref: "imgA"}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-v1" end)

    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2, image_ref: "imgB"}))
    assert_receive {:building_new_signature, worker}, 1_000

    coverage = condition(latest(agent, "w"), "BaseVendorCoverage")
    assert %{"status" => "False"} = coverage
    assert coverage["message"] =~ "missing vendors: none"
    assert coverage["message"] =~ "stale vendors: amd"
    assert %{"status" => "True"} = condition(latest(agent, "w"), "Ready")

    send(worker, :finish)
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-v2" end)
  end

  test "coverage calls an observed base without vendor history unverified, not missing" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "amd-base", "amd")

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("amd-base")} end,
        export_reconcile_interval_ms: 0
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    :sys.replace_state(builder, fn state ->
      put_in(state.workloads["w"].vendor_built, %{})
    end)

    send(builder, :export_reconcile)

    assert_eventually(fn ->
      coverage = condition(latest(agent, "w"), "BaseVendorCoverage")

      coverage["status"] == "False" and coverage["message"] =~ "missing vendors: none" and
        coverage["message"] =~ "unverified vendors" and
        coverage["message"] =~ "cannot vouch for signature" and coverage["message"] =~ "amd"
    end)
  end

  test "export reconcile republishes full conditions once when a vendor fact appears" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("amd-base")} end,
        export_reconcile_interval_ms: 0
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)
    assert %{"status" => "True"} = condition(latest(agent, "w"), "BaseVendorCoverage")
    Agent.update(agent, fn _calls -> [] end)

    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)

    assert [{"embervm", "w", status}] = recorded(agent)
    assert length(status["conditions"]) == 3
    assert %{"status" => "True", "reason" => "BaseReady"} = condition(status, "Ready")
    assert %{"status" => "True", "reason" => "BaseBuilt"} = condition(status, "BaseBuilt")

    coverage = condition(status, "BaseVendorCoverage")
    assert %{"status" => "False"} = coverage
    assert coverage["message"] =~ "missing vendors: intel"

    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)
    assert [{"embervm", "w", ^status}] = recorded(agent)
  end

  test "single-vendor convergence remains one build and reports full coverage" do
    agent = start_recorder()
    table = new_cap_table()
    {:ok, builds} = Agent.start_link(fn -> 0 end)
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    build_fun = fn :fake_channel, _req ->
      Agent.update(builds, &(&1 + 1))
      {:ok, resp("amd-base")}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)
    assert %{"status" => "True"} = condition(latest(agent, "w"), "BaseVendorCoverage")

    :ok = BaseBuilder.reconcile(builder, desc())
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))
    Process.sleep(50)

    assert Agent.get(builds, & &1) == 1
    assert %{"status" => "True"} = condition(latest(agent, "w"), "BaseVendorCoverage")
  end

  # -- per-vendor repair enqueue (arms coverage bookkeeping into build decisions) --
  #
  # PR #4993 added vendor_built/fleet_vendors/vendor_needs_build? and the
  # BaseVendorCoverage condition, but reconcile_desc/2 and retry_workload/2 still
  # asked only the SCALAR built_signature/snapshot_ref "is this built?", which
  # means "some vendor, somewhere" once and for all short-circuits every later
  # reconcile even when a second fleet vendor has nothing. These tests exercise
  # the repair-enqueue that closes that gap.

  test "reconcile repair-enqueues a vendor missing a base without flipping status to building" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")
    test_pid = self()

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn c -> {c, c + 1} end) do
        0 ->
          {:ok, resp("amd-base")}

        1 ->
          send(test_pid, {:building_intel, self()})

          receive do
            :finish -> {:ok, resp("intel-base")}
          end
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    before_repair = latest(agent, "w")
    assert %{"status" => "True", "reason" => "BaseBuilt"} = condition(before_repair, "BaseBuilt")
    assert %{"status" => "True"} = condition(before_repair, "Ready")

    # A second fleet vendor appears. Same spec (only generation moved), so the
    # scalar guard (built_signature == signature and snapshot_ref != nil) holds;
    # that used to be a blanket no-op.
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))

    assert_receive {:building_intel, worker}, 1_000

    # While intel's repair build is in flight, the workload already serves on
    # amd: no status write flips BaseBuilt to building, and the scalar fields
    # are untouched (only the completed repair build's apply_result may move
    # them, which is a separate, already-documented concern -- see the
    # should_hydrate?/3 regression-guard tests below).
    assert latest(agent, "w") == before_repair

    mid_repair = :sys.get_state(builder).workloads["w"]
    assert mid_repair.scalar_vendor == "amd"
    assert mid_repair.snapshot_ref == "amd-base"

    # The assertion above (latest(agent, "w") == before_repair) only proves the
    # repair enqueue itself writes nothing; re-reading Ready off before_repair
    # (captured BEFORE the repair even started) would pass unconditionally,
    # even with the repair-enqueue feature reverted entirely. Force a REAL
    # status write while the intel repair build is still in flight: intel
    # joining the fleet flips BaseVendorCoverage from True to False, and the
    # export reconcile republishes on exactly that kind of coverage change
    # (refresh_snapshot_refs/1). This is the write that actually exercises the
    # invariant that matters: Ready must stay True even while a status write
    # legitimately happens during partial coverage.
    send(builder, :export_reconcile)

    assert_eventually(fn ->
      match?(%{"status" => "False"}, condition(latest(agent, "w"), "BaseVendorCoverage"))
    end)

    mid_repair_write = latest(agent, "w")
    assert mid_repair_write != before_repair
    assert %{"status" => "True"} = condition(mid_repair_write, "Ready")
    assert %{"status" => "True", "reason" => "BaseBuilt"} = condition(mid_repair_write, "BaseBuilt")

    send(worker, :finish)

    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].vendor_built["intel"] end)
    intel_built = BaseBuilder.status(builder).workloads["w"].vendor_built["intel"]
    assert intel_built.ref == "intel-base"
  end

  test "reconcile is a no-op once every fleet vendor has a current base" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")

    {:ok, builds} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      Agent.update(builds, &(&1 + 1))
      {:ok, resp("amd-base")}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].vendor_built["intel"] end)
    assert Agent.get(builds, & &1) == 2

    # Both vendors now have a current record: a further reconcile enqueues
    # nothing, exactly the pre-existing single-vendor no-op property extended
    # to a fully-covered fleet.
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 3}))
    Process.sleep(50)

    assert Agent.get(builds, & &1) == 2
    assert %{"status" => "True"} = condition(latest(agent, "w"), "BaseVendorCoverage")
  end

  test "an unverified vendor (observed ref, no CP record) is repair-enqueued and converges on an already-built result" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn c -> {c, c + 1} end) do
        0 -> {:ok, resp("amd-base")}
        _ -> {:ok, resp("intel-observed")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    # intel already has a base ON DISK (the node observed and reported it), but
    # the CP holds no vendor_built record for it: unverified, not missing.
    # vendor_needs_build?/2 treats the two identically (see the comment on
    # vendors_needing_build/2 for why: vendor_built is in-memory-only and never
    # rehydrated, so skipping unverified would leave coverage broken forever
    # after every CP restart).
    #
    # A CLASSED brick (put_brick), not a bare put_vendor_fact: put_vendor_fact
    # writes no size_class/mem_budget_mib, so instance_build_facts/2 treats
    # node-1/intel as a build_rank {1, 0} WILDCARD, which outranks amd's
    # classed {0, 8192} and moves the workload's OWN pin onto node-1/intel via
    # keep_or_replace/3 -- at which point build_instance_of_vendor/3 just
    # returns w.node_id and the cross-instance repair path this test claims to
    # exercise never actually runs. put_brick keeps node-1/intel classed like
    # amd so the pin stays on node-4/amd and the repair genuinely targets a
    # DIFFERENT instance than the one built on.
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    put_base_fact(table, "node-1", "intel", "w", "intel-observed", :BASE_BUILD_STATE_READY, true)
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))

    # noded's AlreadyBuilt short-circuit still returns {:ok, %BuildBaseResponse{}}
    # carrying the pre-existing ref (server.go answers off s.bases.get(baseKey)
    # before beginBuild/driveBuild), which is what build_fun models by returning
    # the SAME "intel-observed" ref. apply_result/4 writes vendor_built["intel"]
    # from that response exactly as it would from a fresh build.
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].vendor_built["intel"] end)
    intel_built = BaseBuilder.status(builder).workloads["w"].vendor_built["intel"]
    assert intel_built.ref == "intel-observed"
    assert Agent.get(calls, & &1) == 2
    assert %{"status" => "True"} = condition(latest(agent, "w"), "BaseVendorCoverage")

    # Convergence: the CP now vouches for intel too, so a further reconcile
    # enqueues nothing more.
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 3}))
    Process.sleep(50)
    assert Agent.get(calls, & &1) == 2
  end

  test "retry_workload enqueues a missing vendor instead of treating the scalar guard as nothing to do" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn c -> {c, c + 1} end) do
        0 -> {:ok, resp("amd-base")}
        _ -> {:ok, resp("intel-base")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")

    # Directly drive the backoff-retry path (handle_info({:retry, name}, ...)).
    # The scalar guard holds (amd's build is current and recorded), which is
    # exactly the condition retry_workload/2 used to treat as "the desired base
    # got built by some other path meanwhile; nothing to do". It must now
    # repair-enqueue intel instead of no-op'ing.
    send(builder, {:retry, "w"})

    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].vendor_built["intel"] end)
    assert Agent.get(calls, & &1) == 2
  end

  test "should_hydrate? regression guard: a pinned instance whose vendor differs from scalar_vendor never hydrates the wrong ref" do
    test_pid = self()
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn c -> {c, c + 1} end) do
        0 -> {:ok, resp("amd-snap")}
        _ -> {:ok, resp("intel-snap")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-snap" end)
    assert :sys.get_state(builder).workloads["w"].scalar_vendor == "amd"

    # The amd instance vanishes; an intel instance takes over the pin, and it
    # AFFIRMATIVELY reports no base for "w" -- should_hydrate?/3's ordinary
    # trigger. But apply_result/4 stamps scalar fields from whichever vendor
    # built LAST, so the recorded scalar ref ("amd-snap") belongs to amd, not to
    # this intel instance: hydrating it here would restore the wrong content
    # onto the wrong vendor.
    :ok = BaseBuilder.remove_node(builder, "node-4/amd")
    NodeCapacity.drop(table, {"node-4", "amd"})
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    put_base_fact(table, "node-1", "intel", "w", "", :BASE_BUILD_STATE_NONE, false)
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")

    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))

    refute_receive {:restore_called, _}, 200

    # Falls through to the scalar-guard repair path instead: intel genuinely has
    # no base of its own, so it gets a real BuildBase, never a wrong-vendor
    # RestoreArtifact.
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].vendor_built["intel"] end)
    assert Agent.get(calls, & &1) == 2
  end

  test "regression: restore-first still restores the pin vendor's own ref after a cross-vendor repair build" do
    # The exact steady state #4893 site 3 creates: the workload is pinned to an
    # amd instance, and a LATER intel repair build (per-vendor enqueue is now
    # routine) completes. Since adoption-time vendor scoping, that repair no
    # longer moves the scalar snapshot_ref/scalar_vendor at all -- the scalar
    # keeps naming the PIN vendor's ref -- and the intel result lives only in
    # vendor_built. When the amd instance AFFIRMATIVELY reports its own base
    # absent, should_hydrate? must still fire and restore amd's OWN recorded
    # ref (vendor_built["amd"]), which is now also what the scalar names.
    test_pid = self()
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn c -> {c, c + 1} end) do
        0 -> {:ok, resp("amd-base")}
        _ -> {:ok, resp("intel-base")}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    # The upload lands on node-4: confirm amd's base as fetchable from the
    # store before the intel repair build joins the fleet.
    put_base_fact(table, "node-4", "amd", "w", "amd-base", :BASE_BUILD_STATE_READY, true)
    send(builder, :export_reconcile)
    assert_eventually(fn ->
      :sys.get_state(builder).workloads["w"].store_confirmed["amd"].ref == "amd-base"
    end)

    # A second fleet vendor (intel) appears and its repair build completes.
    # Both bricks are classed identically, so build_rank ties and the amd
    # instance -- registered first -- keeps its sticky pin per keep_or_replace/3.
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))

    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].vendor_built["intel"] end)
    workload = :sys.get_state(builder).workloads["w"]

    # ADOPTION-time vendor scoping (#4893 site 3): the intel repair build does
    # NOT re-derive the headline ref from build order. The scalar still names
    # the pin vendor's (amd's) base; intel's result is recorded per vendor.
    assert workload.scalar_vendor == "amd"
    assert workload.snapshot_ref == "amd-base"
    assert workload.vendor_built["intel"].ref == "intel-base"
    assert workload.node_id == "node-4/amd"

    # amd's own instance now AFFIRMATIVELY reports its base absent again
    # (scratch loss, cold-start). Restore-first anchors on the per-vendor
    # record plus the confirmed store copy, not on the scalar comparison this
    # regression used to guard against.
    put_base_fact(table, "node-4", "amd", "w", "", :BASE_BUILD_STATE_NONE, false)
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 3}))

    assert_receive {:restore_called, "amd-base"}, 1_000

    # No new BuildBase was dispatched (a hydrate, not a rebuild, is what fired).
    assert Agent.get(calls, & &1) == 2
  end

  test "repair enqueue prefers the same-vendor instance whose node already reports a base, not the highest-ranked one" do
    # The actual prod shape: several instances of the SAME vendor (three intel
    # bricks live). Two identically-classed intel bricks tie on build_rank/1,
    # so the OLD tiebreak (Enum.max_by/2 keeps the first maximal element in
    # state.node_ids order, i.e. dial-home registration order) would pick
    # whichever brick registered first -- which reshuffles on every noded pod
    # roll and, on the live fleet, could land a real cold-boot bake on a brick
    # that holds none of the workload's bases while a brick that already has
    # one sits idle. The fix ranks the instance that already reports a base
    # for this workload first.
    agent = start_recorder()
    table = new_cap_table()
    test_pid = self()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")
    put_brick(table, "node-1", "intel-a", cpu_vendor: "intel")
    put_brick(table, "node-2", "intel-b", cpu_vendor: "intel")

    # node-2/intel-b already reports a READY base for "w"; node-1/intel-a
    # (registered FIRST, below) reports nothing at all.
    put_base_fact(table, "node-2", "intel-b", "w", "intel-existing", :BASE_BUILD_STATE_READY, true)

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn c -> {c, c + 1} end) do
        0 ->
          {:ok, resp("amd-base")}

        _ ->
          send(test_pid, {:repair_building, self()})

          receive do
            :finish -> {:ok, resp("intel-existing")}
          end
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    :ok = BaseBuilder.add_node(builder, "node-1/intel-a", "intel-a")
    :ok = BaseBuilder.add_node(builder, "node-2/intel-b", "intel-b")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))

    assert_receive {:repair_building, worker}, 1_000

    status = BaseBuilder.status(builder)
    assert status.nodes["node-2/intel-b"].building == "w"
    assert status.nodes["node-1/intel-a"].building == nil
    refute "w" in status.nodes["node-1/intel-a"].queued

    send(worker, :finish)

    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].vendor_built["intel"] end)
    assert Agent.get(calls, & &1) == 2
  end

  test "a fleet vendor whose only instance cannot build this workload's mem need never crashes or enqueues" do
    # build_instance_of_vendor/3's [] -> nil clause. fleet_vendors/2 and
    # build_instance_of_vendor/3 both derive from eligible_build_instances/2
    # with the SAME need_mib, so an ineligible-only vendor is excluded from
    # vendors_needing_build/2 before build_instance_of_vendor/3 would ever be
    # asked about it -- which is exactly the invariant the PR's own comment on
    # build_instance_of_vendor/3 claims ("a vendor that vendors_needing_build/2
    # returns therefore always resolves here"). This test is the safety net
    # for that invariant, and the coverage the PR's rationale for not reusing
    # node_of_vendor/3 was otherwise missing: an ineligible-only vendor must
    # never crash the builder or leave a phantom enqueue.
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000, cpu_vendor: "amd")
    put_brick(table, "node-1", "intel", size_class: "2gi", mem_budget: 2_048, mem_headroom: 2_000, cpu_vendor: "intel")

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      Agent.update(calls, &(&1 + 1))
      {:ok, resp("amd-base")}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 8_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 8_000, generation: 2}))
    Process.sleep(50)

    assert Process.alive?(builder)
    assert BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base"
    # Only the original amd build ever ran: intel never became eligible, so it
    # never entered vendors_needing_build/2 and no BuildBase was dispatched.
    assert Agent.get(calls, & &1) == 1
    assert %{"status" => "True"} = condition(latest(agent, "w"), "BaseVendorCoverage")
  end

  test "a failed repair build does not flip BaseBuilt or Ready to False for a workload that still serves" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")
    test_pid = self()

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      case Agent.get_and_update(calls, fn c -> {c, c + 1} end) do
        0 ->
          {:ok, resp("amd-base")}

        _ ->
          send(test_pid, :repair_failed)
          # A real observed failure class (issue text): the guest never
          # reached readiness on the repair instance.
          {:error,
           %GRPC.RPCError{
             status: 4,
             message: "guest readiness: vsockhttp: timed out waiting for guest ready at /shim/ready"
           }}
      end
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)
    before_repair = latest(agent, "w")

    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))

    assert_receive :repair_failed, 1_000
    # Let the failed build's apply_result run.
    Process.sleep(50)

    # The workload still serves on amd, so the repair failure must not write
    # ANY new status: not :building (never did), and now not :failed either.
    # A build the caller never observed as :building must not be observable
    # as failed, or a workload that genuinely serves would park at
    # BaseBuilt=False/Ready=False indefinitely for a build nobody asked for.
    assert latest(agent, "w") == before_repair
    assert %{"status" => "True", "reason" => "BaseBuilt"} = condition(before_repair, "BaseBuilt")
    assert %{"status" => "True"} = condition(before_repair, "Ready")

    # The backoff retry is still armed as normal, so the repair keeps trying.
    assert :sys.get_state(builder).workloads["w"].backoff_ms != nil
  end

  test "a repair enqueue does not fire for a workload with a hydrate already in flight" do
    agent = start_recorder()
    table = new_cap_table()
    put_brick(table, "node-4", "amd", cpu_vendor: "amd")

    {:ok, calls} = Agent.start_link(fn -> 0 end)

    build_fun = fn :fake_channel, _req ->
      Agent.update(calls, &(&1 + 1))
      {:ok, resp("amd-base")}
    end

    # Accepted, but never becomes READY within this test: the hydrate worker
    # stays in state.hydrating polling, which is exactly the in-flight window
    # enqueue_vendor_repairs/3 must defer to.
    restore_fun = fn :fake_channel, _req ->
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    builder =
      start_builder(
        nodes: [%{id: "node-4/amd", address: "amd"}],
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 50,
        hydrate_poll_max: 40
      )

    :ok = BaseBuilder.reconcile(builder, desc())
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "amd-base" end)

    # The upload lands (exported == true), one reconcile tick confirms the
    # store copy in the ledger: since #4893, restore-first needs that positive
    # fetchability evidence before it will fire.
    put_base_fact(table, "node-4", "amd", "w", "amd-base", :BASE_BUILD_STATE_READY, true)
    send(builder, :export_reconcile)
    assert_eventually(fn ->
      :sys.get_state(builder).workloads["w"].store_confirmed["amd"].ref == "amd-base"
    end)

    # amd's own instance affirmatively reports its base absent: restore-first
    # fires and marks "w" hydrating.
    put_base_fact(table, "node-4", "amd", "w", "", :BASE_BUILD_STATE_NONE, false)
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 2}))
    assert_eventually(fn -> MapSet.member?(:sys.get_state(builder).hydrating, "w") end)

    # A second fleet vendor (intel) shows up and genuinely needs a build. The
    # 60s WorkloadWatcher resync (modeled here by a further reconcile with the
    # SAME desc) must NOT repair-enqueue it while "w" is still hydrating: the
    # in-flight RestoreArtifact and a fresh BuildBase must never race for the
    # same base key.
    put_brick(table, "node-1", "intel", cpu_vendor: "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "intel")
    :ok = BaseBuilder.reconcile(builder, desc(%{generation: 3}))

    Process.sleep(80)

    assert MapSet.member?(:sys.get_state(builder).hydrating, "w")
    assert Agent.get(calls, & &1) == 1
    refute :sys.get_state(builder).workloads["w"].vendor_built["intel"]
  end

  test "status carries one entry per CPU vendor the fleet reports a base on" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")
    put_vendor_fact(table, "node-1", "ds", "w", "w__intel", "intel")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    build_current(builder, agent, "w__amd")

    status = latest(agent, "w")

    # The single builder-advanced handle is unchanged (additive field).
    assert status["snapshotRef"] == "w__amd"
    assert status["snapshotRefs"] == %{"amd" => ["w__amd"], "intel" => ["w__intel"]}
  end

  test "a vendor mid-rollout carries BOTH of its refs, sorted, not an arbitrary winner" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")
    # node-1 rebuilt; node-2 still runs the previous runtime image. Same vendor,
    # two live refs. Collapsing to one would be order-dependent and could report
    # the superseded ref as current.
    put_vendor_fact(table, "node-1", "ds", "w", "w__intel_new", "intel")
    put_vendor_fact(table, "node-2", "ds", "w", "w__intel_old", "intel")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    build_current(builder, agent, "w__amd")

    assert latest(agent, "w")["snapshotRefs"] == %{
             "amd" => ["w__amd"],
             "intel" => ["w__intel_new", "w__intel_old"]
           }
  end

  test "a node reporting no CPU vendor is left out rather than keyed under an empty vendor" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")
    # A daemon that predates vendor reporting: no key, not a wrong key.
    put_vendor_fact(table, "node-1", "ds", "w", "w__unknown", "")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    build_current(builder, agent, "w__amd")

    assert latest(agent, "w")["snapshotRefs"] == %{"amd" => ["w__amd"]}
  end

  test "no vendor known anywhere omits the key rather than writing an empty map" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun
      )

    build_current(builder, agent, "w__amd")

    status = latest(agent, "w")

    # Absent, NOT %{}: a CP that has heard no vendors must not clobber a good
    # value already on the CR with an empty map.
    refute Map.has_key?(status, "snapshotRefs")
    assert status["snapshotRef"] == "w__amd"
  end

  test "remote retention lists per vendor and spares the vendor's current ref" do
    agent = start_recorder()
    table = new_cap_table()
    test_pid = self()

    put_vendor_fact(table, "node-4", nil, "w", "w__amd_cur", "amd")
    put_vendor_fact(table, "node-1", "ds", "w", "w__intel_cur", "intel")

    # The store still holds each vendor's current ref plus one superseded ref.
    list_fun = fn _channel, workload, vendor ->
      send(test_pid, {:listed, workload, vendor})

      entries =
        case vendor do
          "amd" ->
            [%{ref: "w__amd_cur", size_bytes: 10}, %{ref: "w__amd_old", size_bytes: 20}]

          "intel" ->
            [%{ref: "w__intel_cur", size_bytes: 30}, %{ref: "w__intel_old", size_bytes: 40}]
        end

      {:ok, entries, false}
    end

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd_cur")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        list_fun: list_fun,
        # Both vendors' instances need a dialable address: the sweep asks a node
        # OF THAT VENDOR for the store listing, and the intel node is not the
        # build's placement node.
        nodes: [@node, %{id: "node-1/ds", address: "node-1:9090"}]
      )

    build_current(builder, agent, "w__amd_cur")

    # Drive one sweep. It must consult EACH vendor exactly once (the store object
    # is shared across a vendor's nodes, so per-node iteration would duplicate).
    _ = BaseBuilder.retention_sweep_now(builder)

    assert_receive {:listed, "w", "amd"}, 1_000
    assert_receive {:listed, "w", "intel"}, 1_000

    # Dry run: nothing is evicted in this build, so a second sweep still sees the
    # same store and simply logs again.
    _ = BaseBuilder.retention_sweep_now(builder)
    assert_receive {:listed, "w", "amd"}, 1_000
  end

  test "remote retention with the gate ON evicts superseded store bases per vendor" do
    agent = start_recorder()
    table = new_cap_table()
    test_pid = self()

    put_vendor_fact(table, "node-4", nil, "w", "w__amd_cur", "amd")
    put_vendor_fact(table, "node-1", "ds", "w", "w__intel_cur", "intel")

    list_fun = fn _channel, _workload, vendor ->
      entries =
        case vendor do
          "amd" -> [%{ref: "w__amd_cur", size_bytes: 10}, %{ref: "w__amd_old", size_bytes: 20}]
          "intel" -> [%{ref: "w__intel_cur", size_bytes: 30}, %{ref: "w__intel_old", size_bytes: 40}]
        end

      {:ok, entries, false}
    end

    remote_evict_fun = fn _channel, workload, vendor, ref ->
      send(test_pid, {:remote_evicted, workload, vendor, ref})
      {:ok, %{}}
    end

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd_cur")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        list_fun: list_fun,
        remote_evict_fun: remote_evict_fun,
        remote_retention_sweep_enabled: true,
        nodes: [@node, %{id: "node-1/ds", address: "node-1:9090"}]
      )

    build_current(builder, agent, "w__amd_cur")
    _ = BaseBuilder.retention_sweep_now(builder)

    # Each vendor's SUPERSEDED store base is evicted, carrying that vendor.
    assert_receive {:remote_evicted, "w", "amd", "w__amd_old"}, 1_000
    assert_receive {:remote_evicted, "w", "intel", "w__intel_old"}, 1_000

    # Neither vendor's CURRENT base is touched.
    refute_receive {:remote_evicted, "w", _, "w__amd_cur"}, 200
    refute_receive {:remote_evicted, "w", _, "w__intel_cur"}, 200
  end

  test "remote retention holds candidates when keep refs are absent from the listing" do
    agent = start_recorder()
    table = new_cap_table()
    test_pid = self()

    put_vendor_fact(table, "node-4", nil, "w", "w__phantom", "amd")

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: fn :fake_channel, _req -> {:ok, resp("w__phantom")} end,
        list_fun: fn _channel, _workload, _vendor ->
          {:ok, [%{ref: "w__old", size_bytes: 20}], false}
        end,
        remote_evict_fun: fn _channel, workload, vendor, ref ->
          send(test_pid, {:remote_evicted, workload, vendor, ref})
          {:ok, %{}}
        end,
        remote_retention_sweep_enabled: true
      )

    build_current(builder, agent, "w__phantom")

    log = capture_log(fn -> BaseBuilder.retention_sweep_now(builder) end)

    assert log =~ "retention held: current base unverified"
    assert log =~ "workload w on vendor amd"
    assert log =~ "w__phantom"
    assert log =~ "w__old"
    assert log =~ "1 held because the current base was unverified"
    refute_receive {:remote_evicted, "w", "amd", "w__old"}, 500
  end

  test "remote retention spares a superseded base a session or primed VM still holds" do
    agent = start_recorder()
    table = new_cap_table()
    test_pid = self()

    put_vendor_fact(table, "node-4", nil, "w", "w__amd_cur", "amd")

    list_fun = fn _channel, _workload, _vendor ->
      {:ok, [%{ref: "w__amd_cur", size_bytes: 10}, %{ref: "w__pinned", size_bytes: 99}], false}
    end

    remote_evict_fun = fn _channel, workload, vendor, ref ->
      send(test_pid, {:remote_evicted, workload, vendor, ref})
      {:ok, %{}}
    end

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd_cur")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        list_fun: list_fun,
        remote_evict_fun: remote_evict_fun,
        remote_retention_sweep_enabled: true
      )

    build_current(builder, agent, "w__amd_cur")

    # A superseded ref STILL REFERENCED (not yet drained/evicted): this is the one
    # case where a wrong delete is real loss rather than a rebuild, because a
    # pinned session can only relight against this exact base.
    :sys.replace_state(builder, fn state ->
      w = state.workloads["w"]
      w = %{w | base_refs: %{"w__pinned" => %{primed: 0, sessions: 1, evicted: false}}}
      %{state | workloads: %{"w" => w}}
    end)

    _ = BaseBuilder.retention_sweep_now(builder)

    refute_receive {:remote_evicted, "w", _, "w__pinned"}, 500
  end

  test "remote retention skips a vendor whose daemon does not implement ListArtifacts" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd_cur", "amd")

    # An older daemon answers UNIMPLEMENTED. The sweep must fail toward KEEPING
    # bytes rather than crashing the builder.
    list_fun = fn _channel, _workload, _vendor -> {:error, :unimplemented} end

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd_cur")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        list_fun: list_fun
      )

    build_current(builder, agent, "w__amd_cur")
    _ = BaseBuilder.retention_sweep_now(builder)

    # The builder is still alive and still serving status.
    assert BaseBuilder.status(builder).workloads["w"].snapshot_ref == "w__amd_cur"
  end

  test "the periodic reconcile picks up a vendor that advertised after the build" do
    agent = start_recorder()
    table = new_cap_table()
    # Only the AMD builder node has advertised its base at build time, which is
    # what a CP restart inside the base-advert dial-home window looks like.
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        # Timer off: drive the reconcile explicitly so this is deterministic.
        export_reconcile_interval_ms: 0
      )

    build_current(builder, agent, "w__amd")
    assert latest(agent, "w")["snapshotRefs"] == %{"amd" => ["w__amd"]}

    # An intel node finishes dial-home and advertises its own base AFTER the
    # build. Before #4084 the map stayed frozen at amd-only until the next build.
    put_vendor_fact(table, "node-1", "ds", "w", "w__intel", "intel")
    send(builder, :export_reconcile)

    assert_eventually(fn ->
      latest(agent, "w")["snapshotRefs"] == %{"amd" => ["w__amd"], "intel" => ["w__intel"]}
    end)
  end

  test "the periodic reconcile explicitly clears a missing vendor once and preserves survivors" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")
    put_vendor_fact(table, "node-1", "ds", "w", "w__intel", "intel")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_reconcile_interval_ms: 0
      )

    build_current(builder, agent, "w__amd")

    # Establish the last successfully written effective map, then discard setup
    # calls so the assertions below inspect only the stale-clear patch.
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)
    Agent.update(agent, fn _calls -> [] end)

    NodeCapacity.drop(table, {"node-1", "ds"})
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)

    assert recorded(agent) == [
             {"embervm", "w",
              %{"snapshotRefs" => %{"amd" => ["w__amd"], "intel" => nil}}}
           ]

    # last_snapshot_refs tracks the effective desired map, not the patch body,
    # so the explicit clear is not emitted again on the next identical tick.
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)

    assert recorded(agent) == [
             {"embervm", "w",
              %{"snapshotRefs" => %{"amd" => ["w__amd"], "intel" => nil}}}
           ]
  end

  test "the periodic reconcile writes nothing during a total capacity-fact gap" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")
    put_vendor_fact(table, "node-1", "ds", "w", "w__intel", "intel")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_reconcile_interval_ms: 0
      )

    build_current(builder, agent, "w__amd")
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)
    Agent.update(agent, fn _calls -> [] end)

    NodeCapacity.drop(table, {"node-4", "ds"})
    NodeCapacity.drop(table, {"node-1", "ds"})
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)

    assert recorded(agent) == []
  end

  test "the periodic reconcile writes nothing when the effective map is unchanged" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_reconcile_interval_ms: 0
      )

    build_current(builder, agent, "w__amd")
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)
    Agent.update(agent, fn _calls -> [] end)

    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)

    assert recorded(agent) == []
  end

  test "the periodic reconcile does not re-patch status when the fleet view is unchanged" do
    agent = start_recorder()
    table = new_cap_table()
    put_vendor_fact(table, "node-4", nil, "w", "w__amd", "amd")

    build_fun = fn :fake_channel, _req -> {:ok, resp("w__amd")} end

    builder =
      start_builder(
        capacity_table: table,
        status_writer: recording_status_writer(agent),
        build_fun: build_fun,
        export_reconcile_interval_ms: 0
      )

    build_current(builder, agent, "w__amd")

    # One refresh settles last_snapshot_refs against what the build already wrote.
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)
    settled = length(recorded(agent))

    # Further ticks against an unchanged fleet must be silent, or a converged
    # cluster writes status per workload per tick forever.
    send(builder, :export_reconcile)
    send(builder, :export_reconcile)
    _ = :sys.get_state(builder)

    assert length(recorded(agent)) == settled
  end

  describe "base_revision/1 (the daemon's base cache key)" do
    # noded keys a base as sha256(image_ref, workload_revision, cpu_vendor), so this
    # token IS the base's cache identity. It used to be the CR's metadata.generation,
    # which changes on ANY spec edit -- so a flag that cannot touch a guest rootfs
    # re-keyed the base and took the public demo offline for the rebuild (2026-07-27,
    # nodeLocalWake on demo-postgres, generation 86 -> 87).

    test "a generation bump alone does NOT change the revision" do
      # The exact shape of the outage: same base inputs, new CR generation.
      assert BaseBuilder.base_revision(desc(%{generation: 86})) ==
               BaseBuilder.base_revision(desc(%{generation: 87}))
    end

    test "fields signature/1 excludes do not change the revision" do
      # "a cap-only edit never rebuilds" must hold at the DAEMON, not just in the
      # signature map this module keeps in memory.
      base = BaseBuilder.base_revision(desc())

      for extra <- [
            %{generation: 999},
            %{idle_bank_seconds: 1},
            %{node_local_wake: true},
            %{metering_fail_open: true},
            %{banked_ttl_seconds: 60}
          ] do
        assert BaseBuilder.base_revision(desc(extra)) == base,
               "#{inspect(extra)} must not re-key the base"
      end
    end

    test "every field that DOES shape the base changes the revision" do
      base = BaseBuilder.base_revision(desc())

      for change <- [
            %{image_ref: "imgB"},
            %{vcpus: 2},
            %{mem_mib: 512},
            %{guest_port: 9090},
            %{ready_path: "/other/ready"},
            %{init_env: %{"A" => "1"}}
          ] do
        refute BaseBuilder.base_revision(desc(change)) == base,
               "#{inspect(change)} must re-key the base"
      end
    end

    test "init_env hashes independently of map insertion order" do
      # This is a PERSISTED cache key: map iteration order is not part of the term's
      # contract, so an unsorted encoding could hash two ways for identical input and
      # cause exactly the spurious rebuild being fixed here.
      a = desc(%{init_env: %{"A" => "1", "B" => "2", "C" => "3"}})
      b = desc(%{init_env: %{"C" => "3", "B" => "2", "A" => "1"}})

      assert BaseBuilder.base_revision(a) == BaseBuilder.base_revision(b)
    end

    test "a different init_env VALUE still changes the revision" do
      refute BaseBuilder.base_revision(desc(%{init_env: %{"A" => "1"}})) ==
               BaseBuilder.base_revision(desc(%{init_env: %{"A" => "2"}}))
    end

    test "the revision is a stable short hex digest" do
      rev = BaseBuilder.base_revision(desc())
      assert byte_size(rev) == 16
      assert rev =~ ~r/^[0-9a-f]{16}$/
      # Deterministic across calls: a cache key that moved on its own would rebuild
      # the base on every reconcile.
      assert rev == BaseBuilder.base_revision(desc())
    end

    test "a zip workload keys on its archive sha256, not on generation" do
      zip_at = fn sha -> desc(%{zip: %{runtime: "python3.12", sha256: sha}}) end

      assert BaseBuilder.base_revision(zip_at.("aaa")) ==
               BaseBuilder.base_revision(Map.put(zip_at.("aaa"), :generation, 42))

      refute BaseBuilder.base_revision(zip_at.("aaa")) ==
               BaseBuilder.base_revision(zip_at.("bbb"))
    end
  end

  # -- hydrate the wake's anchor node (#4127) ---------------------------------

  # Nothing owned the pair (workload, anchor node): this module pins a base to ONE
  # instance, a stateful wake anchors to the VOLUME's node, and when those diverge no
  # base is ever placed where the wake needs one, so the wake fails
  # :no_eligible_instance on every retry forever. That took the public demo down for
  # hours on an otherwise healthy fleet.
  test "note_base_missing hydrates the anchor node that reports NO base fact at all" do
    test_pid = self()

    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {builder, _agent, table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    # A SECOND node exists and advertises NOTHING: no base fact for "w" at all. That
    # is the #4127 shape. But another node in the fleet (node-1) still reports the
    # snap1 base as READY, so snapshot_refs_by_vendor can find it and the anchor can
    # hydrate from that fleet-wide ref even though it has no fact of its own.
    put_brick(table, "node-1", "a1", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)
    put_base_fact(table, "node-1", "a1", "w", "snap1", :BASE_BUILD_STATE_READY, true)

    # The anchor node-9 has no base fact at all (node has not reported yet).
    # is the #4127 shape, and node_reports_base_absent?/3 is false for it (a MISSING
    # fact is deliberately not "absent"), so the periodic reconcile can never hydrate
    # it. A wake failure is stronger evidence: the daemon was asked and said no.
    put_brick(table, "node-9", "b1", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    # Registered too: eligible_build_instances/2 iterates state.node_ids, so a
    # capacity fact alone is not enough to make it a build candidate.
    :ok = BaseBuilder.add_node(builder, "node-9/b1", "a9")

    :ok = BaseBuilder.note_base_missing(builder, "w", "node-9")

    assert_receive {:restore_called, "snap1"}, 1_000
  end

  test "note_base_missing hydrates the anchor vendor ref, not the builder vendor ref" do
    test_pid = self()

    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {builder, _agent, table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    put_brick(table, "node-1", "intel", cpu_vendor: "intel", mem_budget: 16_384, mem_headroom: 16_000)
    put_vendor_fact(table, "node-1", "intel", "w", "snap-intel", "intel")
    :ok = BaseBuilder.add_node(builder, "node-1/intel", "a1")

    :ok = BaseBuilder.note_base_missing(builder, "w", "node-1")

    assert_receive {:restore_called, "snap-intel"}, 1_000
    refute_receive {:restore_called, "snap1"}, 100
  end

  test "note_base_missing spawns ONE hydrate for a repeating wake failure" do
    test_pid = self()

    restore_fun = fn :fake_channel, %Embervm.Node.V1.RestoreArtifactRequest{artifact: ref} ->
      send(test_pid, {:restore_called, ref.ref})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {builder, _agent, table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    put_brick(table, "node-9", "b1", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)
    # Fleet-wide fact: node-1 still reports the base as READY so snapshot_refs_by_vendor
    # can find it even though the anchor (node-9) has no base fact.
    put_brick(table, "node-1", "a1", size_class: "8gi", mem_budget: 8_192, mem_headroom: 8_000)
    put_base_fact(table, "node-1", "a1", "w", "snap1", :BASE_BUILD_STATE_READY, true)

    # Registered too: eligible_build_instances/2 iterates state.node_ids, so a
    # capacity fact alone is not enough to make it a build candidate.
    :ok = BaseBuilder.add_node(builder, "node-9/b1", "a9")

    # The wake retry loop runs every ~10s indefinitely; the `hydrating` dedupe is
    # what keeps that from spawning a worker per attempt.
    for _ <- 1..5, do: BaseBuilder.note_base_missing(builder, "w", "node-9")

    assert_receive {:restore_called, "snap1"}, 1_000
    refute_receive {:restore_called, _}, 200
  end

  test "note_base_missing does nothing when no build-eligible instance is on that node" do
    test_pid = self()

    restore_fun = fn :fake_channel, _req ->
      send(test_pid, {:restore_called, :unexpected})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {builder, _agent, _table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    # The anchor itself is wrong (stale volume debris flapping it onto a node that
    # is not in the fleet). No hydrate can help, so it must not spawn one.
    :ok = BaseBuilder.note_base_missing(builder, "w", "node-does-not-exist")

    refute_receive {:restore_called, _}, 200
  end

  test "note_base_missing does nothing when there is no recorded base to hydrate from" do
    test_pid = self()

    restore_fun = fn :fake_channel, _req ->
      send(test_pid, {:restore_called, :unexpected})
      {:ok, %Embervm.Node.V1.RestoreArtifactResponse{accepted: true}}
    end

    {builder, _agent, _table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50
      )

    # A first build has to happen normally; there is no ref to restore.
    :ok = BaseBuilder.note_base_missing(builder, "never-built", "node-4")

    refute_receive {:restore_called, _}, 200
  end

  test "note_base_missing is a no-op when the builder is not running" do
    # Mirrors BrickController.note_denial/2: a missing server must never raise into
    # the wake path, whose outcome is unchanged either way.
    assert BaseBuilder.note_base_missing(:no_such_base_builder_4127, "w", "node-9") == :ok
  end
end
