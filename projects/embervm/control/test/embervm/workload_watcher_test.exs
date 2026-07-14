defmodule Embervm.WorkloadWatcherTest do
  @moduledoc """
  Exercises `Embervm.WorkloadWatcher`'s reconcile loop end to end: a canned
  `lister` stands in for `Embervm.K8s.list_workloads/0`, an `Agent`-backed
  `status_writer` stands in for `Embervm.K8s.patch_workload_status/3` and
  records every call for assertions, and `watch_startup: false` means the
  informer opens no watch and arms no timer, so every reconcile is driven
  explicitly via `reconcile_now/1`. The list-then-watch path itself is covered
  in the "watch stream" describe block with an injected `watcher_fun`. Every
  test uses a unique table name and an unnamed (`name: nil`) watcher process so
  tests run fully async with zero shared state, and never touch the
  application's own supervised watcher/table.
  """
  use ExUnit.Case, async: true

  alias Embervm.WorkloadCatalog
  alias Embervm.WorkloadWatcher

  defp unique_table do
    String.to_atom("wl_watch_#{System.unique_integer([:positive])}")
  end

  # An Agent recording every status_writer call as {namespace, name,
  # status_map}, newest last, so assertions can pattern-match the most
  # recent write for a given workload.
  defp start_recorder do
    {:ok, agent} = Agent.start_link(fn -> [] end)
    agent
  end

  defp recorded_calls(agent) do
    Agent.get(agent, & &1)
  end

  defp recording_status_writer(agent) do
    fn namespace, name, status_map ->
      Agent.update(agent, fn calls -> calls ++ [{namespace, name, status_map}] end)
      :ok
    end
  end

  # Injected base-builder trigger seams so the watcher tests never touch the
  # application's real supervised Embervm.BaseBuilder (which boots in `mix test`).
  # Records reconcile descriptors and forgotten names into an agent for
  # assertions. Defaults keep the trigger inert for tests that only care about
  # cataloging/status.
  defp base_seams(opts \\ []) do
    agent = Keyword.get(opts, :agent)

    reconcile = fn desc ->
      if agent, do: Agent.update(agent, fn s -> %{s | reconciled: s.reconciled ++ [desc]} end)
      :ok
    end

    forget = fn name ->
      if agent, do: Agent.update(agent, fn s -> %{s | forgotten: s.forgotten ++ [name]} end)
      :ok
    end

    [base_reconcile_fun: reconcile, base_forget_fun: forget]
  end

  defp start_base_recorder do
    {:ok, agent} = Agent.start_link(fn -> %{reconciled: [], forgotten: []} end)
    agent
  end

  # watch_startup: false means init spawns no streamer and arms no timer: every
  # reconcile is driven explicitly through reconcile_now/1, exactly as these
  # LIST-reconcile tests want. The informer/watch path is exercised separately
  # in the "watch stream" describe block below via an injected watcher_fun.
  defp start_watcher(lister, status_writer, table, extra \\ []) do
    {:ok, pid} =
      WorkloadWatcher.start_link(
        [
          name: nil,
          table: table,
          lister: lister,
          status_writer: status_writer,
          watch_startup: false
        ] ++ Keyword.merge(base_seams(), extra)
      )

    pid
  end

  # Mirrors a real API response: binary keys throughout, exactly what
  # :json.decode/1 on a Finch response body would produce.
  defp valid_cr(overrides \\ %{}) do
    base = %{
      "metadata" => %{"name" => "semgrep", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "class" => "task",
        "source" => %{"image" => %{"ref" => "x", "port" => 8080}},
        "resources" => %{"vcpus" => 1, "memMib" => 256},
        "concurrency" => %{"floor" => 0, "cap" => 4},
        "invocation" => %{"retry" => %{"maxAttempts" => 5}}
      }
    }

    deep_merge(base, overrides)
  end

  defp deep_merge(left, right) do
    Map.merge(left, right, fn _k, l, r ->
      if is_map(l) and is_map(r), do: deep_merge(l, r), else: r
    end)
  end

  defp ready_status(calls, name) do
    Enum.filter(calls, fn {_ns, n, _status} -> n == name end) |> List.last()
  end

  test "add: a valid CR is cataloged, writes watcher-owned status, and triggers the base builder" do
    table = unique_table()
    agent = start_recorder()
    base = start_base_recorder()
    lister = fn -> {:ok, [valid_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table, base_seams(agent: base))

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "semgrep")
    assert entry.image_ref == "x"
    assert entry.port == 8080
    assert entry.vcpus == 1
    assert entry.mem_mib == 256
    assert entry.floor == 0
    assert entry.cap == 4
    assert entry.retry.max_attempts == 5
    assert entry.retry.retry_on == [:transport, :timeout, :guest5xx]

    # The watcher writes ONLY its own keys for a valid CR: observedGeneration and
    # primedFloorSatisfied, and crucially NOT conditions (the BaseBuilder owns
    # Ready/BaseBuilt, so the two merge-patches never clobber each other).
    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert status_map["observedGeneration"] == 1
    assert status_map["primedFloorSatisfied"] == false
    refute Map.has_key?(status_map, "conditions")

    # The build trigger fired with the base-shaping fields.
    assert [desc] = Agent.get(base, & &1.reconciled)
    assert desc.name == "semgrep"
    assert desc.namespace == "embervm"
    assert desc.generation == 1
    assert desc.image_ref == "x"
    assert desc.guest_port == 8080
    assert desc.vcpus == 1
    assert desc.mem_mib == 256
  end

  test "update: a changed generation and retry.maxAttempts updates the catalog entry" do
    table = unique_table()
    agent = start_recorder()
    {:ok, lister_agent} = Agent.start_link(fn -> [valid_cr()] end)
    lister = fn -> {:ok, Agent.get(lister_agent, & &1)} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)
    assert {:ok, entry} = WorkloadCatalog.fetch(table, "semgrep")
    assert entry.retry.max_attempts == 5

    updated =
      valid_cr(%{
        "metadata" => %{"generation" => 2},
        "spec" => %{"invocation" => %{"retry" => %{"maxAttempts" => 9}}}
      })

    Agent.update(lister_agent, fn _ -> [updated] end)
    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry2} = WorkloadCatalog.fetch(table, "semgrep")
    assert entry2.retry.max_attempts == 9

    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert status_map["observedGeneration"] == 2
  end

  test "delete: a CR no longer returned by the lister is removed from the catalog" do
    table = unique_table()
    agent = start_recorder()
    {:ok, lister_agent} = Agent.start_link(fn -> [valid_cr()] end)
    lister = fn -> {:ok, Agent.get(lister_agent, & &1)} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)
    assert {:ok, _entry} = WorkloadCatalog.fetch(table, "semgrep")

    Agent.update(lister_agent, fn _ -> [] end)
    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "semgrep") == :error
  end

  test "invalid class: not cataloged, status reports Ready=False/ClassUnsupported, GenServer survives" do
    table = unique_table()
    agent = start_recorder()
    base = start_base_recorder()
    cr = valid_cr(%{"spec" => %{"class" => "session"}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table, base_seams(agent: base))

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "semgrep") == :error

    # The watcher owns the Ready condition for the invalid lane, and forgets any
    # base build for a now-invalid Workload (never triggers a build for it).
    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ClassUnsupported"}] = status_map["conditions"]
    assert Agent.get(base, & &1.reconciled) == []
    assert "semgrep" in Agent.get(base, & &1.forgotten)

    assert Process.alive?(watcher)
  end

  test "invalid concurrency (cap < floor): not cataloged, status reports InvalidConcurrency" do
    table = unique_table()
    agent = start_recorder()
    cr = valid_cr(%{"spec" => %{"concurrency" => %{"floor" => 10, "cap" => 2}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "semgrep") == :error

    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "InvalidConcurrency"}] = status_map["conditions"]

    assert Process.alive?(watcher)
  end

  test "a malformed CR does not crash the watcher, and other valid CRs in the same list still catalog" do
    table = unique_table()
    agent = start_recorder()
    malformed = %{"metadata" => %{}}
    lister = fn -> {:ok, [malformed, valid_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert Process.alive?(watcher)
    assert {:ok, _entry} = WorkloadCatalog.fetch(table, "semgrep")
  end

  test "list error: a previously-cataloged entry survives (fail-open), watcher stays alive" do
    table = unique_table()
    agent = start_recorder()
    {:ok, mode_agent} = Agent.start_link(fn -> :ok end)

    lister = fn ->
      case Agent.get(mode_agent, & &1) do
        :ok -> {:ok, [valid_cr()]}
        :boom -> {:error, :boom}
      end
    end

    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)
    assert {:ok, before} = WorkloadCatalog.fetch(table, "semgrep")

    Agent.update(mode_agent, fn _ -> :boom end)
    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, after_error} = WorkloadCatalog.fetch(table, "semgrep")
    assert after_error == before
    assert Process.alive?(watcher)
  end

  # -- watch stream (list-then-watch informer) ------------------------------

  # These exercise the informer's state machine (event application + the
  # resume-watch vs resync-LIST decision) WITHOUT a live apiserver, by
  # injecting a scripted `watcher_fun` in place of Embervm.K8s.watch_workloads/2
  # and a 3-tuple `lister` in place of Embervm.K8s.list_workloads/0. The watch
  # runs in a real spawned streamer process (that is the part we want under
  # test), so observations are made through `assert_eventually`, which polls the
  # catalog rather than assuming a fixed message ordering.
  describe "watch stream" do
    test "boot LIST catalogs, then a watch ADDED catalogs a second workload" do
      table = unique_table()
      agent = start_recorder()
      {:ok, watcher_pid} = start_informer(table, agent, [valid_cr()], [{[added_event(named_cr("sandbox"))], :block}])

      assert_eventually(fn -> match?({:ok, _}, WorkloadCatalog.fetch(table, "semgrep")) end)
      assert_eventually(fn -> match?({:ok, _}, WorkloadCatalog.fetch(table, "sandbox")) end)
      assert Process.alive?(watcher_pid)
    end

    test "watch MODIFIED updates an existing catalog entry" do
      table = unique_table()
      agent = start_recorder()
      modified = named_cr("semgrep", %{"spec" => %{"invocation" => %{"retry" => %{"maxAttempts" => 9}}}})
      {:ok, _pid} = start_informer(table, agent, [valid_cr()], [{[modified_event(modified)], :block}])

      assert_eventually(fn ->
        match?({:ok, %{retry: %{max_attempts: 9}}}, WorkloadCatalog.fetch(table, "semgrep"))
      end)
    end

    test "watch DELETED drops the workload from the catalog" do
      table = unique_table()
      agent = start_recorder()
      {:ok, _pid} = start_informer(table, agent, [valid_cr()], [{[deleted_event(valid_cr())], :block}])

      assert_eventually(fn -> WorkloadCatalog.fetch(table, "semgrep") == :error end)
    end

    test "a terminal ERROR event triggers a resync LIST (recovering a delete missed while disconnected)" do
      table = unique_table()
      agent = start_recorder()
      # Boot LIST returns semgrep; the watch delivers only an ERROR (RV
      # expired) and closes. The resync LIST returns an EMPTY collection, so
      # the reconcile sweep must drop semgrep even though no DELETED event was
      # ever seen. That is the "catch deletes missed while disconnected" path.
      {:ok, lister_agent} = Agent.start_link(fn -> [valid_cr()] end)

      lister = fn ->
        crs = Agent.get_and_update(lister_agent, fn crs -> {crs, []} end)
        {:ok, crs, "100"}
      end

      {:ok, _pid} =
        WorkloadWatcher.start_link(
          name: nil,
          table: table,
          lister: lister,
          status_writer: recording_status_writer(agent),
          # The ERROR event flags a resync; the stream must then CLOSE so the
          # watch-end handler runs the resync LIST. (A :block episode would fire
          # the event but never end, so the resync would never trigger.) The
          # close is fast, so it goes through the backoff timer; keep that tiny.
          base_reconcile_fun: fn _ -> :ok end,
          base_forget_fun: fn _ -> :ok end,
          watcher_fun: scripted_watcher([{[error_event()], {:ok, :closed}}]),
          base_backoff_ms: 10,
          max_backoff_ms: 20,
          watch_startup: true
        )

      assert_eventually(fn -> WorkloadCatalog.fetch(table, "semgrep") == :error end)
    end

    test "a watch error backs off then resyncs; the watcher survives and re-lists" do
      table = unique_table()
      agent = start_recorder()
      {:ok, calls} = Agent.start_link(fn -> 0 end)

      lister = fn ->
        Agent.update(calls, &(&1 + 1))
        {:ok, [valid_cr()], "100"}
      end

      {:ok, watcher_pid} =
        WorkloadWatcher.start_link(
          name: nil,
          table: table,
          lister: lister,
          status_writer: recording_status_writer(agent),
          # First watch attempt errors; the second blocks (open). With a tiny
          # backoff the resync fires fast enough to assert without slowing CI.
          base_reconcile_fun: fn _ -> :ok end,
          base_forget_fun: fn _ -> :ok end,
          watcher_fun: scripted_watcher([{[], {:error, :boom}}, {[], :block}]),
          base_backoff_ms: 10,
          max_backoff_ms: 20,
          watch_startup: true
        )

      # Two LISTs prove the cycle: the boot LIST, then the post-error resync.
      assert_eventually(fn -> Agent.get(calls, & &1) >= 2 end)
      assert Process.alive?(watcher_pid)
    end

    test "a BOOKMARK event is absorbed without disrupting subsequent deltas" do
      table = unique_table()
      agent = start_recorder()
      # A BOOKMARK (RV-only, empty object) must be applied silently; the ADDED
      # that follows in the same stream still catalogs, proving the BOOKMARK
      # clause neither crashes the watcher nor swallows later events.
      episode = [{[bookmark_event("150"), added_event(named_cr("sandbox"))], :block}]
      {:ok, watcher_pid} = start_informer(table, agent, [valid_cr()], episode)

      assert_eventually(fn -> match?({:ok, _}, WorkloadCatalog.fetch(table, "sandbox")) end)
      assert Process.alive?(watcher_pid)
    end

    test "a clean watch close resumes the watch without re-listing" do
      table = unique_table()
      agent = start_recorder()
      {:ok, calls} = Agent.start_link(fn -> 0 end)

      lister = fn ->
        Agent.update(calls, &(&1 + 1))
        {:ok, [valid_cr()], "100"}
      end

      # First watch closes cleanly with no events; the second catalogs a new
      # workload ("sandbox") then blocks. A clean close must RESUME the watch
      # (start a new streamer) WITHOUT a resync LIST, so once "sandbox" appears
      # we know the second streamer ran, and the lister must still have been
      # called exactly once (the boot LIST) — a resync would have made it two.
      {:ok, watcher_pid} =
        WorkloadWatcher.start_link(
          name: nil,
          table: table,
          lister: lister,
          status_writer: recording_status_writer(agent),
          base_reconcile_fun: fn _ -> :ok end,
          base_forget_fun: fn _ -> :ok end,
          watcher_fun:
            scripted_watcher([{[], {:ok, :closed}}, {[added_event(named_cr("sandbox"))], :block}]),
          # min_watch_ms: 0 makes the (instant) fake close count as a healthy
          # long-lived close, so it takes the immediate-rewatch branch rather
          # than the fast-close backoff branch. That is the path under test.
          min_watch_ms: 0,
          watch_startup: true
        )

      # Positive signal that the re-watch fired (rather than racing a fixed
      # sleep): the second episode's ADDED lands in the catalog. Only then is
      # the "no relist" assertion meaningful.
      assert_eventually(fn -> match?({:ok, _}, WorkloadCatalog.fetch(table, "sandbox")) end)
      assert Agent.get(calls, & &1) == 1
      assert Process.alive?(watcher_pid)
    end
  end

  # -- watch-test helpers ---------------------------------------------------

  # Starts a real informer (watch_startup: true) with a boot LIST returning
  # `list_crs` (as a 3-tuple carrying an RV, like the production lister) and an
  # injected watcher_fun scripted with `episodes`.
  defp start_informer(table, agent, list_crs, episodes) do
    lister = fn -> {:ok, list_crs, "100"} end

    WorkloadWatcher.start_link(
      [
        name: nil,
        table: table,
        lister: lister,
        status_writer: recording_status_writer(agent),
        watcher_fun: scripted_watcher(episodes),
        watch_startup: true
      ] ++ base_seams()
    )
  end

  # A programmable stand-in for Embervm.K8s.watch_workloads/2. Backed by an
  # Agent holding a queue of episodes; each call pops one. An episode is
  # `{events, result}` where result is `{:ok, :closed}` / `{:error, reason}`, OR
  # `{events, :block}` which fires the events then blocks forever (an open,
  # idle watch) so the watcher does not spin re-watching an exhausted script.
  # When the queue empties, further calls block too.
  defp scripted_watcher(episodes) do
    {:ok, queue} = Agent.start_link(fn -> episodes end)

    fn _rv, on_event ->
      episode =
        Agent.get_and_update(queue, fn
          [ep | rest] -> {ep, rest}
          [] -> {:block, []}
        end)

      case episode do
        :block ->
          Process.sleep(:infinity)

        {events, :block} ->
          Enum.each(events, on_event)
          Process.sleep(:infinity)

        {events, result} ->
          Enum.each(events, on_event)
          result
      end
    end
  end

  defp named_cr(name, overrides \\ %{}) do
    valid_cr(deep_merge(%{"metadata" => %{"name" => name}}, overrides))
  end

  defp added_event(cr), do: %{"type" => "ADDED", "object" => cr}
  defp modified_event(cr), do: %{"type" => "MODIFIED", "object" => cr}
  defp deleted_event(cr), do: %{"type" => "DELETED", "object" => cr}
  defp bookmark_event(rv), do: %{"type" => "BOOKMARK", "object" => %{"metadata" => %{"resourceVersion" => rv}}}

  defp error_event do
    %{
      "type" => "ERROR",
      "object" => %{"kind" => "Status", "apiVersion" => "v1", "code" => 410, "reason" => "Expired"}
    }
  end

  # Polls `fun` until it returns truthy or the timeout elapses. Used because the
  # watch path applies events through real inter-process messages, so the exact
  # moment a delta lands in the catalog is not synchronous with the test.
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
end
