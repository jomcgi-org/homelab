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
    assert NodeAuth.connect_opts() == [adapter_opts: [transport_opts: [timeout: 3_000]]]
  end

  test "configured token attaches a bearer authorization header" do
    Application.put_env(:embervm, :noded_bearer_token, "  node-secret\n")

    assert NodeAuth.connect_opts() == [
             adapter_opts: [transport_opts: [timeout: 3_000]],
             headers: [{"authorization", "Bearer node-secret"}]
           ]
  end

  test "connect options bound Mint's TCP connection establishment" do
    assert get_in(NodeAuth.connect_opts(), [:adapter_opts, :transport_opts, :timeout]) == 3_000
    assert NodeAuth.connect_timeout_ms() == 3_000
  end
end
