defmodule Embervm.OpLog.ProjectionParityTest do
  use ExUnit.Case, async: true

  alias Embervm.OpLog.{Postgres, SQLite}

  test "SQLite and Postgres explicitly handle the same closed op kinds" do
    assert SQLite.projected_kinds() == Postgres.projected_kinds()

    for kind <- SQLite.projected_kinds() do
      assert kind in Embervm.OpLog.kinds()
    end
  end
end
