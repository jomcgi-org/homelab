defmodule Embervm.WorkloadWatcherTest do
  @moduledoc """
  Exercises `Embervm.WorkloadWatcher`'s reconcile loop end to end: a canned
  `lister` stands in for `Embervm.K8s.list_workloads/0`, an `Agent`-backed
  `status_writer` stands in for `Embervm.K8s.patch_workload_status/3` and
  records every call for assertions, and `reconcile_interval_ms: nil` means
  no timer ever fires on its own, every reconcile is driven explicitly via
  `reconcile_now/1`. Every test uses a unique table name and an unnamed
  (`name: nil`) watcher process so tests run fully async with zero shared
  state, and never touch the application's own supervised watcher/table.
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

  defp start_watcher(lister, status_writer, table) do
    {:ok, pid} =
      WorkloadWatcher.start_link(
        name: nil,
        table: table,
        lister: lister,
        status_writer: status_writer,
        reconcile_interval_ms: nil
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

  test "add: a valid CR is cataloged with parsed retry, status reports Ready=False/BaseNotBuilt" do
    table = unique_table()
    agent = start_recorder()
    lister = fn -> {:ok, [valid_cr()]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

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

    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert status_map["observedGeneration"] == 1
    assert [%{"type" => "Ready", "status" => "False", "reason" => "BaseNotBuilt"}] = status_map["conditions"]
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
    cr = valid_cr(%{"spec" => %{"class" => "session"}})
    lister = fn -> {:ok, [cr]} end
    watcher = start_watcher(lister, recording_status_writer(agent), table)

    :ok = WorkloadWatcher.reconcile_now(watcher)

    assert WorkloadCatalog.fetch(table, "semgrep") == :error

    assert {_ns, "semgrep", status_map} = ready_status(recorded_calls(agent), "semgrep")
    assert [%{"type" => "Ready", "status" => "False", "reason" => "ClassUnsupported"}] = status_map["conditions"]

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
end
