defmodule Embervm.OpLog.PostgresTest do
  @moduledoc """
  Compile-time + no-connection coverage for `Embervm.OpLog.Postgres` (PR-4,
  #18/#27). CI has no Postgres service for the control plane today (see the
  fleet-finish plan's PR-4 section), so this deliberately does NOT stand one
  up: a full round-trip conformance suite (re-running the SQLite test suite's
  scenarios against a real Postgres) is that plan's acceptance bar for the
  future DSN-cutover PR, not this one. What IS verifiable without a live
  database: the module satisfies `@behaviour Embervm.OpLog` (a compile-time,
  CI-visible check via `mix compile --warnings-as-errors` catching a missing
  callback), and `db_size/1` is a plain function that never dials out.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Postgres

  test "implements every Embervm.OpLog callback" do
    behaviours = Postgres.__info__(:attributes) |> Keyword.get_values(:behaviour) |> List.flatten()
    assert Embervm.OpLog in behaviours

    # The exact callback closure (op_log.ex): asserted by name+arity so a
    # callback silently dropped from the adapter fails here, not just via the
    # (also-real) @behaviour compile warning.
    expected = [
      {:append, 2},
      {:read_from, 2},
      {:load_tasks, 1},
      {:load_sessions, 1},
      {:load_serving_instances, 1},
      {:load_stateful_instances, 1},
      {:load_volumes, 1},
      {:load_volume_blessing, 1},
      {:load_blessing_leases, 1},
      {:load_checkpoint_dispatches, 1},
      {:load_group_instances, 1},
      {:load_group_members, 1},
      {:load_result, 2},
      {:load_request, 2},
      {:list_usage, 2},
      {:compact, 2},
      {:compacted_through, 1},
      {:evict_task, 2}
    ]

    exported = Postgres.__info__(:functions)

    for {name, arity} <- expected do
      assert {name, arity} in exported, "missing callback #{name}/#{arity}"
    end
  end

  test "db_size/1 is not supported for the Postgres backend (no single PVC file to stat)" do
    # No connection is started; db_size/1 never touches the GenServer or dials
    # out, so this is safe to call against an address with nothing listening.
    assert Postgres.db_size(:no_such_server) == {:error, :not_supported}
  end
end
