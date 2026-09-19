defmodule Embervm.SessionTelemetry do
  @moduledoc """
  Stable, bounded failure telemetry for session create and invoke spans.

  Response bodies and low-level error terms can contain paths, transport text,
  or guest output. They are useful to the caller but unsafe as span attributes.
  This module maps those shapes to a small `ember.reason` taxonomy and never
  attaches the original value to a span.
  """

  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.SessionTrace

  @phase_headers [
    {"embervm.session.hydration", "hydration", "workspace_hydration_failed"},
    {"embervm.session.repo_clone", "repo-clone", "repo_clone_failed"}
  ]

  @phase_statuses ~w(cloned skipped_existing failed lost attempt_cap)

  @doc """
  Run one caller-owned session invoke wait and finish its span in this process.

  The invoke worker can be killed by its watchdog, so it cannot reliably own the
  span which must describe the caller-visible outcome. The request traceparent is
  captured only after this span opens, which makes queue, relight, and guest
  execution spans its children across the GenServer and worker boundaries.
  """
  @spec with_output_wait(String.t(), map(), map(), (map() -> term())) :: term()
  def with_output_wait(session_id, session, req, invoke_fun) when is_function(invoke_fun, 1) do
    Tracer.with_span "embervm.session.output_wait", %{
      attributes: session_attributes(session_id, session)
    } do
      traced_req = Map.put(req, :traceparent, SessionTrace.current_traceparent())

      try do
        result = invoke_fun.(traced_req)
        mark_invoke_result(result)
        record_guest_phases(result, session_id, session)
        result
      rescue
        error ->
          mark_error(:invoke_exception)
          reraise error, __STACKTRACE__
      catch
        kind, reason ->
          mark_error(:invoke_exception)
          :erlang.raise(kind, reason, __STACKTRACE__)
      end
    end
  end

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

  defp mark_invoke_result({:ok, %{status_code: code, body: body}}),
    do: mark_guest_response(code, body)

  defp mark_invoke_result({:error, reason}), do: mark_error(reason)
  defp mark_invoke_result(_result), do: :ok

  defp record_guest_phases({:ok, %{headers: headers}}, session_id, session) when is_map(headers) do
    Enum.each(@phase_headers, fn {span_name, header_slug, failure_reason} ->
      with {:ok, duration_ms} <- phase_duration(headers, header_slug),
           {:ok, status} <- phase_status(headers, header_slug) do
        started_at =
          :opentelemetry.timestamp() -
            System.convert_time_unit(duration_ms, :millisecond, :native)

        attributes =
          session_attributes(session_id, session)
          |> Map.put("ember.phase.status", status)

        Tracer.with_span span_name, %{start_time: started_at, attributes: attributes} do
          if status in ["failed", "lost", "attempt_cap"] do
            mark_error(failure_reason)
          end
        end
      end
    end)
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  defp record_guest_phases(_result, _session_id, _session), do: :ok

  defp phase_duration(headers, slug) do
    with value when is_binary(value) <- header_value(headers, "x-ember-phase-#{slug}-ms"),
         {duration_ms, ""} <- Integer.parse(value),
         true <- duration_ms >= 0 and duration_ms <= 3_600_000 do
      {:ok, duration_ms}
    else
      _ -> :error
    end
  end

  defp phase_status(headers, slug) do
    case header_value(headers, "x-ember-phase-#{slug}-status") do
      status when status in @phase_statuses -> {:ok, status}
      _ -> :error
    end
  end

  defp header_value(headers, name) do
    Enum.find_value(headers, fn {key, value} ->
      if String.downcase(to_string(key)) == name, do: to_string(value)
    end)
  end

  defp session_attributes(session_id, session) do
    %{
      "ember.session_id" => session_id,
      "ember.workload" => Map.get(session, :workload),
      "ember.principal" => Map.get(session, :principal)
    }
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
