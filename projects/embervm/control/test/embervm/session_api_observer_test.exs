defmodule Embervm.SessionApiObserverTestPlug do
  @moduledoc false

  import Plug.Conn

  alias Embervm.SessionApiPolicy

  def init(opts), do: opts

  def call(conn, _opts) do
    conn = SessionApiPolicy.call(conn, [])

    if conn.halted do
      conn
    else
      respond(conn, get_req_header(conn, "x-test-outcome"))
    end
  end

  defp respond(_conn, ["raise"]), do: raise("observer test failure")
  defp respond(_conn, ["exit"]), do: GenServer.call(:missing_observer_test_server, :boom)
  defp respond(conn, ["explicit-500"]), do: send_resp(conn, 500, "failed")

  defp respond(conn, _outcome) do
    case get_req_header(conn, "authorization") do
      [] -> send_resp(conn, 401, "unauthorized")
      ["Bearer forbidden"] -> send_resp(conn, 403, "forbidden")
      [_token] -> send_resp(conn, 200, "ok")
    end
  end
end

defmodule Embervm.SessionApiObserverTest do
  use ExUnit.Case, async: false

  import Plug.Test

  require Record

  alias Embervm.SessionApiObserver

  Record.defrecordp(
    :span,
    Record.extract(:span, from_lib: "opentelemetry/include/otel_span.hrl")
  )

  @port 58_083
  @span_name "embervm.http.request"

  setup do
    :ets.match_delete(SessionApiObserver, {{:bucket, :_}, :_})
    :ok = :otel_batch_processor.set_exporter(:otel_exporter_pid, self())

    on_exit(fn ->
      :gen_statem.call(:otel_batch_processor_global, {:set_exporter, :none})
    end)

    :ok
  end

  test "exports normal and exceptional Bandit completions exactly once as roots" do
    start_supervised!(
      {Bandit,
       plug: Embervm.SessionApiObserverTestPlug,
       scheme: :http,
       port: @port}
    )

    cases = [
      {:get, "/v1/sessions/s-1", [{"authorization", "Bearer good"}], 200,
       "/v1/sessions/:id", true},
      {:post, "/v1/sessions/s-1/invoke", [], 401, "/v1/sessions/:id/invoke", true},
      {:delete, "/v1/sessions/s-1", [{"authorization", "Bearer forbidden"}], 403,
       "/v1/sessions/:id", true},
      {:put, "/v1/sessions/s-1/invoke", [{"authorization", "Bearer good"}], 403,
       "/v1/sessions/:id/invoke", false},
      {:post, "/v1/workloads/runtime/sessions", test_headers("explicit-500"), 500,
       "/v1/workloads/:name/sessions", true},
      {:get, "/v1/workloads/runtime/sessions", test_headers("raise"), 500,
       "/v1/workloads/:name/sessions", true},
      {:delete, "/v1/sessions/s-2", test_headers("exit"), 500, "/v1/sessions/:id", true}
    ]

    for {method, path, headers, expected_status, _route, _allowed} <- cases do
      assert {:ok, response} =
               Finch.build(method, "http://127.0.0.1:#{@port}#{path}", headers)
               |> Finch.request(Embervm.Finch)

      assert response.status == expected_status
      Process.sleep(110)
    end

    :ok = :otel_tracer_provider.force_flush()
    spans = receive_request_spans(length(cases))

    actual =
      spans
      |> Enum.map(&request_signature/1)
      |> Enum.sort()

    expected =
      cases
      |> Enum.map(fn {method, _path, _headers, status, route, allowed} ->
        {method |> Atom.to_string() |> String.upcase(), status, route, allowed}
      end)
      |> Enum.sort()

    assert actual == expected
    assert Enum.all?(spans, &(span(&1, :parent_span_id) in [:undefined, nil]))
    assert spans |> Enum.map(&span(&1, :trace_id)) |> Enum.uniq() |> length() == length(spans)
    assert Enum.all?(spans, &(attributes(&1)["ember.http.duration_ms"] >= 0))

    :ok = :otel_tracer_provider.force_flush()
    refute_receive {:span, span(name: @span_name)}, 200
  end

  test "keeps mixed request observations exact below the envelope" do
    %{bucket_ms: bucket_ms} = SessionApiObserver.observation_envelope()
    bucket_width = System.convert_time_unit(bucket_ms, :millisecond, :native)
    completed_base = System.monotonic_time() + System.convert_time_unit(30, :second, :native)

    for index <- 1..20 do
      {method, path, status} = mixed_request(index)
      completed_at = completed_base + index * bucket_width
      duration_ms = if index == 20, do: 400_000, else: index * 10

      emit_stop(method, path, status, completed_at, duration_ms)
    end

    :ok = :otel_tracer_provider.force_flush()
    spans = receive_request_spans(20)

    refute Enum.any?(spans, &(attributes(&1)["ember.observation.kind"] == "saturation"))
    assert error_rate(spans) == 0.10

    eligible = Enum.reject(spans, &(attributes(&1)["http.route"] == "/v1/sessions/:id/invoke"))
    assert length(eligible) == 14
    assert percentile_99(eligible) == 400_000
  end

  test "emits one marker and suppresses an evaluation above the envelope" do
    %{observations_per_bucket: limit} = SessionApiObserver.observation_envelope()
    completed_at = System.monotonic_time() + System.convert_time_unit(60, :second, :native)

    for index <- 1..(limit + 4) do
      status = if rem(index, 2) == 0, do: 500, else: 200
      emit_stop(:get, "/v1/sessions/s-#{index}", status, completed_at, 10)
    end

    :ok = :otel_tracer_provider.force_flush()
    spans = receive_request_spans(limit + 1)
    kinds = Enum.frequencies_by(spans, &attributes(&1)["ember.observation.kind"])

    assert kinds == %{"request" => limit, "saturation" => 1}
    assert error_rate(spans) == :suppressed

    :ok = :otel_tracer_provider.force_flush()
    refute_receive {:span, span(name: @span_name)}, 200
  end

  test "timestamps long create and invoke observations at completion" do
    completed_at = System.monotonic_time() + System.convert_time_unit(90, :second, :native)

    emit_stop(
      :post,
      "/v1/workloads/runtime/sessions",
      200,
      completed_at,
      15 * 60 * 1_000
    )

    emit_exception(
      :post,
      "/v1/sessions/s-long/invoke",
      completed_at + System.convert_time_unit(200, :millisecond, :native),
      6 * 60 * 1_000
    )

    :ok = :otel_tracer_provider.force_flush()
    spans = receive_request_spans(2)

    create = Enum.find(spans, &(attributes(&1)["http.route"] == "/v1/workloads/:name/sessions"))
    invoke = Enum.find(spans, &(attributes(&1)["http.route"] == "/v1/sessions/:id/invoke"))

    assert attributes(create)["ember.http.duration_ms"] == 900_000
    assert attributes(invoke)["ember.http.duration_ms"] == 360_000
    assert attributes(invoke)["http.response.status_code"] == 500

    for completed_span <- spans do
      assert span(completed_span, :end_time) - span(completed_span, :start_time) <
               System.convert_time_unit(2, :second, :nanosecond)
    end
  end

  defp test_headers(outcome) do
    [{"authorization", "Bearer good"}, {"x-test-outcome", outcome}]
  end

  defp mixed_request(index) when index <= 5,
    do: {:post, "/v1/sessions/s-#{index}/invoke", mixed_status(index)}

  defp mixed_request(6), do: {:put, "/v1/sessions/s-6/invoke", 403}

  defp mixed_request(index),
    do: {:post, "/v1/workloads/runtime-#{index}/sessions", mixed_status(index)}

  defp mixed_status(index) when index in [10, 20], do: 500
  defp mixed_status(index) when rem(index, 5) == 0, do: 403
  defp mixed_status(_index), do: 200

  defp emit_stop(method, path, status, completed_at, duration_ms) do
    span_ref = make_ref()
    started_at = completed_at - System.convert_time_unit(duration_ms, :millisecond, :native)
    start_conn = conn(method, path)
    response_conn = Plug.Conn.put_status(start_conn, status)

    :telemetry.execute(
      [:bandit, :request, :start],
      %{monotonic_time: started_at},
      %{conn: start_conn, telemetry_span_context: span_ref}
    )

    :telemetry.execute(
      [:bandit, :request, :stop],
      %{monotonic_time: completed_at},
      %{conn: response_conn, telemetry_span_context: span_ref}
    )
  end

  defp emit_exception(method, path, completed_at, duration_ms) do
    span_ref = make_ref()
    started_at = completed_at - System.convert_time_unit(duration_ms, :millisecond, :native)
    request_conn = conn(method, path)

    :telemetry.execute(
      [:bandit, :request, :start],
      %{monotonic_time: started_at},
      %{conn: request_conn, telemetry_span_context: span_ref}
    )

    :telemetry.execute(
      [:bandit, :request, :exception],
      %{monotonic_time: completed_at},
      %{
        exception: RuntimeError.exception("failed"),
        kind: :exit,
        conn: request_conn,
        telemetry_span_context: span_ref
      }
    )
  end

  defp receive_request_spans(count), do: receive_request_spans(count, [])
  defp receive_request_spans(0, spans), do: spans

  defp receive_request_spans(count, spans) do
    receive do
      {:span, exported} ->
        if span(exported, :name) == @span_name do
          receive_request_spans(count - 1, [exported | spans])
        else
          receive_request_spans(count, spans)
        end
    after
      2_000 -> flunk("timed out after receiving #{length(spans)} request spans")
    end
  end

  defp attributes(exported), do: :otel_attributes.map(span(exported, :attributes))

  defp request_signature(exported) do
    attrs = attributes(exported)

    {
      attrs["http.request.method"],
      attrs["http.response.status_code"],
      attrs["http.route"],
      attrs["ember.policy.allowed"]
    }
  end

  defp error_rate(spans) do
    attrs = Enum.map(spans, &attributes/1)

    if Enum.any?(attrs, &(&1["ember.observation.kind"] == "saturation")) do
      :suppressed
    else
      requests = Enum.filter(attrs, &is_integer(&1["http.response.status_code"]))
      errors = Enum.count(requests, &(&1["http.response.status_code"] >= 500))
      errors / length(requests)
    end
  end

  defp percentile_99(spans) do
    spans
    |> Enum.map(&attributes(&1)["ember.http.duration_ms"])
    |> Enum.sort()
    |> List.last()
  end
end
