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

    # The watcher writes ONLY its own key for a valid CR: observedGeneration.
    # It does NOT write conditions (the BaseBuilder owns Ready/BaseBuilt) nor
    # primedFloorSatisfied (the PoolManager owns it, Task 11), so the three
    # writers' merge-patches never clobber each other.
    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert status_map["observedGeneration"] == 1
    refute Map.has_key?(status_map, "primedFloorSatisfied")
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
    # `cluster` is reserved for a later rung (task/session/serving/stateful are
    # all valid as of R4).
    cr = valid_cr(%{"spec" => %{"class" => "cluster"}})
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

  # -- session class (R2) -----------------------------------------------------

  # A valid session CR: an image source plus the required spec.session block.
  defp session_cr(overrides \\ %{}) do
    base = %{
      "metadata" => %{"name" => "sandbox-session", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "class" => "session",
        "source" => %{"image" => %{"ref" => "sandbox", "port" => 1027}},
        "resources" => %{"vcpus" => 1, "memMib" => 2048},
        "concurrency" => %{"floor" => 1, "cap" => 4},
        "session" => %{
          "idleBankSeconds" => 120,
          "maxLifetimeSeconds" => 21_600,
          "maxSessions" => 8,
          "invokeQueueCap" => 4
        }
      }
    }

    deep_merge(base, overrides)
  end

  defp serving_cr(overrides \\ %{}) do
    base = %{
      "metadata" => %{"name" => "sandbox-serving", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "class" => "serving",
        "source" => %{"image" => %{"ref" => "sandbox", "port" => 1027}},
        "resources" => %{"vcpus" => 1, "memMib" => 2048},
        "concurrency" => %{"cap" => 2},
        "serving" => %{
          "port" => 8080,
          "healthPath" => "/live",
          "host" => "sandbox.serve.example.com",
          "minInstances" => 1,
          "maxInstances" => 2,
          "idleBankSeconds" => 120,
          "drainSeconds" => 10,
          "maxLifetimeSeconds" => 21_600
        }
      }
    }

    deep_merge(base, overrides)
  end

  test "session class: a valid session CR is cataloged with class and session config" do
    table = unique_table()
    agent = start_recorder()
    lister = fn -> {:ok, [session_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "sandbox-session")
    assert entry.class == "session"
    assert entry.cap == 4
    assert entry.floor == 1
    assert entry.session.idle_bank_seconds == 120
    assert entry.session.max_lifetime_seconds == 21_600
    # bankedTtlSeconds omitted -> defaults to maxLifetimeSeconds.
    assert entry.session.banked_ttl_seconds == 21_600
    assert entry.session.max_sessions == 8
    assert entry.session.invoke_queue_cap == 4

    # A valid CR: the watcher writes only observedGeneration (no conditions).
    assert {_ns, "sandbox-session", status_map} = ready_status(recorded_calls(agent), "sandbox-session")
    assert status_map["observedGeneration"] == 1
    refute Map.has_key?(status_map, "conditions")
  end

  test "session class: session block defaults apply when fields are omitted" do
    table = unique_table()
    agent = start_recorder()
    # An empty session block: every numeric field falls back to its default.
    # (deep_merge would keep the populated fields, so replace the block outright.)
    cr = put_in(session_cr(), ["spec", "session"], %{})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "sandbox-session")
    assert entry.session.idle_bank_seconds == 300
    assert entry.session.max_lifetime_seconds == 86_400
    assert entry.session.banked_ttl_seconds == 86_400
    assert entry.session.max_sessions == 16
    assert entry.session.invoke_queue_cap == 4
  end

  test "session class missing spec.session is Ready=False/SessionSpecMissing, not cataloged" do
    table = unique_table()
    agent = start_recorder()
    cr = session_cr()
    cr = Map.update!(cr, "spec", &Map.delete(&1, "session"))
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "sandbox-session") == :error

    assert {_ns, "sandbox-session", status_map} = ready_status(recorded_calls(agent), "sandbox-session")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "SessionSpecMissing"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "task class carrying a spec.session block is Ready=False/SessionSpecUnexpected" do
    table = unique_table()
    agent = start_recorder()
    cr = valid_cr(%{"spec" => %{"session" => %{"idleBankSeconds" => 120}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "semgrep") == :error

    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "SessionSpecUnexpected"}] = status_map["conditions"]
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

  test "serving class: a valid serving CR is cataloged with class and serving config" do
    table = unique_table()
    agent = start_recorder()
    lister = fn -> {:ok, [serving_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "sandbox-serving")
    assert entry.class == "serving"
    assert entry.cap == 2
    assert entry.serving.port == 8080
    assert entry.serving.health_path == "/live"
    assert entry.serving.host == "sandbox.serve.example.com"
    assert entry.serving.min_instances == 1
    assert entry.serving.max_instances == 2
    assert entry.serving.idle_bank_seconds == 120
    assert entry.serving.drain_seconds == 10
    assert entry.serving.max_lifetime_seconds == 21_600
    # bankedTtlSeconds omitted -> defaults to maxLifetimeSeconds (Task 9).
    assert entry.serving.banked_ttl_seconds == 21_600
    # ADR embervm/018 Fork A: nodeLocalWake/meteringFailOpen default false when the
    # CR omits them, so an unmodified serving workload keeps CP-only synchronous wake.
    assert entry.serving.node_local_wake == false
    assert entry.serving.metering_fail_open == false

    # A valid CR: the watcher writes only observedGeneration (no conditions).
    assert {_ns, "sandbox-serving", status_map} = ready_status(recorded_calls(agent), "sandbox-serving")
    assert status_map["observedGeneration"] == 1
    refute Map.has_key?(status_map, "conditions")
  end

  test "serving class: serving block defaults apply when fields are omitted" do
    table = unique_table()
    agent = start_recorder()
    # Only the required fields (port, host); every other field falls back to
    # its default. (deep_merge would keep the populated fields, so replace
    # the block outright.)
    cr = put_in(serving_cr(), ["spec", "serving"], %{"port" => 8080, "host" => "sandbox.serve.example.com"})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "sandbox-serving")
    assert entry.serving.health_path == "/healthz"
    assert entry.serving.min_instances == 0
    assert entry.serving.max_instances == 2
    assert entry.serving.idle_bank_seconds == 300
    assert entry.serving.drain_seconds == 5
    assert entry.serving.max_lifetime_seconds == 86_400
    # bankedTtlSeconds omitted with no maxLifetimeSeconds -> the default (86_400).
    assert entry.serving.banked_ttl_seconds == 86_400
  end

  test "serving class missing spec.serving is Ready=False/ServingSpecMissing, not cataloged" do
    table = unique_table()
    agent = start_recorder()
    cr = serving_cr()
    cr = Map.update!(cr, "spec", &Map.delete(&1, "serving"))
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "sandbox-serving") == :error

    assert {_ns, "sandbox-serving", status_map} = ready_status(recorded_calls(agent), "sandbox-serving")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ServingSpecMissing"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "task class carrying a spec.serving block is Ready=False/ServingSpecUnexpected" do
    table = unique_table()
    agent = start_recorder()
    cr = valid_cr(%{"spec" => %{"serving" => %{"port" => 8080, "host" => "x.example.com"}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "semgrep") == :error

    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ServingSpecUnexpected"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "session class carrying a spec.serving block is Ready=False/ServingSpecUnexpected" do
    table = unique_table()
    agent = start_recorder()
    cr = session_cr(%{"spec" => %{"serving" => %{"port" => 8080, "host" => "x.example.com"}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "sandbox-session") == :error

    assert {_ns, "sandbox-session", status_map} = ready_status(recorded_calls(agent), "sandbox-session")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ServingSpecUnexpected"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "serving class idleBankSeconds below the 30s minimum is rejected (CRD-schema-equivalent bound)" do
    # The CRD schema itself enforces this minimum at admission; this test
    # documents the watcher's own re-defaulting never silently accepts a
    # sub-minimum value that somehow reached LIST/WATCH (e.g. an older CRD
    # version), by asserting the watcher passes the value straight through
    # rather than clamping it. If a future edit adds a watcher-side floor
    # this test's assertion should change to reflect the rejection.
    table = unique_table()
    agent = start_recorder()
    cr = put_in(serving_cr(), ["spec", "serving", "idleBankSeconds"], 5)
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    # v1alpha1 relies on the CRD schema's own `minimum: 30` to reject this at
    # admission (the apiserver never lets it reach LIST/WATCH); the watcher
    # itself does not re-validate the bound, mirroring how it does not
    # re-validate other CRD-enforced minimums (e.g. concurrency.cap >= 1).
    assert {:ok, entry} = WorkloadCatalog.fetch(table, "sandbox-serving")
    assert entry.serving.idle_bank_seconds == 5
  end

  test "serving class: concurrency.cap and serving.maxInstances disagreeing is Ready=False/ConcurrencyServingMismatch" do
    table = unique_table()
    agent = start_recorder()
    cr = serving_cr(%{"spec" => %{"concurrency" => %{"cap" => 5}, "serving" => %{"maxInstances" => 2}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "sandbox-serving") == :error

    assert {_ns, "sandbox-serving", status_map} = ready_status(recorded_calls(agent), "sandbox-serving")

    assert [%{"type" => "Ready", "status" => "False", "reason" => "ConcurrencyServingMismatch"}] =
             status_map["conditions"]

    assert Process.alive?(watcher)
  end

  test "serving class: concurrency.cap and serving.maxInstances agreeing is valid" do
    table = unique_table()
    agent = start_recorder()
    cr = serving_cr(%{"spec" => %{"concurrency" => %{"cap" => 3}, "serving" => %{"maxInstances" => 3}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "sandbox-serving")
    assert entry.cap == 3
    assert entry.serving.max_instances == 3
  end

  test "serving class: two workloads declaring the same host, the second is Ready=False/ServingHostConflict" do
    table = unique_table()
    agent = start_recorder()

    first = serving_cr()
    second = serving_cr(%{"metadata" => %{"name" => "sandbox-serving-2"}})
    lister = fn -> {:ok, [first, second]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    # The first-listed workload wins the host and catalogs cleanly.
    assert {:ok, _} = WorkloadCatalog.fetch(table, "sandbox-serving")

    # The second is rejected: same host, already owned.
    assert WorkloadCatalog.fetch(table, "sandbox-serving-2") == :error

    assert {_ns, "sandbox-serving-2", status_map} = ready_status(recorded_calls(agent), "sandbox-serving-2")

    assert [%{"type" => "Ready", "status" => "False", "reason" => "ServingHostConflict"}] =
             status_map["conditions"]

    assert Process.alive?(watcher)
  end

  test "serving class: a resolved host conflict re-validates cleanly (two-reconcile eventual convergence)" do
    table = unique_table()
    agent = start_recorder()

    first = serving_cr()
    second = serving_cr(%{"metadata" => %{"name" => "sandbox-serving-2"}})
    {:ok, lister_agent} = Agent.start_link(fn -> [first, second] end)
    lister = fn -> {:ok, Agent.get(lister_agent, & &1)} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)
    assert WorkloadCatalog.fetch(table, "sandbox-serving-2") == :error

    # The first workload is deleted (no longer returned by LIST); the second
    # now owns the host uncontested and must eventually catalog cleanly,
    # proving the collision check is live-catalog-derived, not some sticky
    # watcher-local rejection. Same watcher, mutable lister across reconciles
    # (mirroring the fail-open list-error test's idiom), not a second watcher
    # instance: two live watchers would both try to create the SAME named ETS
    # table and collide.
    #
    # The heal takes TWO reconciles, not one: reconcile_full/3 validates every
    # CR in the list against the LIVE catalog first, and only prunes names
    # absent from the list AFTER that validation pass completes (see
    # `all_names(state.table) -- seen` at the end of reconcile_full/3). So on
    # the reconcile right after `first` disappears from the list, `second`
    # still validates against a catalog where `first` (and its host) has not
    # been pruned yet, so it conflicts again; `first` is pruned only at the
    # END of that same reconcile. `second` therefore catalogs cleanly on the
    # FOLLOWING reconcile, once `first` is actually gone. This is correct
    # eventually-consistent behavior for a shared validate-before-prune path
    # (also used by task/session), not a bug in the serving-only logic.
    Agent.update(lister_agent, fn _ -> [second] end)
    # R2: first is still in the catalog (pruned at the end of THIS pass), so
    # second still conflicts.
    :ok = WorkloadWatcher.reconcile_now(watcher)
    assert WorkloadCatalog.fetch(table, "sandbox-serving-2") == :error
    # R3: first is now gone, second catalogs cleanly.
    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "sandbox-serving-2")
    assert entry.serving.host == "sandbox.serve.example.com"
  end

  # -- stateful class (R4) ----------------------------------------------------

  # A valid stateful CR: an image source plus the required spec.stateful block,
  # and NO concurrency block (singleton by construction).
  defp stateful_cr(overrides \\ %{}) do
    base = %{
      "metadata" => %{"name" => "scratch-postgres", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "class" => "stateful",
        "source" => %{"image" => %{"ref" => "scratch-postgres", "port" => 1027}},
        "resources" => %{"vcpus" => 1, "memMib" => 512},
        "stateful" => %{
          "port" => 5432,
          # listenPort must fall inside the default range 5400..5409 (the guest
          # port 5432 is distinct and unconstrained).
          "listenPort" => 5401,
          "volumeSizeGiB" => 10,
          "volumeMountPath" => "/data",
          "idleBankSeconds" => 600,
          "maxLifetimeSeconds" => 604_800,
          "bankedTtlSeconds" => 2_592_000,
          "wakeTimeoutSeconds" => 60,
          "secretRef" => "scratch-postgres-creds"
        }
      }
    }

    deep_merge(base, overrides)
  end

  test "stateful class: a valid stateful CR is cataloged with class and stateful config, singleton floor/cap" do
    table = unique_table()
    agent = start_recorder()
    lister = fn -> {:ok, [stateful_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "scratch-postgres")
    assert entry.class == "stateful"
    # Singleton by construction: no concurrency block, internal floor/cap 0/1.
    assert entry.floor == 0
    assert entry.cap == 1
    assert entry.stateful.port == 5432
    assert entry.stateful.listen_port == 5401
    assert entry.stateful.volume_size_gib == 10
    assert entry.stateful.volume_mount_path == "/data"
    assert entry.stateful.idle_bank_seconds == 600
    assert entry.stateful.max_lifetime_seconds == 604_800
    assert entry.stateful.banked_ttl_seconds == 2_592_000
    assert entry.stateful.wake_timeout_seconds == 60
    assert entry.stateful.secret_ref == "scratch-postgres-creds"
    # ADR embervm/018 Phase 2: nodeLocalWake/meteringFailOpen default false when the
    # CR omits them, so an unmodified stateful workload keeps CP-only wake.
    assert entry.stateful.node_local_wake == false
    assert entry.stateful.metering_fail_open == false

    assert {_ns, "scratch-postgres", status_map} = ready_status(recorded_calls(agent), "scratch-postgres")
    assert status_map["observedGeneration"] == 1
    refute Map.has_key?(status_map, "conditions")
  end

  test "stateful class: stateful block defaults apply when optional fields are omitted" do
    table = unique_table()
    agent = start_recorder()
    # Only the required fields; every optional field falls back to its default.
    cr =
      put_in(stateful_cr(), ["spec", "stateful"], %{
        "port" => 5432,
        "listenPort" => 5400,
        "volumeSizeGiB" => 5
      })

    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "scratch-postgres")
    assert entry.stateful.volume_mount_path == "/data"
    assert entry.stateful.idle_bank_seconds == 300
    assert entry.stateful.max_lifetime_seconds == 86_400
    assert entry.stateful.banked_ttl_seconds == 604_800
    assert entry.stateful.wake_timeout_seconds == 60
    # secretRef is optional; omitted here, so it parses to nil (D-R4.PR-7.1: a
    # workload with no first-boot secrets to deliver never reads a Secret).
    assert entry.stateful.secret_ref == nil
    assert entry.stateful.interruptible_bank == false
    assert entry.stateful.auto_wake == false
  end

  test "stateful class: autoWake true is parsed onto the catalog entry (PR-B0a A2)" do
    table = unique_table()
    agent = start_recorder()
    cr = put_in(stateful_cr(), ["spec", "stateful", "autoWake"], true)
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "scratch-postgres")
    assert entry.stateful.auto_wake == true
  end

  test "stateful class: interruptibleBank true is parsed onto the catalog entry (ADR 008)" do
    table = unique_table()
    agent = start_recorder()
    cr = put_in(stateful_cr(), ["spec", "stateful", "interruptibleBank"], true)
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "scratch-postgres")
    assert entry.stateful.interruptible_bank == true
  end

  test "stateful class: explicit interruptibleBank false parses to false (ADR 008)" do
    table = unique_table()
    agent = start_recorder()
    cr = put_in(stateful_cr(), ["spec", "stateful", "interruptibleBank"], false)
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)
    :ok = WorkloadWatcher.reconcile_now(watcher)
    assert {:ok, entry} = WorkloadCatalog.fetch(table, "scratch-postgres")
    assert entry.stateful.interruptible_bank == false
  end

  test "stateful class missing spec.stateful is Ready=False/StatefulSpecMissing, not cataloged" do
    table = unique_table()
    agent = start_recorder()
    cr = stateful_cr()
    cr = Map.update!(cr, "spec", &Map.delete(&1, "stateful"))
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "scratch-postgres") == :error

    assert {_ns, "scratch-postgres", status_map} = ready_status(recorded_calls(agent), "scratch-postgres")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "StatefulSpecMissing"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "stateful class carrying a spec.concurrency block is Ready=False/ConcurrencyUnexpected" do
    table = unique_table()
    agent = start_recorder()
    cr = stateful_cr(%{"spec" => %{"concurrency" => %{"cap" => 2}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "scratch-postgres") == :error

    assert {_ns, "scratch-postgres", status_map} = ready_status(recorded_calls(agent), "scratch-postgres")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ConcurrencyUnexpected"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "stateful class carrying a spec.serving block is Ready=False/ServingSpecUnexpected" do
    table = unique_table()
    agent = start_recorder()
    cr = stateful_cr(%{"spec" => %{"serving" => %{"port" => 8080, "host" => "x.example.com"}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "scratch-postgres") == :error

    assert {_ns, "scratch-postgres", status_map} = ready_status(recorded_calls(agent), "scratch-postgres")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ServingSpecUnexpected"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "stateful class: a listenPort outside the configured range is Ready=False/StatefulListenPortOutOfRange" do
    table = unique_table()
    agent = start_recorder()
    cr = put_in(stateful_cr(), ["spec", "stateful", "listenPort"], 9999)
    lister = fn -> {:ok, [cr]} end
    # Inject a narrow range so the test is independent of the default.
    watcher = start_watcher(lister, recording_status_writer(agent), table, stateful_listen_range: 5400..5409)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "scratch-postgres") == :error

    assert {_ns, "scratch-postgres", status_map} = ready_status(recorded_calls(agent), "scratch-postgres")

    assert [%{"type" => "Ready", "status" => "False", "reason" => "StatefulListenPortOutOfRange"}] =
             status_map["conditions"]

    assert Process.alive?(watcher)
  end

  test "stateful class: two workloads on the same listenPort, the second is Ready=False/StatefulListenPortConflict" do
    table = unique_table()
    agent = start_recorder()

    first = stateful_cr()
    second = stateful_cr(%{"metadata" => %{"name" => "scratch-postgres-2"}})
    lister = fn -> {:ok, [first, second]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    # The first-listed workload wins the listenPort and catalogs cleanly.
    assert {:ok, _} = WorkloadCatalog.fetch(table, "scratch-postgres")
    assert WorkloadCatalog.fetch(table, "scratch-postgres-2") == :error

    assert {_ns, "scratch-postgres-2", status_map} = ready_status(recorded_calls(agent), "scratch-postgres-2")

    assert [%{"type" => "Ready", "status" => "False", "reason" => "StatefulListenPortConflict"}] =
             status_map["conditions"]

    assert Process.alive?(watcher)
  end

  test "stateful class: editing volumeSizeGiB is Ready=False/StatefulVolumeSizeImmutable" do
    table = unique_table()
    agent = start_recorder()

    # First reconcile catalogs the workload at 10 GiB.
    {:ok, lister_agent} = Agent.start_link(fn -> [stateful_cr()] end)
    lister = fn -> {:ok, Agent.get(lister_agent, & &1)} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)
    assert {:ok, entry} = WorkloadCatalog.fetch(table, "scratch-postgres")
    assert entry.stateful.volume_size_gib == 10

    # A later edit to 20 GiB is rejected against the cataloged entry; the entry
    # is dropped (an invalid CR is never served) and the immutability condition
    # is written.
    Agent.update(lister_agent, fn _ -> [put_in(stateful_cr(), ["spec", "stateful", "volumeSizeGiB"], 20)] end)
    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "scratch-postgres") == :error

    assert {_ns, "scratch-postgres", status_map} = ready_status(recorded_calls(agent), "scratch-postgres")

    assert [%{"type" => "Ready", "status" => "False", "reason" => "StatefulVolumeSizeImmutable"}] =
             status_map["conditions"]

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

  # -- composite class (R5) ---------------------------------------------------

  # A valid composite CR: an image source (the group base seed), the required
  # spec.group block (a leader member + a 2-replica worker member, entry on the
  # leader), and NO concurrency block (group size is fixed by the member set).
  defp composite_cr(overrides \\ %{}) do
    base = %{
      "metadata" => %{"name" => "demo-group", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "class" => "composite",
        "source" => %{"image" => %{"ref" => "demo-group", "port" => 1027}},
        "resources" => %{"vcpus" => 1, "memMib" => 512},
        "group" => %{
          "members" => [
            %{
              "name" => "leader",
              "role" => "leader",
              "startOrder" => 0,
              "source" => %{"image" => %{"ref" => "demo-leader"}},
              "healthPort" => 8080
            },
            %{
              "name" => "worker",
              "role" => "worker",
              "startOrder" => 1,
              "replicas" => 2,
              "source" => %{"image" => %{"ref" => "demo-worker"}},
              "healthPort" => 8080
            }
          ],
          # entry.listenPort must fall inside the default composite range 5410..5419.
          "entry" => %{"member" => "leader", "port" => 8080, "listenPort" => 5410},
          "idleBankSeconds" => 600,
          "maxLifetimeSeconds" => 86_400,
          "bankedTtlSeconds" => 604_800,
          "wakeTimeoutSeconds" => 120
        }
      }
    }

    deep_merge(base, overrides)
  end

  test "composite class: a valid composite CR is cataloged with class and group config, group floor/cap" do
    table = unique_table()
    agent = start_recorder()
    lister = fn -> {:ok, [composite_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "demo-group")
    assert entry.class == "composite"
    # No concurrency block: internal floor/cap 0/1 (scale-to-zero group).
    assert entry.floor == 0
    assert entry.cap == 1
    assert length(entry.group.members) == 2
    assert Enum.map(entry.group.members, & &1.name) == ["leader", "worker"]
    worker = Enum.find(entry.group.members, &(&1.name == "worker"))
    assert worker.replicas == 2
    assert worker.start_order == 1
    assert worker.image_ref == "demo-worker"
    assert worker.health_port == 8080
    assert entry.group.entry.member == "leader"
    assert entry.group.entry.port == 8080
    assert entry.group.entry.listen_port == 5410
    assert entry.group.idle_bank_seconds == 600
    assert entry.group.max_lifetime_seconds == 86_400
    assert entry.group.banked_ttl_seconds == 604_800
    assert entry.group.wake_timeout_seconds == 120
    assert entry.group.secret_ref == nil

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")
    assert status_map["observedGeneration"] == 1
    refute Map.has_key?(status_map, "conditions")
  end

  test "composite class: group block timer defaults apply when omitted" do
    table = unique_table()
    agent = start_recorder()

    cr =
      put_in(composite_cr(), ["spec", "group"], %{
        "members" => [
          %{"name" => "solo", "source" => %{"image" => %{"ref" => "solo"}}, "healthPort" => 9000}
        ],
        "entry" => %{"member" => "solo", "port" => 9000, "listenPort" => 5411}
      })

    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "demo-group")
    assert entry.group.idle_bank_seconds == 600
    assert entry.group.max_lifetime_seconds == 86_400
    assert entry.group.banked_ttl_seconds == 604_800
    assert entry.group.wake_timeout_seconds == 120
    # A replicas-omitted member defaults to 1.
    assert Enum.at(entry.group.members, 0).replicas == 1
  end

  test "composite class with a stable secretRef parses name/key" do
    table = unique_table()
    agent = start_recorder()

    cr =
      put_in(composite_cr(), ["spec", "group", "secretRef"], %{
        "name" => "group-secret",
        "key" => "token"
      })

    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "demo-group")
    assert entry.group.secret_ref == %{name: "group-secret", key: "token"}
  end

  test "composite class missing spec.group is Ready=False/GroupSpecMissing, not cataloged" do
    table = unique_table()
    agent = start_recorder()
    cr = composite_cr()
    cr = Map.update!(cr, "spec", &Map.delete(&1, "group"))
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "demo-group") == :error

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "GroupSpecMissing"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "composite class carrying a spec.concurrency block is Ready=False/ConcurrencyUnexpected" do
    table = unique_table()
    agent = start_recorder()
    cr = composite_cr(%{"spec" => %{"concurrency" => %{"cap" => 2}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "demo-group") == :error

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ConcurrencyUnexpected"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "composite class carrying a spec.stateful block is Ready=False/StatefulSpecUnexpected" do
    table = unique_table()
    agent = start_recorder()
    cr = composite_cr(%{"spec" => %{"stateful" => %{"port" => 5432, "listenPort" => 5401, "volumeSizeGiB" => 1}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "demo-group") == :error

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "StatefulSpecUnexpected"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "a stateful CR carrying a spec.group block is Ready=False/GroupSpecUnexpected (symmetric cross-rejection)" do
    table = unique_table()
    agent = start_recorder()
    cr = stateful_cr(%{"spec" => %{"group" => %{"members" => [], "entry" => %{}}}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "scratch-postgres") == :error

    assert {_ns, "scratch-postgres", status_map} = ready_status(recorded_calls(agent), "scratch-postgres")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "GroupSpecUnexpected"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "composite class: duplicate member names are Ready=False/GroupMemberNameConflict" do
    table = unique_table()
    agent = start_recorder()

    cr =
      put_in(composite_cr(), ["spec", "group", "members"], [
        %{"name" => "dup", "source" => %{"image" => %{"ref" => "a"}}, "healthPort" => 8080},
        %{"name" => "dup", "source" => %{"image" => %{"ref" => "b"}}, "healthPort" => 8080}
      ])

    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "demo-group") == :error

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "GroupMemberNameConflict"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "composite class: an entry naming a missing member is Ready=False/GroupEntryMemberUnknown" do
    table = unique_table()
    agent = start_recorder()
    cr = put_in(composite_cr(), ["spec", "group", "entry", "member"], "ghost")
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "demo-group") == :error

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "GroupEntryMemberUnknown"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "composite class: an entry naming an expanded replica name is accepted" do
    table = unique_table()
    agent = start_recorder()
    # `worker` has replicas 2, so `worker-1` is a valid entry target.
    cr = put_in(composite_cr(), ["spec", "group", "entry", "member"], "worker-1")
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "demo-group")
    assert entry.group.entry.member == "worker-1"
  end

  test "composite class: a listenPort outside the configured composite range is Ready=False/GroupListenPortOutOfRange" do
    table = unique_table()
    agent = start_recorder()
    cr = put_in(composite_cr(), ["spec", "group", "entry", "listenPort"], 9999)
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table, composite_listen_range: 5410..5419)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "demo-group") == :error

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")

    assert [%{"type" => "Ready", "status" => "False", "reason" => "GroupListenPortOutOfRange"}] =
             status_map["conditions"]

    assert Process.alive?(watcher)
  end

  test "composite class: two groups on the same listenPort, the second is Ready=False/GroupListenPortConflict" do
    table = unique_table()
    agent = start_recorder()

    first = composite_cr()
    second = composite_cr(%{"metadata" => %{"name" => "demo-group-2"}})
    lister = fn -> {:ok, [first, second]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    # One of the two wins (LIST order sets the tiebreak); the other is rejected.
    cataloged = [
      WorkloadCatalog.fetch(table, "demo-group"),
      WorkloadCatalog.fetch(table, "demo-group-2")
    ]

    assert Enum.count(cataloged, &match?({:ok, _}, &1)) == 1

    conflict =
      recorded_calls(agent)
      |> Enum.map(fn {_ns, _n, status} -> status end)
      |> Enum.filter(&match?(%{"conditions" => [%{"reason" => "GroupListenPortConflict"}]}, &1))

    assert conflict != []
    assert Process.alive?(watcher)
  end

  test "composite class: an over-cap expanded member count is Ready=False/GroupSizeExceeded" do
    table = unique_table()
    agent = start_recorder()
    # leader(1) + worker(replicas 4) = 5 expanded members, over a maxGroupSize of 4.
    cr = put_in(composite_cr(), ["spec", "group", "members"], [
      %{"name" => "leader", "source" => %{"image" => %{"ref" => "l"}}, "healthPort" => 8080},
      %{"name" => "worker", "replicas" => 4, "source" => %{"image" => %{"ref" => "w"}}, "healthPort" => 8080}
    ])

    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table, max_group_size: 4)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "demo-group") == :error

    assert {_ns, "demo-group", status_map} = ready_status(recorded_calls(agent), "demo-group")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "GroupSizeExceeded"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  # -- zip-lane source (R1, ADR embervm/002) --------------------------------

  # A zip-lane CR: source.zip replaces source.image. The base drops the image
  # member entirely (deep_merge would otherwise leave both, which is invalid).
  defp zip_cr(zip_overrides \\ %{}) do
    zip =
      Map.merge(
        %{
          "runtime" => "python312",
          "codeUri" => "http://filer/embervm-zips/echo.zip",
          "sha256" => "abc123"
        },
        zip_overrides
      )

    %{
      "metadata" => %{"name" => "zipfn", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "class" => "task",
        "source" => %{"zip" => zip},
        "resources" => %{"vcpus" => 1, "memMib" => 512},
        "concurrency" => %{"floor" => 0, "cap" => 4}
      }
    }
  end

  test "zip source: a valid zip CR catalogs with the frozen port + zip block and triggers the builder" do
    table = unique_table()
    agent = start_recorder()
    base = start_base_recorder()
    lister = fn -> {:ok, [zip_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table, base_seams(agent: base))

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "zipfn")
    # Image lane fields: image_ref is nil, port is the frozen zip contract port,
    # ready/invoke default from the zip block.
    assert entry.image_ref == nil
    assert entry.port == 1027
    assert entry.ready_path == "/shim/ready"
    assert entry.invoke_path == "/invoke"
    assert entry.zip.runtime == "python312"
    assert entry.zip.code_uri == "http://filer/embervm-zips/echo.zip"
    assert entry.zip.sha256 == "abc123"
    assert entry.zip.handler == "app.handle"

    # The build descriptor carries the zip block and EMBER_HANDLER init_env.
    assert [desc] = Agent.get(base, & &1.reconciled)
    assert desc.image_ref == nil
    assert desc.zip.sha256 == "abc123"
    assert desc.init_env == %{"EMBER_HANDLER" => "app.handle"}
  end

  test "zip source: a custom handler flows into EMBER_HANDLER init_env" do
    table = unique_table()
    agent = start_recorder()
    base = start_base_recorder()
    lister = fn -> {:ok, [zip_cr(%{"handler" => "main.entry"})]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table, base_seams(agent: base))

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert {:ok, entry} = WorkloadCatalog.fetch(table, "zipfn")
    assert entry.zip.handler == "main.entry"
    assert [desc] = Agent.get(base, & &1.reconciled)
    assert desc.init_env == %{"EMBER_HANDLER" => "main.entry"}
  end

  test "zip source: missing sha256 is rejected Ready=False/InvalidZipSource, not cataloged, no crash" do
    table = unique_table()
    agent = start_recorder()
    base = start_base_recorder()
    cr = zip_cr() |> update_in(["spec", "source", "zip"], &Map.delete(&1, "sha256"))
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table, base_seams(agent: base))

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "zipfn") == :error
    assert {_ns, "zipfn", status_map} = ready_status(recorded_calls(agent), "zipfn")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "InvalidZipSource"}] = status_map["conditions"]
    assert Agent.get(base, & &1.reconciled) == []
    assert Process.alive?(watcher)
  end

  test "zip source: unknown runtime is rejected Ready=False/InvalidZipSource" do
    table = unique_table()
    agent = start_recorder()
    cr = zip_cr(%{"runtime" => "rust1"})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "zipfn") == :error
    assert {_ns, "zipfn", status_map} = ready_status(recorded_calls(agent), "zipfn")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "InvalidZipSource"}] = status_map["conditions"]
    assert Process.alive?(watcher)
  end

  test "both source members set is rejected Ready=False/SourceAmbiguous" do
    table = unique_table()
    agent = start_recorder()

    cr =
      zip_cr()
      |> put_in(["spec", "source", "image"], %{"ref" => "x", "port" => 8080})

    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "zipfn") == :error
    assert {_ns, "zipfn", status_map} = ready_status(recorded_calls(agent), "zipfn")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "SourceAmbiguous"}] = status_map["conditions"]
    assert Process.alive?(watcher)
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

  # -- periodic BaseBuilder resync (RCA H1 self-heal) --------------------------

  # Exercises the fix for RCA candidate H1: BaseBuilder.reconcile/2 is a
  # fire-and-forget cast_if_alive with no internal re-assert, so a single
  # dropped delivery (the BaseBuilder momentarily down, a transient error, a
  # restart race) previously left a base unprovisioned forever. These tests
  # drive the periodic resync synchronously via resync_bases_now/1 (never a
  # real timer, per this test module's async/watch_startup: false discipline)
  # to prove: (1) a dropped initial cast leaves nothing reconciled, and (2) the
  # resync tick re-delivers it, self-healing within one interval instead of
  # wedging forever.
  describe "periodic BaseBuilder resync" do
    test "a dropped initial cast is re-delivered by the next resync tick" do
      table = unique_table()
      agent = start_recorder()
      base = start_base_recorder()

      # Simulate BaseBuilder being down for the FIRST reconcile (the dropped
      # delivery): base_reconcile_fun is a no-op that records nothing, exactly
      # like cast_if_alive/2 silently swallowing a cast when
      # GenServer.whereis(BaseBuilder) is nil.
      dropped = fn _desc -> :ok end
      lister = fn -> {:ok, [valid_cr()]} end

      watcher =
        start_watcher(lister, recording_status_writer(agent), table, base_reconcile_fun: dropped)

      :ok = WorkloadWatcher.reconcile_now(watcher)

      # The Workload IS cataloged (cataloging and the base-build trigger are
      # independent), but the "delivery" to BaseBuilder never happened.
      assert {:ok, _entry} = WorkloadCatalog.fetch(table, "semgrep")
      assert Agent.get(base, & &1.reconciled) == []

      # Swap in the real recording seam (as if the BaseBuilder had since come
      # back up) without touching the catalog or re-listing, then fire one
      # resync tick.
      healed_fun = base_seams(agent: base)[:base_reconcile_fun]
      :sys.replace_state(watcher, fn state -> %{state | base_reconcile_fun: healed_fun} end)
      :ok = WorkloadWatcher.resync_bases_now(watcher)

      # The resync self-healed the dropped delivery: exactly one reconcile call
      # now recorded, for the Workload that was already in the catalog.
      assert [desc] = Agent.get(base, & &1.reconciled)
      assert desc.name == "semgrep"
      assert desc.image_ref == "x"
      assert desc.guest_port == 8080
    end

    test "resync re-casts the identical descriptor a fresh LIST would have sent" do
      table = unique_table()
      agent = start_recorder()
      base = start_base_recorder()
      lister = fn -> {:ok, [valid_cr()]} end
      watcher = start_watcher(lister, recording_status_writer(agent), table, base_seams(agent: base))

      :ok = WorkloadWatcher.reconcile_now(watcher)
      assert [from_list] = Agent.get(base, & &1.reconciled)

      :ok = WorkloadWatcher.resync_bases_now(watcher)
      assert [^from_list, from_resync] = Agent.get(base, & &1.reconciled)
      assert from_resync == from_list
    end

    test "a deleted Workload is never re-cast by a later resync tick" do
      table = unique_table()
      agent = start_recorder()
      base = start_base_recorder()
      {:ok, lister_agent} = Agent.start_link(fn -> [valid_cr()] end)
      lister = fn -> {:ok, Agent.get(lister_agent, & &1)} end
      watcher = start_watcher(lister, recording_status_writer(agent), table, base_seams(agent: base))

      :ok = WorkloadWatcher.reconcile_now(watcher)
      assert [_] = Agent.get(base, & &1.reconciled)

      Agent.update(lister_agent, fn _ -> [] end)
      :ok = WorkloadWatcher.reconcile_now(watcher)
      assert WorkloadCatalog.fetch(table, "semgrep") == :error

      :ok = WorkloadWatcher.resync_bases_now(watcher)
      # Still exactly the one reconcile call from before the delete; the resync
      # only re-casts what is CURRENTLY in the catalog.
      assert [_] = Agent.get(base, & &1.reconciled)
    end

    test "resync_interval_ms: 0 disables the periodic timer" do
      table = unique_table()
      agent = start_recorder()
      base = start_base_recorder()

      # watch_startup: true (the default) so init/1's timer-arming branch
      # actually runs; a 0 interval must still never fire a resync tick. The
      # boot LIST catalogs the Workload and casts base_reconcile_fun ONCE (the
      # normal admission path); if the timer were armed despite
      # resync_interval_ms: 0, a second identical cast would show up within the
      # wait below.
      {:ok, watcher} =
        WorkloadWatcher.start_link(
          Keyword.merge(
            [
              name: nil,
              table: table,
              lister: fn -> {:ok, [valid_cr()]} end,
              watcher_fun: fn _rv, _emit -> Process.sleep(:infinity) end,
              status_writer: recording_status_writer(agent),
              resync_interval_ms: 0
            ],
            base_seams(agent: base)
          )
        )

      assert_eventually(fn -> Agent.get(base, & &1.reconciled) != [] end)
      Process.sleep(150)
      assert [_one] = Agent.get(base, & &1.reconciled)
      assert Process.alive?(watcher)
    end
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
