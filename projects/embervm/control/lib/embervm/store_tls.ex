defmodule Embervm.StoreTLS do
  @moduledoc """
  Boot-time object-store TLS verification and restore-storm suppression.

  A configured store is probed once after `Embervm.StoreFinch` starts. Failure
  is observable but never prevents the control plane from booting. When that
  failure is a TLS error, control-plane store reads used to mint restore
  capabilities are refused before the S3 client's retry loop.
  """

  use GenServer
  require Logger

  alias Embervm.S3Client

  @type snapshot ::
          %{store_tls: String.t()}
          | %{store_tls: String.t(), store_tls_error: String.t()}

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @spec snapshot(GenServer.server()) :: snapshot()
  def snapshot(server \\ __MODULE__), do: GenServer.call(server, :snapshot)

  @doc """
  Permit a control-plane store read, or refuse it after a failed TLS probe.

  The refusal warning is emitted once per workload for this probe failure. A
  control-plane restart creates a new probe result and a new warning budget.
  """
  @spec permit_restore(String.t()) :: :ok | {:error, term()}
  def permit_restore(workload), do: permit_restore(__MODULE__, workload)

  @spec permit_restore(GenServer.server(), String.t()) :: :ok | {:error, term()}
  def permit_restore(server, workload), do: GenServer.call(server, {:permit_restore, workload})

  @impl true
  def init(opts) do
    endpoint = Keyword.get(opts, :endpoint, "") || ""
    bucket = Keyword.get(opts, :bucket, "embervm")

    client =
      S3Client.new(endpoint, bucket,
        access_key_id: Keyword.get(opts, :access_key_id),
        secret_access_key: Keyword.get(opts, :secret_access_key),
        finch: Keyword.get(opts, :finch, Embervm.StoreFinch)
      )

    probe = Keyword.get(opts, :probe, fn -> S3Client.probe(client) end)

    state = %{
      endpoint: endpoint,
      probe: probe,
      status: if(client, do: :pending, else: :unconfigured),
      error: nil,
      tls_failed: false,
      warned_workloads: MapSet.new()
    }

    if client, do: {:ok, state, {:continue, :probe}}, else: {:ok, state}
  end

  @impl true
  def handle_continue(:probe, state) do
    case safe_probe(state.probe) do
      {:ok, status} when is_integer(status) ->
        Logger.info("embervm store TLS probe verified", endpoint: state.endpoint, status: status)
        {:noreply, %{state | status: :verified}}

      {:error, reason} ->
        Logger.warning("embervm store TLS probe failed",
          endpoint: state.endpoint,
          error: inspect(reason)
        )

        {:noreply,
         %{state | status: :failed, error: reason, tls_failed: tls_error?(reason)}}

      other ->
        reason = {:unexpected_probe_result, other}

        Logger.warning("embervm store TLS probe failed",
          endpoint: state.endpoint,
          error: inspect(reason)
        )

        {:noreply, %{state | status: :failed, error: reason}}
    end
  end

  @impl true
  def handle_call(:snapshot, _from, state) do
    snapshot =
      case state.status do
        :verified -> %{store_tls: "verified"}
        :failed -> %{store_tls: "failed", store_tls_error: inspect(state.error)}
        :unconfigured -> %{store_tls: "unconfigured"}
        :pending -> %{store_tls: "failed", store_tls_error: "store TLS probe pending"}
      end

    {:reply, snapshot, state}
  end

  def handle_call({:permit_restore, workload}, _from, %{tls_failed: true} = state) do
    unless MapSet.member?(state.warned_workloads, workload) do
      Logger.warning("embervm restore capability refused after store TLS probe failure",
        workload: workload,
        reason: inspect(state.error)
      )
    end

    state = %{state | warned_workloads: MapSet.put(state.warned_workloads, workload)}
    {:reply, {:error, {:store_tls_failed, state.error}}, state}
  end

  def handle_call({:permit_restore, _workload}, _from, state), do: {:reply, :ok, state}

  defp safe_probe(probe) do
    probe.()
  rescue
    e -> {:error, {:raised, e}}
  catch
    kind, reason -> {:error, {kind, reason}}
  end

  defp tls_error?({:tls_alert, _detail}), do: true
  defp tls_error?(%{reason: reason}), do: tls_error?(reason)

  defp tls_error?(tuple) when is_tuple(tuple) do
    tuple |> Tuple.to_list() |> Enum.any?(&tls_error?/1)
  end

  defp tls_error?(list) when is_list(list), do: Enum.any?(list, &tls_error?/1)
  defp tls_error?(_reason), do: false
end
