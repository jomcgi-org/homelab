defmodule Embervm.StoreProbe do
  @moduledoc """
  Periodically proves that the control plane can reach its HTTPS artifact store.

  The probe uses the same `Embervm.S3Client` and shared `Embervm.Finch` pool as
  restore reads. The object is deliberately allowed to be absent: a 404, or any
  other HTTP response, proves that TLS peer and hostname verification completed.
  Empty and plaintext endpoints are disabled because they have no public TLS
  trust path to check.

  Results are current-state observations rather than a latch. Each completed
  check replaces the previous result, so repaired trust becomes visible without
  restarting the pod. A new timer is armed only after a check completes, which
  prevents overlapping requests when the store is slow.
  """
  use GenServer

  require Logger

  @default_interval_ms 300_000
  @probe_key "probe/.keep"

  @type status :: %{
          state: :ok | :degraded | :disabled,
          reason: String.t() | nil,
          last_ok_at: String.t() | nil,
          last_checked_at: String.t() | nil
        }

  def start_link(opts) do
    name = Keyword.get(opts, :name, __MODULE__)
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  @doc "Returns the latest completed store trust observation."
  @spec status(GenServer.server()) :: status()
  def status(server \\ __MODULE__), do: GenServer.call(server, :status)

  @impl true
  def init(opts) do
    client = Keyword.get(opts, :client)
    endpoint = endpoint(client)

    state = %{
      client: client,
      endpoint: endpoint,
      interval_ms: Keyword.get(opts, :interval_ms, @default_interval_ms),
      s3_client: Keyword.get(opts, :s3_client, Embervm.S3Client),
      status: initial_status(client, endpoint)
    }

    if state.status.state != :disabled, do: send(self(), :probe)
    {:ok, state}
  end

  @impl true
  def handle_call(:status, _from, state), do: {:reply, state.status, state}

  @impl true
  def handle_info(:probe, state) do
    checked_at = timestamp()
    result = safe_get(state.s3_client, state.client)
    status = classify(result, checked_at, state.status.last_ok_at)

    emit_transition(state.status, status, state.endpoint)
    Process.send_after(self(), :probe, state.interval_ms)

    {:noreply, %{state | status: status}}
  end

  defp initial_status(nil, _endpoint), do: disabled_status()
  defp initial_status(_client, "http://" <> _rest), do: disabled_status()

  defp initial_status(_client, _endpoint) do
    %{state: :degraded, reason: "not checked", last_ok_at: nil, last_checked_at: nil}
  end

  defp disabled_status do
    %{state: :disabled, reason: nil, last_ok_at: nil, last_checked_at: timestamp()}
  end

  defp safe_get(s3_client, client) do
    s3_client.get(client, @probe_key)
  rescue
    error -> {:error, {:raised, error}}
  catch
    kind, reason -> {:error, {kind, reason}}
  end

  defp classify({:ok, _body}, checked_at, _last_ok_at), do: ok_status(checked_at)
  defp classify({:error, :not_found}, checked_at, _last_ok_at), do: ok_status(checked_at)

  defp classify({:error, {:unexpected_status, status}}, checked_at, _last_ok_at)
       when is_integer(status),
       do: ok_status(checked_at)

  defp classify({:error, reason}, checked_at, last_ok_at) do
    %{
      state: :degraded,
      reason: inspect(reason),
      last_ok_at: last_ok_at,
      last_checked_at: checked_at
    }
  end

  defp classify(other, checked_at, last_ok_at) do
    %{
      state: :degraded,
      reason: "unexpected result: #{inspect(other)}",
      last_ok_at: last_ok_at,
      last_checked_at: checked_at
    }
  end

  defp ok_status(checked_at) do
    %{state: :ok, reason: nil, last_ok_at: checked_at, last_checked_at: checked_at}
  end

  defp emit_transition(_previous, %{state: :degraded, reason: reason}, endpoint) do
    Logger.warning("embervm store probe: store fetch failed", reason: reason, endpoint: endpoint)
  end

  defp emit_transition(%{state: :degraded, reason: "not checked"}, %{state: :ok}, _endpoint), do: :ok

  defp emit_transition(%{state: :degraded}, %{state: :ok}, endpoint) do
    Logger.info("embervm store probe: store fetch recovered", endpoint: endpoint)
  end

  defp emit_transition(_previous, _current, _endpoint), do: :ok

  defp endpoint(%{endpoint: endpoint}) when is_binary(endpoint), do: endpoint
  defp endpoint(_client), do: nil

  defp timestamp do
    DateTime.utc_now() |> DateTime.truncate(:second) |> DateTime.to_iso8601()
  end
end
