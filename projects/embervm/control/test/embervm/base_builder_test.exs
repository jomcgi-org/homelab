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
    %{ref: ref, workload: workload, size_bytes: bytes, base_state: :BASE_BUILD_STATE_READY}
  end

  # An unregistered / .tmp-orphan on-disk base dir: noded reports it with
  # base_state UNSPECIFIED. The sweep must treat it as a candidate (it is neither
  # current nor BUILDING), so the reclaim drains orphans too.
  defp orphan_base(ref, workload, bytes) do
    %{ref: ref, workload: workload, size_bytes: bytes, base_state: :BASE_BUILD_STATE_UNSPECIFIED}
  end

  # Drive one build so the CP has a placed, current snapshot_ref for "w" on node-4.
  # The builder's own build_fun (set by the caller) returns `ref`.
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
        retention_sweep_enabled: true
      )

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

    # Gate on: both the superseded READY base and the .tmp orphan are evicted;
    # the current base is never touched.
    assert_receive {:evicted, "w", "w__superseded"}, 1_000
    assert_receive {:evicted, "w", "w__tmporphan"}, 1_000
    refute_receive {:evicted, "w", "w__current"}, 100
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
      retention_sweep_enabled: true
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
    builder = start_builder(capacity_table: table, retention_sweep_enabled: true)

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
      retention_sweep_enabled: true
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
    builder = start_builder(capacity_table: table, retention_sweep_enabled: true)

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

  test "retention sweep skips a workload whose current base is not yet exported (durability floor)" do
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
        retention_sweep_enabled: true
      )

    build_current(builder, agent, "w__current")

    # Current base is present but NOT yet exported: the whole workload is skipped,
    # so the local cache is never emptied before the S3 durability floor lands.
    put_local_bases_fact(table, "w", "w__current", false, [
      ready_base("w__current", "w", 512),
      ready_base("w__orphan1", "w", 2_048)
    ])

    plan = BaseBuilder.retention_sweep_now(builder)

    entry = Enum.find(plan, &(&1.workload == "w"))
    assert entry.skipped_unexported == true
    assert entry.evict_refs == []
    refute_receive {:evicted, "w", "w__orphan1"}, 200
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
        retention_sweep_enabled: true
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
    assert amd.skipped_unexported == true
    assert amd.evict_refs == []
    refute_receive {:evicted, "w", "w__old_amd"}, 200
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
        retention_sweep_enabled: true
      )

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "w__current" end)

    assert_receive {:exported, "w__current"}, 1_000
    refute_receive {:exported, "w__current"}, 100

    # Export coalescing needs the unexported facts above. Retention, however, is
    # gated on durability, so update both node-mate facts before asserting that the
    # shared inventory is planned and evicted once.
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
        retention_sweep_enabled: true
      )

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
  # built_signature == signature), then flip the node fact so the base reads
  # AFFIRMATIVELY absent (BASE_BUILD_STATE_NONE): the recorded-but-absent trigger.
  # Returns {builder, agent, table}. The initial export is drained so it never
  # perturbs later assertions.
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
        ] ++ Keyword.take(opts, [:restore_fun, :op_log, :op_log_mod, :hydrate_poll_interval_ms, :hydrate_poll_max])
      )

    put_brick(table, "node-4", "big", size_class: "16gi", mem_budget: 16_384, mem_headroom: 16_000)

    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_eventually(fn -> BaseBuilder.status(builder).workloads["w"].snapshot_ref == "snap1" end)
    assert_receive {:exported, "snap1"}, 1_000

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

  test "restore-first falls back to BuildBase on an S3 miss (FAILED_PRECONDITION)" do
    test_pid = self()

    # noded reports the ref is not in the store: a fast, distinguishable miss.
    restore_fun = fn :fake_channel, _req ->
      {:error, %GRPC.RPCError{status: 9, message: "not present in store"}}
    end

    {builder, _agent, _table} =
      build_then_report_base_absent(
        restore_fun: restore_fun,
        hydrate_poll_interval_ms: 5,
        hydrate_poll_max: 50,
        build_fun: fn :fake_channel, _req ->
          send(test_pid, :rebuilt)
          {:ok, resp("snap1")}
        end
      )

    # Re-reconcile: restore-first tries, gets FAILED_PRECONDITION, falls back to a
    # rebuild AT ONCE (no poll wait).
    :ok = BaseBuilder.reconcile(builder, desc(%{mem_mib: 4_000}))
    assert_receive :rebuilt, 1_000
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
end
