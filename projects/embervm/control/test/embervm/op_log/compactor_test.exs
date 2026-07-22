defmodule Embervm.OpLog.CompactorTest do
  @moduledoc """
  Covers the Compactor's handling of `db_size/1` returning
  `{:error, :not_supported}` (PR-4, #18/#27): `Embervm.OpLog.Postgres` has no
  single PVC file to stat, so a sweep against it must complete and log a
  summary with the size field omitted rather than crash or warn. Uses a fake
  op-log module (not a real Postgres or SQLite backend) so this stays a pure
  unit test of the Compactor's own dispatch, matching the plan's "no live
  Postgres in CI" constraint.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Compactor

  defmodule FakeOpLogNotSupported do
    @moduledoc "compact/2 succeeds trivially; db_size/1 mirrors Embervm.OpLog.Postgres."
    def compact(_server, _now_ms) do
      {:ok,
       %{
         results_deleted: 0,
         tasks_compacted: 0,
         sessions_compacted: 0,
         serving_instances_compacted: 0,
         stateful_instances_compacted: 0,
         group_instances_compacted: 0,
         ops_compacted: 0,
         compacted_through: 0,
         done: true
       }}
    end

    def db_size(_server), do: {:error, :not_supported}
  end

  test "a sweep against a db_size-unsupported backend completes without crashing" do
    {:ok, pid} =
      Compactor.start_link(
        name: nil,
        op_log: :fake_server,
        op_log_mod: FakeOpLogNotSupported,
        # A long interval: this test drives :sweep directly rather than waiting.
        interval_ms: 3_600_000
      )

    send(pid, :sweep)

    # No assertion on the log line itself (ExUnit captures logs separately);
    # the process staying alive after handling :sweep is the assertion that
    # the {:error, :not_supported} branch did not crash the GenServer.
    assert Process.alive?(pid)
    :ok = GenServer.stop(pid)
  end
end
