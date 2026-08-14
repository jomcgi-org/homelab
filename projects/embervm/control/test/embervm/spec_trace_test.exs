defmodule Embervm.SpecTraceTest do
  use ExUnit.Case, async: false

  alias Embervm.SpecTrace
  alias Embervm.SpecTrace.Store.SQLite

  setup do
    System.put_env("EMBERVM_SPEC_TRACE", "on")
    SpecTrace.configure()

    # Restore the global `:persistent_term` gate, see the same note in
    # checker_test. This module leaks in a second way too: "disabled emission is
    # a no-op" flips the gate off mid-module, so without a restore the value a
    # later module inherits depends on which test happened to run last.
    on_exit(fn ->
      System.put_env("EMBERVM_SPEC_TRACE", "off")
      SpecTrace.configure()
    end)

    store = start_supervised!({SQLite, name: nil, path: ":memory:"})

    # The writer keeps its REGISTERED name deliberately. `SpecTrace.emit/3`
    # resolves the writer via `Process.whereis/1`, so an unnamed writer means
    # every emission silently finds nothing and the test exercises none of the
    # production path while still passing its own drain call. The store stays
    # unnamed because it is addressed by pid.
    writer = start_supervised!({SpecTrace.Writer, store_mod: SQLite, store: store, batch_size: 10, flush_ms: 10})
    {:ok, writer: writer, store: store}
  end

  # Booleans and nil survive as themselves, enum-like atoms become strings.
  #
  # This exists because the opposite shipped and was nearly merged. `jsonable/1`
  # matched on is_atom/1 first, and in Elixir true/false/nil ARE atoms, so every
  # boolean reached the store as "true"/"false". The destroy invariants compare
  # `vars["gate"]` against real booleans, so on live data the vacuous arm never
  # fired and the violation filter never matched: the checker returned PASS
  # without evaluating anything.
  #
  # Every test still passed. The checker fixtures were hand-built maps that never
  # went through the Writer, and the integration assertions had been changed to
  # expect the corrupted strings. Two test suites agreeing with each other and
  # both disagreeing with production.
  #
  # So assert the TYPE, through the real Writer, at the boundary where the
  # conversion happens. A fixture cannot catch a transport bug.
  test "preserves booleans and nil through the writer, stringifies other atoms", %{writer: writer, store: store} do
    SpecTrace.emit(:adoption, :confirm_destroy, %{
      "gate" => false,
      "node_confirmed" => true,
      "had_vm" => true,
      "vm_id" => nil,
      "provenance" => :adopted
    })

    :ok = SpecTrace.drain(writer)
    assert {:ok, records} = SQLite.read_window(store, action: "confirm_destroy")
    assert [record] = records

    assert record["vars"]["gate"] === false
    assert record["vars"]["node_confirmed"] === true
    assert record["vars"]["had_vm"] === true
    assert record["vars"]["vm_id"] === nil
    assert record["vars"]["provenance"] == "adopted"

    refute record["vars"]["gate"] == "false"
    refute record["vars"]["node_confirmed"] == "true"

    SpecTrace.emit(:adoption, :confirm_destroy, %{
      "gate" => true,
      "node_confirmed" => nil,
      "had_vm" => false
    })

    :ok = SpecTrace.drain(writer)
    assert {:ok, records} = SQLite.read_window(store, action: "confirm_destroy")
    record = List.last(records)
    assert record["vars"]["had_vm"] === false
    assert record["vars"]["node_confirmed"] === nil
  end

  test "round trips records and preamble", %{writer: writer, store: store} do
    SpecTrace.emit(:adoption, :prime, %{"vm_id" => "vm-1", lane: :task})
    :ok = SpecTrace.drain(writer)
    assert {:ok, records} = SQLite.read_window(store)
    assert Enum.count(records, &(&1["action"] == "preamble")) == 1
    record = Enum.find(records, &(&1["action"] == "prime"))
    assert record["spec"] == "adoption"
    assert record["vars"]["lane"] == "task"
    assert record["ts"] > 1_700_000_000_000
    assert abs(record["ts"] - System.system_time(:millisecond)) < 60_000
    assert record["mono"]
    preamble = Enum.find(records, &(&1["action"] == "preamble"))
    assert preamble["vars"]["enabled"]
    assert preamble["vars"]["schema_version"] == SpecTrace.schema_version()
    assert preamble["vars"]["ttl_ms"] == 86_400_000
  end

  test "filters and causal ordering", %{writer: writer, store: store} do
    SpecTrace.emit(:adoption, :prime, %{"i" => 1})
    SpecTrace.emit(:billing, :checkpoint, %{"i" => 2})
    :ok = SpecTrace.drain(writer)
    {:ok, all} = SQLite.read_window(store, action: "checkpoint")
    assert Enum.all?(all, &(&1["action"] == "checkpoint"))
    record = hd(all)
    assert {:ok, [^record]} = SQLite.read_window(store, run_id: record["run_id"], spec: "billing")
    assert {:ok, [_]} = SQLite.read_window(store, since_seq: record["seq"], since_ts_ms: record["ts"], until_ts_ms: record["ts"])
    assert all == Enum.sort_by(all, & &1["mono"])
  end

  test "disabled emission is a no-op" do
    System.put_env("EMBERVM_SPEC_TRACE", "off")
    SpecTrace.configure()
    assert SpecTrace.emit(:adoption, :prime, %{}) == :ok
  end

  test "sweep deletes in bounded batches", %{store: store} do
    records = for seq <- 1..3, do: %{"run_id" => "run", "seq" => seq, "mono" => seq, "ts" => 10, "spec" => "adoption", "action" => "prime", "vars" => %{}}
    assert :ok = SQLite.write(store, records)
    assert {:ok, %{deleted: 1, done: false}} = SQLite.sweep(store, now_ms: 100, ttl_ms: 1, batch_size: 1)
    assert {:ok, %{deleted: 1, done: false}} = SQLite.sweep(store, now_ms: 100, ttl_ms: 1, batch_size: 1)
    assert {:ok, %{deleted: 1, done: true}} = SQLite.sweep(store, now_ms: 100, ttl_ms: 1, batch_size: 1)
  end
end
