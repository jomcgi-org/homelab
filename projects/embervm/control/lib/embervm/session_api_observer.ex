defmodule Embervm.SessionApiObserver do
  @moduledoc """
  Completion observations for the guarded session HTTP surface.

  Bandit owns the response generated after a plug raises or exits, so a
  `register_before_send` callback cannot see every status returned to callers.
  This supervised listener consumes Bandit's request start, stop, and exception
  telemetry instead. Taking the start record from ETS makes stop and exception
  mutually exclusive even if an adapter ever emits both.

  Request observations are bounded before they reach the collector. One pod
  exports at most four completions in each 100 millisecond bucket. The chart has
  one replica and a rolling update can run at most two, so any rolling second
  contains at most 88 request observations. The first overflow in each bucket
  emits a saturation marker and later overflows emit nothing. Alert formulas
  suppress every evaluation window containing a marker.
  """

  use GenServer

  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.SessionApiPolicy

  @handler_id "embervm-session-api-observer"
  @events [
    [:bandit, :request, :start],
    [:bandit, :request, :stop],
    [:bandit, :request, :exception]
  ]
  @bucket_ms 100
  @observations_per_bucket 4
  @stale_bucket_distance 20

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @impl true
  def init(_opts) do
    table =
      :ets.new(__MODULE__, [
        :named_table,
        :public,
        :set,
        read_concurrency: true,
        write_concurrency: true
      ])

    :telemetry.detach(@handler_id)
    :ok = :telemetry.attach_many(@handler_id, @events, &__MODULE__.handle_event/4, table)
    {:ok, table}
  end

  @impl true
  def terminate(_reason, _table) do
    :telemetry.detach(@handler_id)
    :ok
  end

  @doc false
  def observation_envelope do
    %{
      bucket_ms: @bucket_ms,
      observations_per_bucket: @observations_per_bucket,
      max_per_pod_rolling_second: 44,
      max_two_pod_rolling_second: 88,
      max_two_pod_saturation_markers: 22
    }
  end

  @doc false
  def handle_event(event, measurements, metadata, table) do
    do_handle_event(event, measurements, metadata, table)
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  defp do_handle_event(
         [:bandit, :request, :start],
         %{monotonic_time: started_at},
         %{conn: conn, telemetry_span_context: span_ref},
         table
       ) do
    case SessionApiPolicy.classify(conn) do
      :outside ->
        :ok

      {decision, route} when decision in [:allowed, :denied] ->
        :ets.insert(
          table,
          {{:request, span_ref}, started_at, conn.method, route, decision == :allowed}
        )

        :ok
    end
  end

  defp do_handle_event(
         [:bandit, :request, :stop],
         %{monotonic_time: completed_at},
         %{conn: conn, telemetry_span_context: span_ref},
         table
       ) do
    complete(table, span_ref, completed_at, conn.status || 500)
  end

  defp do_handle_event(
         [:bandit, :request, :exception],
         %{monotonic_time: completed_at},
         %{exception: exception, telemetry_span_context: span_ref},
         table
       ) do
    complete(table, span_ref, completed_at, exception_status(exception))
  end

  defp do_handle_event(_event, _measurements, _metadata, _table), do: :ok

  defp complete(table, span_ref, completed_at, status) do
    case :ets.take(table, {:request, span_ref}) do
      [{{:request, ^span_ref}, started_at, method, route, allowed}] ->
        case observation_decision(table, completed_at) do
          :request -> record_request(started_at, completed_at, method, route, allowed, status)
          :saturation -> record_saturation()
          :drop -> :ok
        end

      [] ->
        :ok
    end
  end

  defp observation_decision(table, completed_at) do
    bucket_width = System.convert_time_unit(@bucket_ms, :millisecond, :native)
    bucket = div(completed_at, bucket_width)
    key = {:bucket, bucket}
    count = :ets.update_counter(table, key, {2, 1}, {key, 0})

    if count == 1 do
      :ets.select_delete(table, [
        {{{:bucket, :"$1"}, :_}, [{:<, :"$1", bucket - @stale_bucket_distance}], [true]}
      ])
    end

    cond do
      count <= @observations_per_bucket -> :request
      count == @observations_per_bucket + 1 -> :saturation
      true -> :drop
    end
  end

  defp record_request(started_at, completed_at, method, route, allowed, status) do
    duration_ms =
      completed_at
      |> Kernel.-(started_at)
      |> max(0)
      |> System.convert_time_unit(:native, :microsecond)
      |> Kernel./(1_000)

    attributes = %{
      "ember.http.duration_ms" => duration_ms,
      "ember.observation.kind" => "request",
      "ember.policy.allowed" => allowed,
      "ember.surface" => "session_api",
      "http.request.method" => method,
      "http.response.status_code" => status,
      "http.route" => route
    }

    record_span(attributes, status)
  end

  defp record_saturation do
    record_span(
      %{
        "ember.observation.kind" => "saturation",
        "ember.observation.saturated" => true,
        "ember.surface" => "session_api"
      },
      nil
    )
  end

  # Honeycomb indexes a span by start time. These one-span traces represent
  # completion observations, so start them at completion while retaining the
  # full request duration as an attribute. A fresh context also prevents a
  # handler's ambient trace from turning the observation into a child span.
  defp record_span(attributes, status) do
    Tracer.with_span OpenTelemetry.Ctx.new(), "embervm.http.request", %{
      kind: :server,
      start_time: :opentelemetry.timestamp(),
      attributes: attributes
    } do
      if is_integer(status) and status >= 500 do
        Tracer.set_status(:error, "HTTP #{status}")
      end
    end
  end

  defp exception_status(exception) do
    exception
    |> Plug.Exception.status()
    |> Plug.Conn.Status.code()
  rescue
    _ -> 500
  catch
    _, _ -> 500
  end
end
