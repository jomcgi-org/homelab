defmodule Embervm.CellTest do
  use ExUnit.Case, async: true

  alias Embervm.{Cell, WorkloadCatalog, WorkloadWatcher}
  alias Embervm.OpLog.{Op, SQLite}

  defp unique_table(prefix) do
    String.to_atom("#{prefix}_#{System.unique_integer([:positive, :monotonic])}")
  end

  defp workload(cell_id) do
    %{
      "metadata" => %{"name" => "routed-workload", "namespace" => "embervm", "generation" => 1},
      "spec" => %{
        "cellId" => cell_id,
        "class" => "task",
        "source" => %{"image" => %{"ref" => "example.invalid/workload", "port" => 8080}},
        "resources" => %{"vcpus" => 1, "memMib" => 256},
        "concurrency" => %{"floor" => 0, "cap" => 1}
      }
    }
  end

  defp start_watcher(cell_id, durable_owner, cr, parent) do
    table = unique_table("cell_catalog")
    assignments = unique_table("cell_assignments")

    {:ok, watcher} =
      WorkloadWatcher.start_link(
        name: nil,
        table: table,
        assignment_table: assignments,
        cell_id: cell_id,
        known_cell_ids: ["cell-a", "cell-b"],
        load_assignments_fun: fn ->
          {:ok,
           [
             %{
               workload: "routed-workload",
               cell_id: durable_owner,
               created_at: 1,
               updated_at: 1
             }
           ]}
        end,
        claim_assignment_fun: fn "routed-workload", _requested -> {:ok, durable_owner} end,
        lister: fn -> {:ok, [cr]} end,
        status_writer: fn _namespace, _name, status ->
          send(parent, {:status, cell_id, status})
          :ok
        end,
        base_reconcile_fun: fn descriptor ->
          send(parent, {:base_reconcile, cell_id, descriptor.name})
          :ok
        end,
        base_forget_fun: fn _name -> :ok end,
        watch_startup: false
      )

    {watcher, table, assignments}
  end

  test "durable assignment routes only through its recorded cell and rebuilds independently" do
    cr = workload("cell-a")
    {cell_a, catalog_a, assignments_a} = start_watcher("cell-a", "cell-a", cr, self())
    {cell_b, catalog_b, assignments_b} = start_watcher("cell-b", "cell-a", cr, self())

    assert :ok = WorkloadWatcher.reconcile_now(cell_a)
    assert :ok = WorkloadWatcher.reconcile_now(cell_b)

    assert {:ok, %{cell_id: "cell-a"}} = WorkloadCatalog.fetch(catalog_a, "routed-workload")
    assert WorkloadCatalog.fetch(catalog_b, "routed-workload") == :error
    assert Cell.route("routed-workload", assignments_a, "cell-a") == :owned

    assert Cell.route("routed-workload", assignments_b, "cell-b") ==
             {:error, {:wrong_cell, "cell-a"}}

    assert_receive {:base_reconcile, "cell-a", "routed-workload"}
    refute_receive {:base_reconcile, "cell-b", "routed-workload"}

    GenServer.stop(cell_a)

    {rebuilt, rebuilt_catalog, rebuilt_assignments} =
      start_watcher("cell-a", "cell-a", cr, self())

    assert :ok = WorkloadWatcher.reconcile_now(rebuilt)
    assert {:ok, %{cell_id: "cell-a"}} = WorkloadCatalog.fetch(rebuilt_catalog, "routed-workload")
    assert Cell.route("routed-workload", rebuilt_assignments, "cell-a") == :owned

    GenServer.stop(cell_b)
    GenServer.stop(rebuilt)
  end

  test "an inconsistent CR cannot replace the durable owner" do
    {watcher, catalog, assignments} =
      start_watcher("cell-b", "cell-a", workload("cell-b"), self())

    assert :ok = WorkloadWatcher.reconcile_now(watcher)
    assert WorkloadCatalog.fetch(catalog, "routed-workload") == :error
    assert Cell.owner("routed-workload", assignments) == {:ok, "cell-a"}

    assert_receive {:status, "cell-b", %{"conditions" => [condition]}}
    assert condition["reason"] == "CellAssignmentImmutable"
  end

  test "SQLite persists immutable assignment and stamps replay records with its cell" do
    path = Path.join(System.tmp_dir!(), "embervm_cell_#{System.unique_integer([:positive, :monotonic])}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(path: path, name: nil, cell_id: "cell-a")
    assert SQLite.claim_workload(op_log, "routed-workload", "cell-a") == {:ok, "cell-a"}
    assert SQLite.claim_workload(op_log, "routed-workload", "cell-b") == {:ok, "cell-a"}

    assert {:ok, 1} =
             SQLite.append(op_log, %Op{
               kind: :denied,
               tenant: "tenant",
               workload: "routed-workload",
               ts: 1,
               payload: %{reason: "test"}
             })

    assert {:ok, [%Op{cell_id: "cell-a", workload: "routed-workload"}]} =
             SQLite.read_from(op_log, 0)

    GenServer.stop(op_log)

    assert {:error, {:open_failed, {:wrong_cell, "cell-a"}}} =
             SQLite.start_link(path: path, name: nil, cell_id: "cell-b")

    {:ok, rebuilt} = SQLite.start_link(path: path, name: nil, cell_id: "cell-a")

    assert {:ok, [%{workload: "routed-workload", cell_id: "cell-a"}]} =
             SQLite.load_workload_cells(rebuilt)
  end

  test "unknown and inactive assignments fail closed" do
    table = unique_table("cell_route")
    Cell.create(table)
    Cell.put(table, "foreign", "cell-b", true)
    Cell.put(table, "deleted", "cell-a", false)

    assert Cell.route("foreign", table, "cell-a") == {:error, {:wrong_cell, "cell-b"}}
    assert Cell.route("deleted", table, "cell-a") == {:error, :unknown_workload}
    assert Cell.route("missing", table, "cell-a") == {:error, :unknown_assignment}
  end
end
