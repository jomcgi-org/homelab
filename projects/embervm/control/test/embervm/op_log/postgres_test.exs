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

  @dsn "postgres://embervm:secret@monolith-pg-rw.monolith.svc.cluster.local:5432/embervm_oplog"

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
      {:load_key_epochs, 1},
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

  describe "connect_opts/1" do
    test "parses the DSN into Postgrex's keyword shape" do
      opts = Postgres.connect_opts(@dsn)

      assert opts[:hostname] == "monolith-pg-rw.monolith.svc.cluster.local"
      assert opts[:port] == 5432
      assert opts[:username] == "embervm"
      assert opts[:password] == "secret"
      assert opts[:database] == "embervm_oplog"
      # nil name: this connection is owned by the GenServer, never registered.
      assert opts[:name] == nil
    end

    test "keeps the connection breathing so an idle flow is not reaped" do
      # The op-log connection sits idle between appends, sometimes for an hour.
      # Cilium is eBPF L4 on this path, so a flow with no packets has its
      # conntrack entry collected, and the next INSERT hangs instead of failing.
      # That took the control plane down twice on 2026-08-23 (see the issue this
      # test cites). SO_KEEPALIVE alone does not help: Linux would not probe for
      # 7200 seconds. The raw options are what make the probe land in time, so
      # assert the values and not merely that keepalive is on.
      socket_options = Postgres.connect_opts(@dsn)[:socket_options]

      assert {:keepalive, true} in socket_options
      # IPPROTO_TCP 6; TCP_KEEPIDLE 4, TCP_KEEPINTVL 5, TCP_KEEPCNT 6 (Linux).
      assert {:raw, 6, 4, <<60::native-32>>} in socket_options
      assert {:raw, 6, 5, <<15::native-32>>} in socket_options
      assert {:raw, 6, 6, <<4::native-32>>} in socket_options
    end

    test "the append budget outlives the database's own timeout" do
      # Ordering, not magnitude, is the property. The old append budget was the
      # implicit GenServer 5000, which expired BEFORE the database gave up: the
      # caller died while its transaction might still commit, and because the
      # op-log is the first child under :rest_for_one, every store behind it
      # restarted too. Whichever way these two numbers are tuned, the database
      # has to be the one that decides.
      assert Postgres.append_timeout_ms() > Postgres.connect_opts(@dsn)[:timeout]
    end
  end
end
