defmodule Embervm.SessionTelemetryTest do
  use ExUnit.Case, async: true

  alias Embervm.SessionTelemetry

  test "classifies actual create and invoke reason variants" do
    assert SessionTelemetry.reason(:session_cap) == "session_cap"
    assert SessionTelemetry.reason(:workload_cap) == "workload_cap"
    assert SessionTelemetry.reason(:invoke_timeout) == "invoke_timeout"
    assert SessionTelemetry.reason({:invoke_start_not_recorded, :unavailable}) ==
             "invoke_start_not_recorded"

    assert SessionTelemetry.reason({:worker_down, :killed}) == "worker_down"
    assert SessionTelemetry.reason({:no_channel, :closed}) == "no_channel"
    assert SessionTelemetry.reason({:rpc, 14}) == "rpc_error"
    assert SessionTelemetry.reason({:prime_failed, {:error, :unavailable}}) ==
             "prime_failed"
  end

  test "classifies known guest failures without returning guest output" do
    pi_body = ~s({"error":"timed out waiting for Pi output after 600 seconds"})
    workspace_body = ~s({"error":"workspace does not exist: /workspace/src"})

    assert SessionTelemetry.guest_reason(422, pi_body) == "pi_output_timeout"
    assert SessionTelemetry.guest_reason(503, workspace_body) == "workspace_missing"
    assert SessionTelemetry.guest_reason(422, ~s({"error":"secret guest output"})) ==
             "guest_http_422"

    assert SessionTelemetry.guest_reason(200, ~s({"workspace_hydration":"ok"})) == nil
  end

  test "bounds dynamic and nested failures to stable reasons" do
    assert SessionTelemetry.reason({:gone, "expired: sensitive detail"}) == "session_gone"
    assert SessionTelemetry.reason({:not_ready, :relighting}) == "session_not_ready"
    assert SessionTelemetry.reason({:store, "database detail"}) == "store_error"
    assert SessionTelemetry.reason("contains user output and spaces") == "operation_failed"
    assert SessionTelemetry.reason(%GRPC.RPCError{status: 4}) == "deadline_exceeded"
    assert SessionTelemetry.reason(%GRPC.RPCError{status: 14}) == "rpc_error"
  end

  test "marking is inert without an exporter and returns the selected reason" do
    assert SessionTelemetry.mark_error(:invoke_timeout) == "invoke_timeout"
    assert SessionTelemetry.mark_guest_response(200, "ok") == nil
    assert SessionTelemetry.mark_guest_response(503, "workspace does not exist: /secret") ==
             "workspace_missing"
  end
end
