defmodule Embervm.SessionTrace do
  @moduledoc """
  W3C-traceparent plumbing for the session invoke/bank/relight spans (R2, Task 9).

  A session invoke fans across several BEAM processes: the router opens the root
  span, the per-session process owns the FIFO (the `queue_wait` phase), the invoke
  worker runs the `SessionAssign` (the `guest_exec` phase), and the manager's
  relight worker runs the `relight` phase. OTel span context lives in the process
  dictionary, so it does NOT cross a `GenServer.call` or a `spawn`. Rather than
  invent a new tracing layer, this module reuses the EXACT idiom the dispatcher
  already uses to nest its spans under a caller's trace (`:otel_tracer.from_remote_span`
  fed from a parsed W3C `traceparent`): the root span is serialized to a traceparent
  string, threaded through the plain `req` map, and restored as the remote parent in
  each downstream worker so every phase span nests under the one session-invoke root.

  This is intentionally the same shape as `Embervm.Dispatcher.restore_trace_ctx/2`
  and `parse_traceparent/1` (which stay private to the dispatcher's async op-log
  path); the session path is synchronous and carries the parent inline, so the
  helper is shared here rather than duplicated.
  """

  @traceparent_key "traceparent"

  @doc """
  The W3C `traceparent` for the CURRENTLY active span, or `nil` when no span is
  active (tracing off, e.g. CI with no exporter).

  Serialization is delegated to OpenTelemetry's W3C TraceContext propagator so
  the current sampling flag and validation rules stay consistent with the SDK.
  """
  @spec current_traceparent() :: String.t() | nil
  def current_traceparent do
    outbound_metadata()[@traceparent_key]
  rescue
    # A trace hiccup must never break the invoke path: no traceparent just means
    # the downstream span is a root instead of a child.
    _ -> nil
  catch
    _, _ -> nil
  end

  @doc """
  Inject the current W3C TraceContext into outbound gRPC metadata.

  Existing metadata is retained, including authorization or application keys.
  With no valid active span the map is returned unchanged.
  """
  @spec outbound_metadata(map()) :: map()
  def outbound_metadata(metadata \\ %{}) when is_map(metadata) do
    :otel_propagator_text_map.inject(
      :otel_propagator_trace_context,
      metadata,
      fn key, value, carrier -> Map.put(carrier, key, value) end
    )
  rescue
    _ -> metadata
  catch
    _, _ -> metadata
  end

  @doc "Add current W3C TraceContext metadata to a gRPC call's options."
  @spec rpc_options(keyword()) :: keyword()
  def rpc_options(options \\ []) when is_list(options) do
    existing = Keyword.get(options, :metadata, %{})
    injected = outbound_metadata(existing)

    cond do
      injected == existing -> options
      Keyword.has_key?(options, :metadata) or map_size(injected) > 0 ->
        Keyword.put(options, :metadata, injected)
      true ->
        options
    end
  rescue
    _ -> options
  catch
    _, _ -> options
  end

  @doc """
  Restore `traceparent` (a W3C string, or nil) as the current process's remote
  parent span so a subsequently-opened span nests under it. A nil/malformed
  traceparent is a no-op (the next span becomes a root). Guarded: a trace hiccup
  never crashes the caller (mirrors `Dispatcher.restore_trace_ctx/2`).
  """
  @spec restore_parent(String.t() | nil) :: :ok
  def restore_parent(traceparent) when is_binary(traceparent) do
    _token =
      :otel_propagator_text_map.extract(
        :otel_propagator_trace_context,
        [{@traceparent_key, traceparent}]
      )

    :ok
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  def restore_parent(_), do: :ok

  @doc "Run a function with only the supplied remote parent active in this process."
  @spec with_parent(String.t() | nil, (-> result)) :: result when result: var
  def with_parent(traceparent, fun) when is_function(fun, 0) do
    token = OpenTelemetry.Ctx.attach(OpenTelemetry.Ctx.new())

    try do
      restore_parent(traceparent)
      fun.()
    after
      OpenTelemetry.Ctx.detach(token)
    end
  end

  @doc "The header/key under which the root traceparent rides the `req` map."
  @spec key() :: String.t()
  def key, do: @traceparent_key

  # Parse a W3C `traceparent` (`<ver>-<32hex trace>-<16hex span>-<2hex flags>`)
  # into integer ids, or :error for anything malformed/absent. Same parser shape
  # as the dispatcher's private one.
  @spec parse_traceparent(String.t() | nil) ::
          {non_neg_integer(), non_neg_integer(), non_neg_integer()} | :error
  def parse_traceparent(tp) when is_binary(tp) do
    case String.split(tp, "-") do
      [_ver, trace_hex, span_hex, flags_hex]
      when byte_size(trace_hex) == 32 and byte_size(span_hex) == 16 ->
        with {trace_id, ""} <- Integer.parse(trace_hex, 16),
             {span_id, ""} <- Integer.parse(span_hex, 16),
             {flags, ""} <- Integer.parse(flags_hex, 16) do
          {trace_id, span_id, flags}
        else
          _ -> :error
        end

      _ ->
        :error
    end
  end

  def parse_traceparent(_), do: :error
end
