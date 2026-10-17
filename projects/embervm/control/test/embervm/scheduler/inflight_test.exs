defmodule Embervm.Scheduler.InflightTest do
  use ExUnit.Case, async: true

  alias Embervm.Scheduler.Inflight

  setup do
    table = Inflight.create(String.to_atom("inflight_test_#{System.unique_integer([:positive])}"))
    %{opts: [table: table], table: table}
  end

  defp at(opts, now), do: Keyword.put(opts, :now, now)

  test "claim debits the brick until release", %{opts: opts} do
    assert Inflight.pending_mib("n/a", opts) == 0

    :ok = Inflight.claim("n/a", :one, 3_072, opts)
    :ok = Inflight.claim("n/a", :two, 1_024, opts)
    :ok = Inflight.claim("n/b", :one, 512, opts)

    assert Inflight.pending_mib("n/a", opts) == 4_096
    assert Inflight.pending_mib("n/b", opts) == 512

    :ok = Inflight.release("n/a", :one, opts)
    assert Inflight.pending_mib("n/a", opts) == 1_024
    # The same ref on another brick is a different claim.
    assert Inflight.pending_mib("n/b", opts) == 512
  end

  test "re-claiming the same ref on a brick replaces the row", %{opts: opts} do
    :ok = Inflight.claim("n/a", :r, 1_000, opts)
    :ok = Inflight.claim("n/a", :r, 2_000, opts)
    assert Inflight.pending_mib("n/a", opts) == 2_000
  end

  test "a negative charge debits nothing", %{opts: opts} do
    :ok = Inflight.claim("n/a", :r, -5, opts)
    assert Inflight.pending_mib("n/a", opts) == 0
  end

  test "settle keeps the debit only for the hold", %{opts: opts} do
    :ok = Inflight.claim("n/a", :r, 3_072, at(opts, 0))
    :ok = Inflight.settle("n/a", :r, opts |> at(1_000) |> Keyword.put(:hold_ms, 5_000))

    assert Inflight.pending_mib("n/a", at(opts, 5_999)) == 3_072
    assert Inflight.pending_mib("n/a", at(opts, 6_000)) == 0
  end

  test "the default hold is 5 seconds and the default expiry 200 seconds", %{opts: opts} do
    assert Inflight.default_hold_ms() == 5_000
    assert Inflight.default_ttl_ms() == 200_000

    :ok = Inflight.claim("n/a", :settled, 100, at(opts, 0))
    :ok = Inflight.settle("n/a", :settled, at(opts, 0))
    :ok = Inflight.claim("n/a", :open, 10, at(opts, 0))

    assert Inflight.pending_mib("n/a", at(opts, 4_999)) == 110
    assert Inflight.pending_mib("n/a", at(opts, 5_000)) == 10
    assert Inflight.pending_mib("n/a", at(opts, 199_999)) == 10
    assert Inflight.pending_mib("n/a", at(opts, 200_000)) == 0
  end

  test "settle never extends a claim past its expiry and ignores a missing row", %{opts: opts} do
    :ok = Inflight.claim("n/a", :r, 100, opts |> at(0) |> Keyword.put(:ttl_ms, 1_000))
    :ok = Inflight.settle("n/a", :r, opts |> at(900) |> Keyword.put(:hold_ms, 5_000))
    assert Inflight.pending_mib("n/a", at(opts, 999)) == 100
    assert Inflight.pending_mib("n/a", at(opts, 1_000)) == 0

    :ok = Inflight.settle("n/a", :never_claimed, opts)
    assert Inflight.entries(at(opts, 0)) == [{"n/a", :r, 100, 1_000}]
  end

  test "an expired claim (a crashed claimer) is ignored and swept", %{opts: opts, table: table} do
    :ok = Inflight.claim("n/a", :crashed, 3_072, opts |> at(0) |> Keyword.put(:ttl_ms, 100))
    assert Inflight.pending_mib("n/a", at(opts, 99)) == 3_072
    assert Inflight.pending_mib("n/a", at(opts, 100)) == 0
    # Still physically present until a sweep.
    assert :ets.info(table, :size) == 1

    # A later claim sweeps expired rows.
    :ok = Inflight.claim("n/b", :live, 1, at(opts, 200))
    assert :ets.info(table, :size) == 1

    :ok = Inflight.claim("n/c", :old, 1, opts |> at(200) |> Keyword.put(:ttl_ms, 10))
    assert Inflight.sweep(at(opts, 210)) == 1
  end

  test "drop clears every claim on a brick the registry forgot", %{opts: opts} do
    :ok = Inflight.claim("n/a", :one, 1, opts)
    :ok = Inflight.claim("n/a", :two, 2, opts)
    :ok = Inflight.claim("n/b", :one, 4, opts)

    :ok = Inflight.drop("n/a", opts)
    assert Inflight.pending_mib("n/a", opts) == 0
    assert Inflight.pending_mib("n/b", opts) == 4
  end

  test "release_ref clears one ref on every brick it hopped to", %{opts: opts} do
    ref = {:task, "t-1", make_ref()}
    :ok = Inflight.claim("n/a", ref, 1, opts)
    :ok = Inflight.claim("n/b", ref, 2, opts)
    :ok = Inflight.claim("n/b", :other, 4, opts)

    :ok = Inflight.release_ref(ref, opts)
    assert Inflight.pending_mib("n/a", opts) == 0
    assert Inflight.pending_mib("n/b", opts) == 4
  end

  test "a missing table reads as no debit and never raises" do
    opts = [table: :inflight_test_missing_table]
    assert Inflight.pending_mib("n/a", opts) == 0
    assert :ok = Inflight.claim("n/a", :r, 1, opts)
    assert :ok = Inflight.settle("n/a", :r, opts)
    assert :ok = Inflight.release("n/a", :r, opts)
    assert :ok = Inflight.release_ref(:r, opts)
    assert :ok = Inflight.drop("n/a", opts)
    assert Inflight.entries(opts) == []
    assert Inflight.sweep(opts) == 0
  end

  test "create is idempotent", %{table: table} do
    assert Inflight.create(table) == table
  end

  describe "one count per boot" do
    test "a report drops the settled claims it follows, ordered by report sequence", %{opts: opts} do
      :ok = Inflight.claim("n/a", :landed, 3_136, opts)
      :ok = Inflight.claim("n/a", :flying, 3_136, opts)
      :ok = Inflight.settle("n/a", :landed, opts)
      assert Inflight.pending("n/a", opts) == {3_136, 3_136}

      # A report on another brick does not touch this one.
      :ok = Inflight.observe_report("n/b", opts)
      assert Inflight.pending("n/a", opts) == {3_136, 3_136}

      :ok = Inflight.observe_report("n/a", opts)
      assert Inflight.pending("n/a", opts) == {3_136, 0}

      # A claim settled after the latest report waits for the next one.
      :ok = Inflight.settle("n/a", :flying, opts)
      assert Inflight.pending("n/a", opts) == {0, 3_136}
      :ok = Inflight.observe_report("n/a", opts)
      assert Inflight.pending("n/a", opts) == {0, 0}
    end

    test "a brick reporting its ledger is charged only the claims it does not yet show", %{opts: opts} do
      reporting = %{
        admission_reported: true,
        admits_on_reservation: true,
        mem_inflight_mib: 3_072,
        inflight_vms: 1,
        mem_inflight_placed_mib: 3_072,
        inflight_placed_vms: 1,
        vm_overhead_mib: 64
      }

      :ok = Inflight.claim("n/a", :admitted, 3_136, opts)
      # The daemon already counts the admitted boot (need plus overhead).
      assert Inflight.debit_mib("n/a", reporting, opts) == 0

      :ok = Inflight.claim("n/a", :racing, 3_136, opts)
      assert Inflight.debit_mib("n/a", reporting, opts) == 3_136

      # Never below zero: placed boots beyond the claims still on the ledger
      # (one released while its boot carries on) add no headroom.
      busy = %{reporting | mem_inflight_placed_mib: 9_000, inflight_placed_vms: 3}
      assert Inflight.debit_mib("n/a", busy, opts) == 0

      # A settled claim counts in full until a report follows it.
      idle = %{reporting | mem_inflight_mib: 0, inflight_vms: 0, mem_inflight_placed_mib: 0, inflight_placed_vms: 0}
      :ok = Inflight.settle("n/a", :admitted, opts)
      assert Inflight.debit_mib("n/a", idle, opts) == 3_136 + 3_136
    end

    test "a boot the control plane did not place cancels none of its claims", %{opts: opts} do
      # An activator wake of 2048 MiB is in flight on a reserved brick: the
      # daemon's admittable already subtracts it, and it is in the totals but not
      # in the placed figure. A create the daemon has not admitted yet must stay
      # charged in full, or the next placement lands in its room (delta review,
      # ledger).
      wake_in_flight = %{
        admission_reported: true,
        admits_on_reservation: true,
        mem_inflight_mib: 2_048,
        inflight_vms: 1,
        mem_inflight_placed_mib: 0,
        inflight_placed_vms: 0,
        vm_overhead_mib: 64
      }

      :ok = Inflight.claim("n/a", :create, 2_112, opts)
      assert Inflight.debit_mib("n/a", wake_in_flight, opts) == 2_112

      # Once the daemon admits the create it shows in the placed figure, at its
      # declared size, and cancels exactly that claim.
      admitted = %{
        wake_in_flight
        | mem_inflight_mib: 4_096,
          inflight_vms: 2,
          mem_inflight_placed_mib: 2_048,
          inflight_placed_vms: 1
      }

      assert Inflight.debit_mib("n/a", admitted, opts) == 0
    end

    test "an observed brick's reported overhead cancels nothing, since no claim on it carries one", %{opts: opts} do
      # noded reports vm_overhead_mib under observed but charges none, so a claim
      # there is the bare need (Brick.charge_mib/2) and the daemon's in-flight
      # boot cancels exactly that.
      observed = %{
        admission_reported: true,
        admits_on_reservation: false,
        mem_inflight_mib: 3_072,
        inflight_vms: 1,
        mem_inflight_placed_mib: 3_072,
        inflight_placed_vms: 1,
        vm_overhead_mib: 64
      }

      :ok = Inflight.claim("n/a", :admitted, 3_072, opts)
      assert Inflight.debit_mib("n/a", observed, opts) == 0

      :ok = Inflight.claim("n/a", :racing, 3_072, opts)
      assert Inflight.debit_mib("n/a", observed, opts) == 3_072
    end

    test "a brick that does not report its ledger is charged every claim", %{opts: opts} do
      :ok = Inflight.claim("n/a", :one, 3_136, opts)
      :ok = Inflight.claim("n/a", :two, 1_000, opts)
      :ok = Inflight.settle("n/a", :two, opts)
      assert Inflight.debit_mib("n/a", %{admission_reported: false, mem_inflight_mib: 3_072}, opts) == 4_136
    end

    test "a report that does not charge landed VMs keeps settled claims until their hold", %{opts: opts} do
      now = 1_000
      at = Keyword.put(opts, :now, now)
      :ok = Inflight.claim("n/a", :landed, 3_072, at)
      :ok = Inflight.settle("n/a", :landed, Keyword.put(at, :hold_ms, 5_000))

      # An observed brick's report: the landed guest has not faulted its memory
      # in, so the report does not charge it and the settled claim stays.
      :ok = Inflight.observe_report("n/a", Keyword.put(at, :counts_landed, false))
      :ok = Inflight.observe_report("n/a", Keyword.put(at, :counts_landed, false))
      assert Inflight.pending("n/a", at) == {0, 3_072}

      # The hold still bounds it.
      assert Inflight.pending("n/a", Keyword.put(opts, :now, now + 5_000)) == {0, 0}

      # A reserved brick's report charges it through the live map, so it drops.
      :ok = Inflight.observe_report("n/a", at)
      assert Inflight.pending("n/a", at) == {0, 0}
    end

    test "drop forgets the brick's report sequence with its claims", %{opts: opts} do
      :ok = Inflight.observe_report("n/a", opts)
      :ok = Inflight.claim("n/a", :r, 100, opts)
      :ok = Inflight.drop("n/a", opts)
      assert Inflight.pending("n/a", opts) == {0, 0}
      assert :ets.lookup(opts[:table], {:report_seq, "n/a"}) == []
    end
  end
end
