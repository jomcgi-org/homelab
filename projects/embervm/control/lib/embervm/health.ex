defmodule Embervm.Health do
  @moduledoc """
  Dependency-free HTTP/1.1 health endpoint over `:gen_tcp`.

  Answers `GET /healthz` with `200 OK` and any other request with `404`. This
  exists so the R0 skeleton proves the OTP release builds, boots, and serves in
  the cluster (the Task 1 acceptance gate) WITHOUT pulling a web-server hex
  dependency into the first Bazel/apko build. A real router (the submit API)
  replaces this in Task 8; the process contract (a supervised child listening on
  the configured port) stays the same.
  """
  use GenServer
  require Logger

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @impl true
  def init(opts) do
    port = Keyword.fetch!(opts, :port)

    listen_opts = [
      :binary,
      packet: :http_bin,
      active: false,
      reuseaddr: true,
      backlog: 128
    ]

    case :gen_tcp.listen(port, listen_opts) do
      {:ok, listen_socket} ->
        # Accept in a separate linked process so the GenServer stays responsive.
        # If the acceptor crashes it takes the GenServer down and the supervisor
        # restarts the whole listener, which is the desired fail-and-restart.
        acceptor = spawn_link(fn -> accept_loop(listen_socket) end)
        Logger.info("embervm health endpoint listening on port #{port}")
        {:ok, %{listen_socket: listen_socket, acceptor: acceptor, port: port}}

      {:error, reason} ->
        {:stop, {:listen_failed, reason}}
    end
  end

  @impl true
  def terminate(_reason, %{listen_socket: socket}) do
    :gen_tcp.close(socket)
    :ok
  end

  # Accept connections forever, handling each in its own short-lived process so a
  # slow or misbehaving client never blocks the accept loop.
  defp accept_loop(listen_socket) do
    case :gen_tcp.accept(listen_socket) do
      {:ok, socket} ->
        spawn(fn -> handle_connection(socket) end)
        accept_loop(listen_socket)

      {:error, :closed} ->
        # Listen socket closed (shutdown); stop the loop.
        :ok

      {:error, reason} ->
        Logger.warning("embervm health accept error: #{inspect(reason)}")
        accept_loop(listen_socket)
    end
  end

  # Read the request line (parsed by the :http_bin packet mode) and respond. Only
  # the method and path matter for a health check; the body and remaining headers
  # are ignored and the connection is closed after one response (no keep-alive).
  defp handle_connection(socket) do
    response =
      case :gen_tcp.recv(socket, 0, 5_000) do
        {:ok, {:http_request, :GET, {:abs_path, "/healthz"}, _version}} ->
          body = "ok"
          "HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: #{byte_size(body)}\r\nconnection: close\r\n\r\n#{body}"

        {:ok, {:http_request, _method, _path, _version}} ->
          "HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"

        _ ->
          "HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
      end

    _ = :gen_tcp.send(socket, response)
    :gen_tcp.close(socket)
  end
end
