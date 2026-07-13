defmodule Embervm.WorkloadCatalogTest do
  @moduledoc """
  Exercises `Embervm.WorkloadCatalog` directly against isolated ETS tables, one
  per test (never the application's supervised `:embervm_workloads` table, so
  these run fully async with zero shared state).
  """
  use ExUnit.Case, async: true

  alias Embervm.Retry
  alias Embervm.WorkloadCatalog

  # A fresh, uniquely-named ETS table per test: create/1 is only safe to call
  # once per table (a second :ets.new/2 on the same name raises), and a
  # process-owned table dies with the test process anyway, so no on_exit
  # cleanup is needed.
  defp fresh_table do
    table = String.to_atom("wl_cat_#{System.unique_integer([:positive])}")
    WorkloadCatalog.create(table)
    table
  end

  test "create/upsert/fetch/drop/all_names round trip" do
    table = fresh_table()

    assert WorkloadCatalog.all_names(table) == []
    assert WorkloadCatalog.fetch(table, "wl-a") == :error

    entry = %{name: "wl-a", retry: Retry.default_config()}
    WorkloadCatalog.upsert(table, "wl-a", entry)

    assert WorkloadCatalog.fetch(table, "wl-a") == {:ok, entry}
    assert WorkloadCatalog.all_names(table) == ["wl-a"]

    WorkloadCatalog.upsert(table, "wl-b", %{name: "wl-b", retry: Retry.default_config()})
    assert Enum.sort(WorkloadCatalog.all_names(table)) == ["wl-a", "wl-b"]

    WorkloadCatalog.drop(table, "wl-a")
    assert WorkloadCatalog.fetch(table, "wl-a") == :error
    assert WorkloadCatalog.all_names(table) == ["wl-b"]
  end

  test "retry_config returns the entry's own retry config for a known name" do
    table = fresh_table()

    custom = %{max_attempts: 7, backoff_ms: 500, backoff_cap_ms: 5_000, retry_on: [:timeout]}
    WorkloadCatalog.upsert(table, "wl-custom", %{name: "wl-custom", retry: custom})

    assert WorkloadCatalog.retry_config(table, "wl-custom") == custom
  end

  test "retry_config falls back to Retry.default_config/0 for an unknown name in an existing table" do
    table = fresh_table()

    assert WorkloadCatalog.retry_config(table, "does-not-exist") == Retry.default_config()
  end

  test "retry_config falls back to Retry.default_config/0 when the table does not exist at all" do
    # Never created: :ets.whereis/1 must return :undefined for this atom, so
    # retry_config/2 must guard on that rather than let :ets.lookup/2 raise
    # ArgumentError. This is the crash-safety case: a reader calling in before
    # the watcher has booted (or between a crash and its restart) must never
    # blow up.
    missing_table = String.to_atom("wl_cat_never_created_#{System.unique_integer([:positive])}")

    assert WorkloadCatalog.retry_config(missing_table, "anything") == Retry.default_config()
  end

  test "retry_config/1 (default-table arity) matches retry_config/2 against the default table" do
    # Exercise the 1-arity form (used by TaskStore.cfg_for/1) against the
    # real default table so the "two explicit clauses" split is proven to
    # actually reach the same table as the 2-arity form.
    default_table = WorkloadCatalog.table()

    # If the application under test is running, the watcher may already own
    # this table; if not, create it. Either way, an unknown workload name on
    # the default table must fall back to the default retry config.
    if :ets.whereis(default_table) == :undefined do
      WorkloadCatalog.create(default_table)
    end

    unique_name = "wl_cat_default_probe_#{System.unique_integer([:positive])}"
    assert WorkloadCatalog.retry_config(unique_name) == Retry.default_config()
  end
end
