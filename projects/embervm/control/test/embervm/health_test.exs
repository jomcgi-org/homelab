defmodule Embervm.HealthTest do
  @moduledoc """
  The trivial passing ExUnit target Task 1 requires under `bazel test`. `mix test`
  starts the :embervm application, which boots Embervm.Health on its default port,
  so this round-trips a real TCP request against the already-running listener
  (starting a second instance would collide on the registered name). It proves the
  supervision-tree wiring and the dependency-free HTTP path together.
  """
  use ExUnit.Case, async: false

  # The app boots Embervm.Health on EMBERVM_HTTP_PORT or 8080 by default; tests
  # run with neither set, so the listener is on 8080.
  @port 8080

  test "GET /healthz returns 200 ok" do
    assert {status, body} = http_get(@port, "/healthz")
    assert status == 200
    assert body == "ok"
  end

  test "unknown path returns 404" do
    assert {status, _body} = http_get(@port, "/nope")
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
