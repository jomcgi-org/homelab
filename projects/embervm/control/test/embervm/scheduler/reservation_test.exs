defmodule Embervm.Scheduler.ReservationTest do
  use ExUnit.Case, async: true

  alias Embervm.Scheduler.Reservation

  setup do
    table = String.to_atom("reservation_#{System.unique_integer([:positive, :monotonic])}")
    {:ok, server} = Reservation.start_link(name: nil, table: table, grace_age_ms: 100)
    %{server: server, table: table}
  end

  defp claim(server, instance_id, ref, opts \\ []) do
    Reservation.claim(server, instance_id, ref, Keyword.merge([workload: "wl", mem_mib: 100], opts))
  end

  test "claims sum across entries and instances", %{server: server, table: table} do
    claim(server, "n/a", "a", mem_mib: 100)
    claim(server, "n/a", "b", mem_mib: 200, count: 2)
    claim(server, "n/b", "c", mem_mib: 50)
    assert Reservation.reserved_mib("n/a", table) == 500
    assert Reservation.reserved_mib("n/b", table) == 50
  end

  test "claim is idempotent on ref", %{server: server, table: table} do
    claim(server, "n/a", "same", mem_mib: 100, now_ms: 10)
    claim(server, "n/a", "same", mem_mib: 300, count: 2, now_ms: 20)
    assert Reservation.reserved_mib("n/a", table) == 600
    assert [%{ref: "same", mem_mib: 300, count: 2}] = Reservation.entries("n/a", table)
  end

  test "duplicate shadow claim events remain one entry", %{server: server, table: table} do
    opts = [server: server, workload: "wl", mem_mib: 256, now_ms: 10]
    assert :ok = Reservation.claim_shadow("n/a", "same", opts)
    assert :ok = Reservation.claim_shadow("n/a", "same", opts)

    assert {:ok, %{released: []}} = Reservation.observe(server, "n/a", [%{vm_id: "same", workload: "wl"}], now_ms: 20, catalog: %{"wl" => 256})
    assert [%{ref: "same", mem_mib: 256, count: 1, claimed_at_ms: 10}] =
             Reservation.entries("n/a", table)
  end

  test "release removes and unknown release is a no-op", %{server: server, table: table} do
    claim(server, "n/a", "a")
    assert :ok = Reservation.release(server, "n/a", "missing")
    assert :ok = Reservation.release(server, "n/a", "a")
    assert Reservation.entries("n/a", table) == []
  end

  test "failed dispatch does not masquerade as confirmed teardown", %{server: server, table: table} do
    claim(server, "n/a", "vm", now_ms: 0)
    assert :ok = Reservation.release_confirmed("n/a", "vm", false, server: server)
    assert {:ok, []} = Reservation.reconcile(server, "n/a", ["vm"], 10)
    assert [%{ref: "vm"}] = Reservation.entries("n/a", table)

    assert :ok = Reservation.release_confirmed("n/a", "vm", true, server: server)
    assert {:ok, []} = Reservation.reconcile(server, "n/a", [], 10)
    assert Reservation.entries("n/a", table) == []
  end

  test "pool targets upsert and count zero removes", %{server: server, table: table} do
    assert :ok = Reservation.set_pool_target(server, "n/a", "wl", 3, 512)
    assert Reservation.reserved_mib("n/a", table) == 1536
    assert :ok = Reservation.set_pool_target(server, "n/a", "wl", 2, 256)
    assert Reservation.reserved_mib("n/a", table) == 512
    assert :ok = Reservation.set_pool_target(server, "n/a", "wl", 0, 256)
    assert Reservation.reserved_mib("n/a", table) == 0
  end

  test "reconcile releases old absent instance entries", %{server: server, table: table} do
    claim(server, "n/a", "old", now_ms: 0)
    assert {:ok, ["old"]} = Reservation.reconcile(server, "n/a", MapSet.new(), 101)
    assert Reservation.entries("n/a", table) == []
  end

  test "reconcile keeps young absent entries", %{server: server, table: table} do
    claim(server, "n/a", "young", now_ms: 50)
    assert {:ok, []} = Reservation.reconcile(server, "n/a", [], 100)
    assert Reservation.reserved_mib("n/a", table) == 100
  end

  test "node observations adopt live VMs and grace-delay absence GC", %{server: server, table: table} do
    live = [%{vm_id: "adopted", workload: "known"}]

    assert {:ok, %{adopted: 1, skipped: 0, released: []}} =
             Reservation.observe(server, "n/a", live, now_ms: 100, catalog: %{"known" => 384})

    assert {:ok, %{adopted: 0, skipped: 0, released: []}} =
             Reservation.observe(server, "n/a", live, now_ms: 120, catalog: %{"known" => 384})

    assert Reservation.reserved_mib("n/a", table) == 384

    assert {:ok, %{released: []}} =
             Reservation.observe(server, "n/a", [], now_ms: 199, catalog: %{"known" => 384})

    assert {:ok, %{released: ["adopted"]}} =
             Reservation.observe(server, "n/a", [], now_ms: 200, catalog: %{"known" => 384})

    assert Reservation.entries("n/a", table) == []
  end

  test "reconcile never absence-collects pool targets", %{server: server, table: table} do
    Reservation.set_pool_target(server, "n/a", "wl", 3, 512)
    assert {:ok, []} = Reservation.reconcile(server, "n/a", [], 100_000)
    assert Reservation.reserved_mib("n/a", table) == 1536
  end

  test "reconcile stamps confirmation once", %{server: server, table: table} do
    claim(server, "n/a", "vm", now_ms: 1)
    assert {:ok, []} = Reservation.reconcile(server, "n/a", ["vm"], 20)
    [%{confirmed_at_ms: 20}] = Reservation.entries("n/a", table)
    assert {:ok, []} = Reservation.reconcile(server, "n/a", ["vm"], 30)
    [%{confirmed_at_ms: 20}] = Reservation.entries("n/a", table)
  end

  test "drop_instance removes instances and pool targets", %{server: server, table: table} do
    claim(server, "n/a", "vm")
    Reservation.set_pool_target(server, "n/a", "wl", 2, 512)
    assert :ok = Reservation.drop_instance(server, "n/a")
    assert Reservation.entries("n/a", table) == []
  end

  test "shadow calls are non-refusing while the ledger is unavailable" do
    missing = String.to_atom("reservation_missing_#{System.unique_integer([:positive])}")

    assert :ok = Reservation.claim_shadow("n/a", "vm", server: missing, workload: "wl", mem_mib: 128)
    assert :ok = Reservation.set_pool_target_shadow("n/a", "wl", 2, 128, server: missing)
    assert :ok = Reservation.observe_shadow("n/a", [], server: missing)
    assert :ok = Reservation.drop_instance_shadow("n/a", server: missing)
  end

  test "adoption skips workloads absent from the catalog and keeps the server alive", %{
    server: server,
    table: table
  } do
    assert {:ok, %{adopted: 2, skipped: 1}} =
             Reservation.adopt(
               "n/a",
               [{"vm-1", "known-a"}, {"vm-2", "missing"}, {"vm-3", "known-b"}],
               server: server,
               catalog: %{"known-a" => 128, "known-b" => 256}
             )

    assert Process.alive?(server)
    assert Reservation.reserved_mib("n/a", table) == 384
    assert :ok = claim(server, "n/a", "subsequent-claim")
  end

  test "fresh table is empty and fail-closed", %{table: table} do
    assert Reservation.reserved_mib("missing", table) == 0
    assert Reservation.entries("missing", table) == []
    assert Reservation.all(table) == %{}
  end
end
