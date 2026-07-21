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
    {:ok, pid} =
      BaseBuilder.start_link(
        [
          name: nil,
          nodes: Keyword.get(opts, :nodes, [@node]),
          connect_fun: fn _addr -> {:ok, :fake_channel} end,
          disconnect_fun: fn :fake_channel -> :ok end,
          # Base-durability PR-1 defaults for tests that do not exercise export:
          # a no-op export seam (so a build's immediate export never dials the real
          # stub against the fake channel) and a disabled reconcile timer (so no
          # background sweep perturbs timing). Export-specific tests override both.
          export_fun: Keyword.get(opts, :export_fun, fn :fake_channel, _req -> {:ok, %{}} end),
          export_reconcile_interval_ms: Keyword.get(opts, :export_reconcile_interval_ms, 0),
          # Default the op-log to a discarding fake so a build's export audit never
          # touches the real SQLite in tests that do not assert on it. The
          # op-log-specific test overrides both to observe the append.
          op_log: Keyword.get(opts, :op_log, :discard),
          op_log_mod: Keyword.get(opts, :op_log_mod, DiscardOpLog)
        ] ++ Keyword.drop(opts, [:export_fun, :export_reconcile_interval_ms, :op_log, :op_log_mod])
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
  # superseded snap1 via report_base_refs/3, asserting that EvictSnapshot fires
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

    evict_fun = fn :fake_channel, ref ->
      send(test_pid, {:evicted, ref})
      {:ok, %Embervm.Node.V1.EvictSnapshotResponse{}}
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

    evict_fun = fn :fake_channel, ref ->
      send(test_pid, {:evicted, ref})
      {:ok, %Embervm.Node.V1.EvictSnapshotResponse{}}
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

    evict_fun = fn :fake_channel, ref ->
      send(test_pid, {:evicted, ref})
      {:ok, %Embervm.Node.V1.EvictSnapshotResponse{}}
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

  # -- discovery-fed node set (artifact-decoupling PR-C, C4) -------------------

  test "add_node/3 re-drives a workload held with no node so its base finally builds" do
    # Under EndpointSlice discovery the builder is SEEDED EMPTY at boot (it cannot
    # touch Finch at construction), so a workload admitted before any node is
    # discovered is held {:pending, :no_node}. When NodeRegistry's discovery later
    # calls add_node, the held workload must re-drive and build.
    agent = start_recorder()
    build_fun = fn :fake_channel, _req -> {:ok, resp("snap-late", "sha256:late")} end

    builder =
      start_builder(nodes: [], status_writer: recording_status_writer(agent), build_fun: build_fun)

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
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_budget_mib: Keyword.get(opts, :mem_budget, 8_192),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom, 8_000),
      live_vms: 0,
      max_live_vms: 8,
      updated_at: 0
    })
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
end
