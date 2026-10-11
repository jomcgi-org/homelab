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
end
