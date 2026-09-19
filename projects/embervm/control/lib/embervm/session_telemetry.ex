defmodule Embervm.SessionTelemetry do
  @moduledoc """
  Stable, bounded failure telemetry for session create and invoke spans.

  Response bodies and low-level error terms can contain paths, transport text,
  or guest output. They are useful to the caller but unsafe as span attributes.
  This module maps those shapes to a small `ember.reason` taxonomy and never
  attaches the original value to a span.
  """

  require OpenTelemetry.Tracer, as: Tracer

  @doc "Mark the current span as failed and return its stable reason string."
  @spec mark_error(term()) :: String.t()
  def mark_error(error) do
    stable_reason = reason(error)
    Tracer.set_attributes(%{"ember.reason" => stable_reason})
    Tracer.set_status(:error, stable_reason)
    stable_reason
  rescue
    _ -> reason(error)
  catch
    _, _ -> reason(error)
  end

  @doc "Mark a failed guest HTTP response without recording its body."
  @spec mark_guest_response(non_neg_integer(), binary()) :: String.t() | nil
  def mark_guest_response(status, body) do
    case guest_reason(status, body) do
      nil -> nil
      stable_reason -> mark_error(stable_reason)
    end
  end

  @doc false
  @spec guest_reason(non_neg_integer(), binary()) :: String.t() | nil
  def guest_reason(status, _body) when status < 400, do: nil

  def guest_reason(422, body) when is_binary(body) do
    if String.contains?(body, "timed out waiting for Pi output") do
      "pi_output_timeout"
    else
      "guest_http_422"
    end
  end

  def guest_reason(503, body) when is_binary(body) do
    if String.contains?(body, "workspace does not exist") do
      "workspace_missing"
    else
      "guest_http_503"
    end
  end

  def guest_reason(status, _body), do: "guest_http_#{status}"

  @doc false
  @spec reason(term()) :: String.t()
  def reason(reason) when is_binary(reason), do: normalize_string(reason)
  def reason(:not_found), do: "session_not_found"
  def reason(:terminal), do: "session_gone"
  def reason(reason) when is_atom(reason), do: Atom.to_string(reason)

  def reason({:denied, reason}), do: reason(reason)
  def reason({:conflict, _reason}), do: "session_create_conflict"
  def reason({:gone, _reason}), do: "session_gone"
  def reason({:not_ready, _state}), do: "session_not_ready"
  def reason({:invoke_start_not_recorded, _reason}), do: "invoke_start_not_recorded"
  def reason({:worker_down, _reason}), do: "worker_down"
  def reason({:no_channel, _reason}), do: "no_channel"
  def reason({:rpc, _status}), do: "rpc_error"
  def reason({:prime_failed, _reason}), do: "prime_failed"
  def reason({:prime_dial_failed, _reason}), do: "prime_dial_failed"
  def reason({:store, _reason}), do: "store_error"
  def reason(%GRPC.RPCError{status: 4}), do: "deadline_exceeded"
  def reason(%GRPC.RPCError{}), do: "rpc_error"
  def reason(_reason), do: "operation_failed"

  defp normalize_string("timed out waiting for Pi output" <> _), do: "pi_output_timeout"
  defp normalize_string("workspace does not exist" <> _), do: "workspace_missing"

  defp normalize_string(reason) do
    if Regex.match?(~r/^[a-z][a-z0-9_]{0,63}$/, reason), do: reason, else: "operation_failed"
  end
end
