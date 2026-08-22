defmodule Embervm.NodeAuthTest do
  use ExUnit.Case, async: false

  alias Embervm.NodeAuth

  setup do
    previous = Application.get_env(:embervm, :noded_bearer_token, :not_set)

    on_exit(fn ->
      case previous do
        :not_set -> Application.delete_env(:embervm, :noded_bearer_token)
        token -> Application.put_env(:embervm, :noded_bearer_token, token)
      end
    end)

    :ok
  end

  test "empty token attaches no connection header" do
    Application.put_env(:embervm, :noded_bearer_token, "  ")
    assert NodeAuth.connect_opts() == []
  end

  test "configured token attaches a bearer authorization header" do
    Application.put_env(:embervm, :noded_bearer_token, "  node-secret\n")

    assert NodeAuth.connect_opts() == [
             headers: [{"authorization", "Bearer node-secret"}]
           ]
  end
end
