defmodule Embervm.HealthTest do
  @moduledoc """
  The trivial passing ExUnit target Task 1 requires under `bazel test`. It boots
  the health listener on an ephemeral port and round-trips a real TCP request, so
  it proves both the supervision-tree wiring and the dep-free HTTP path.
  """
  use ExUnit.Case, async: false

  setup do
    # Port 0 lets the OS pick a free port; read it back off the listen socket via
    # a fixed test port to keep the client simple.
    port = 8099
    start_supervised!({Embervm.Health, port: port})
    {:ok, port: port}
  end

  test "GET /healthz returns 200 ok", %{port: port} do
    assert {status, body} = http_get(port, "/healthz")
    assert status == 200
    assert body == "ok"
  end

  test "unknown path returns 404", %{port: port} do
    assert {status, _body} = http_get(port, "/nope")
    assert status == 404
  end

  # Minimal HTTP/1.1 GET over a raw TCP socket; parses the status line and body.
  defp http_get(port, path) do
    {:ok, socket} = :gen_tcp.connect(~c"127.0.0.1", port, [:binary, active: false], 2_000)
    request = "GET #{path} HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n"
    :ok = :gen_tcp.send(socket, request)
    raw = recv_all(socket, "")
    :gen_tcp.close(socket)

    [status_line | _] = String.split(raw, "\r\n", parts: 2)
    [_http, code | _] = String.split(status_line, " ")
    body = raw |> String.split("\r\n\r\n", parts: 2) |> List.last()
    {String.to_integer(code), body}
  end

  defp recv_all(socket, acc) do
    case :gen_tcp.recv(socket, 0, 2_000) do
      {:ok, data} -> recv_all(socket, acc <> data)
      {:error, :closed} -> acc
      {:error, _} -> acc
    end
  end
end
