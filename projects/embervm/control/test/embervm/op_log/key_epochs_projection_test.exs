defmodule Embervm.OpLog.KeyEpochsProjectionTest do
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Op
  alias Embervm.OpLog.SQLite

  test "both key facts project into key_epochs" do
    path =
      Path.join(System.tmp_dir!(), "embervm_key_epochs_#{System.unique_integer([:positive])}.db")

    on_exit(fn -> File.rm_rf!(path) end)
    {:ok, op_log} = SQLite.start_link(name: nil, path: path)

    assert {:ok, _seq} =
             SQLite.append(op_log, %Op{
               kind: :key_epoch_set,
               tenant: "test",
               principal: "principal-1",
               ts: 100,
               payload: %{principal: "principal-1", epoch: 4, reason: "rotate"}
             })

    assert {:ok, _seq} =
             SQLite.append(op_log, %Op{
               kind: :key_min_epoch_raised,
               tenant: "test",
               principal: "principal-1",
               ts: 101,
               payload: %{principal: "principal-1", min_epoch: 3, reason: "revoke"}
             })

    assert {:ok,
            [
              %{
                principal: "principal-1",
                current_epoch: 4,
                min_epoch: 3,
                updated_at: 101
              }
            ]} = SQLite.load_key_epochs(op_log)
  end
end
