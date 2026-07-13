defmodule Embervm.ExqliteSmokeTest do
  @moduledoc """
  The hex-dependency de-risk gate: proves the exqlite C NIF (bundled sqlite3.c)
  compiled from source hermetically on the offline RBE executor and loads and
  round-trips a query. If this passes, the toolchain can carry the SQLite-WAL
  op-log (Task 6). Uses the low-level Exqlite.Sqlite3 API directly (no
  db_connection pool): the op-log is a single-writer GenServer that owns one
  connection, so pooling is neither needed nor wanted.
  """
  use ExUnit.Case, async: true

  alias Exqlite.Sqlite3

  test "exqlite NIF loads and round-trips an in-memory database" do
    {:ok, conn} = Sqlite3.open(":memory:")
    :ok = Sqlite3.execute(conn, "create table t(x integer)")
    :ok = Sqlite3.execute(conn, "insert into t(x) values (41), (42)")

    {:ok, stmt} = Sqlite3.prepare(conn, "select sum(x) from t")
    assert {:row, [83]} = Sqlite3.step(conn, stmt)
    assert :done = Sqlite3.step(conn, stmt)

    :ok = Sqlite3.release(conn, stmt)
    :ok = Sqlite3.close(conn)
  end

  test "sqlite supports WAL journal mode (the op-log durability backend)" do
    path = Path.join(System.tmp_dir!(), "embervm_wal_smoke_#{System.unique_integer([:positive])}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, conn} = Sqlite3.open(path)
    :ok = Sqlite3.execute(conn, "PRAGMA journal_mode=WAL")

    {:ok, stmt} = Sqlite3.prepare(conn, "PRAGMA journal_mode")
    assert {:row, ["wal"]} = Sqlite3.step(conn, stmt)

    :ok = Sqlite3.release(conn, stmt)
    :ok = Sqlite3.close(conn)
  end
end
