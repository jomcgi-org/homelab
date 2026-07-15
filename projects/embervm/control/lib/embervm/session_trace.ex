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

  # current_span_ctx/0 and set_current_span/1 are OpenTelemetry.Tracer MACROS, so
  # the module must be required even though it is called fully-qualified.
  require OpenTelemetry.Tracer

  @traceparent_key "traceparent"

  @doc """
  The W3C `traceparent` for the CURRENTLY active span, or `nil` when no span is
  recording (tracing off, e.g. CI with no exporter). Sampled flag is forced to
  `01`: we only ever serialize a span we just opened and are recording under, so a
  downstream worker restoring it should sample the same trace.
  """
  @spec current_traceparent() :: String.t() | nil
  def current_traceparent do
    case OpenTelemetry.Tracer.current_span_ctx() do
      :undefined ->
        nil

      span_ctx ->
        trace_hex = OpenTelemetry.Span.hex_trace_id(span_ctx)
        span_hex = OpenTelemetry.Span.hex_span_id(span_ctx)

        if all_zero?(trace_hex) or all_zero?(span_hex) do
          nil
        else
          "00-#{trace_hex}-#{span_hex}-01"
        end
    end
  rescue
    # A trace hiccup must never break the invoke path: no traceparent just means
    # the downstream span is a root instead of a child.
    _ -> nil
  catch
    _, _ -> nil
  end

  @doc """
  Restore `traceparent` (a W3C string, or nil) as the current process's remote
  parent span so a subsequently-opened span nests under it. A nil/malformed
  traceparent is a no-op (the next span becomes a root). Guarded: a trace hiccup
  never crashes the caller (mirrors `Dispatcher.restore_trace_ctx/2`).
  """
  @spec restore_parent(String.t() | nil) :: :ok
  def restore_parent(traceparent) do
    case parse_traceparent(traceparent) do
      {trace_id, span_id, flags} ->
        try do
          remote = :otel_tracer.from_remote_span(trace_id, span_id, flags)
          OpenTelemetry.Tracer.set_current_span(remote)
          :ok
        rescue
          _ -> :ok
        catch
          _, _ -> :ok
        end

      :error ->
        :ok
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

  defp all_zero?(hex), do: String.trim(hex, "0") == ""
end
