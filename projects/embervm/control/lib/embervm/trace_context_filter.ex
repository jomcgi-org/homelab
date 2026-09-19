defmodule Embervm.TraceContextFilter do
  @moduledoc """
  Adds the active recording span's trace ID to Logger events.

  Primary Logger filters run synchronously in the process emitting the event.
  Reading the OpenTelemetry context here captures the correct process-local
  span before the event is handed to a Logger handler in another process. The
  field is removed when no recording span is active, which also prevents an ID
  supplied by stale process metadata from surviving span exit.
  """

  @filter_id :embervm_trace_context

  @doc "Install the process-local correlation filter on the primary Logger path."
  @spec install() :: :ok | {:error, term()}
  def install do
    case :logger.add_primary_filter(@filter_id, {&__MODULE__.filter/2, []}) do
      :ok -> :ok
      {:error, {:already_exist, @filter_id}} -> :ok
      error -> error
    end
  end

  @doc "Enrich one Logger event with the current recording trace, if any."
  @spec filter(:logger.log_event(), term()) :: :logger.log_event()
  def filter(%{meta: meta} = event, _config) when is_map(meta) do
    # trace_id is owned by this filter. Removing it first makes cleanup explicit
    # if a caller process previously carried correlation metadata.
    meta = Map.delete(meta, :trace_id)

    case active_trace_id() do
      nil -> %{event | meta: meta}
      trace_id -> %{event | meta: Map.put(meta, :trace_id, trace_id)}
    end
  end

  def filter(event, _config), do: event

  defp active_trace_id do
    span_ctx = OpenTelemetry.Tracer.current_span_ctx()

    if OpenTelemetry.Span.is_recording(span_ctx) and OpenTelemetry.Span.is_valid(span_ctx) do
      OpenTelemetry.Span.hex_trace_id(span_ctx)
    end
  rescue
    _ -> nil
  catch
    _, _ -> nil
  end
end
