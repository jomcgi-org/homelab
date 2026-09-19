defmodule Embervm.LogFormatterTest do
  use ExUnit.Case, async: true

  alias Embervm.CapacityObserver
  alias Embervm.TraceContextFilter
  require OpenTelemetry.Tracer, as: Tracer

  defp event(message, metadata \\ %{}) do
    %{level: :warning, msg: {:string, message}, meta: metadata}
  end

  defp filtered(message, metadata \\ %{}) do
    TraceContextFilter.filter(event(message, metadata), [])
  end

  test "installs the trace context filter on the primary Logger path" do
    filters = :logger.get_primary_config().filters
    assert Enum.any?(filters, fn {id, _filter} -> id == :embervm_trace_context end)
  end

  test "drops stale trace metadata when no span is active and preserves other metadata" do
    enriched = filtered("outside span", %{trace_id: "stale", task_id: "task-1"})

    refute Map.has_key?(enriched.meta, :trace_id)
    assert enriched.meta.task_id == "task-1"

    decoded =
      enriched
      |> Embervm.LogFormatter.format(%{})
      |> IO.iodata_to_binary()
      |> :json.decode()

    refute Map.has_key?(decoded, "trace_id")
    assert decoded["task_id"] == "task-1"
  end

  test "does not emit an ID for a valid non-recording remote span" do
    previous = OpenTelemetry.Ctx.get_current()
    remote = :otel_tracer.from_remote_span(0x1234, 0x5678, 1)

    try do
      Tracer.set_current_span(remote)
      refute Map.has_key?(filtered("remote", %{trace_id: "stale"}).meta, :trace_id)
    after
      OpenTelemetry.Ctx.attach(previous)
    end
  end

  test "emits the recording trace ID through the JSON formatter" do
    Tracer.with_span "log-correlation" do
      span_ctx = Tracer.current_span_ctx()
      assert OpenTelemetry.Span.is_recording(span_ctx)
      expected = OpenTelemetry.Span.hex_trace_id(span_ctx)

      decoded =
        filtered("embervm probe failed", %{task_id: "task-2"})
        |> Embervm.LogFormatter.format(%{})
        |> IO.iodata_to_binary()
        |> :json.decode()

      assert decoded["trace_id"] == expected
      assert decoded["task_id"] == "task-2"
      assert decoded["message"] == "embervm probe failed"
    end
  end

  test "nested spans restore the parent context and clean metadata after exit" do
    Tracer.with_span "outer-log" do
      outer = Tracer.current_span_ctx()
      outer_span_id = OpenTelemetry.Span.hex_span_id(outer)
      outer_trace_id = filtered("outer before").meta.trace_id

      Tracer.with_span "inner-log" do
        inner = Tracer.current_span_ctx()
        assert OpenTelemetry.Span.hex_span_id(inner) != outer_span_id
        assert filtered("inner").meta.trace_id == outer_trace_id
      end

      assert OpenTelemetry.Span.hex_span_id(Tracer.current_span_ctx()) == outer_span_id
      assert filtered("outer after").meta.trace_id == outer_trace_id
    end

    refute Map.has_key?(filtered("after", %{trace_id: "stale"}).meta, :trace_id)
  end

  test "concurrent processes keep independent trace metadata" do
    caller = self()

    tasks =
      for name <- ["first-log", "second-log"] do
        Task.async(fn ->
          Tracer.with_span name do
            trace_id = filtered(name).meta.trace_id
            send(caller, {:ready, self(), trace_id})

            receive do
              :continue -> {trace_id, filtered(name).meta.trace_id}
            end
          end
        end)
      end

    ready =
      for _ <- tasks do
        assert_receive {:ready, pid, trace_id}
        {pid, trace_id}
      end

    Enum.each(ready, fn {pid, _trace_id} -> send(pid, :continue) end)
    results = Enum.map(tasks, &Task.await/1)

    assert ready |> Enum.map(&elem(&1, 1)) |> Enum.uniq() |> length() == 2
    assert Enum.all?(results, fn {before, after_wait} -> before == after_wait end)
  end

  test "preserves session identity on invoke watchdog logs" do
    line =
      Embervm.LogFormatter.format(
        %{
          level: :warning,
          msg: {:string, "session invoke worker watchdog fired"},
          meta: %{session_id: "session-123", workload: "pi-runtime", node_id: "node-4"}
        },
        %{}
      )
      |> IO.iodata_to_binary()

    decoded = :json.decode(line)

    assert decoded["session_id"] == "session-123"
    assert decoded["workload"] == "pi-runtime"
    assert decoded["node_id"] == "node-4"
  end

  test "preserves every CapacityObserver record field in structured JSON" do
    reservation_table =
      String.to_atom("log_formatter_reservation_#{System.unique_integer([:positive])}")

    record =
      CapacityObserver.build_record(
        %{
          instance_id: "node-1/pod-a",
          node_id: "node-1",
          size_class: "2gi",
          mem_budget_mib: 1_536,
          mem_headroom_mib: 1_111,
          mem_reserved_mib: 0,
          admits_on_reservation: false,
          live_vms: 0,
          max_live_vms: 8
        },
        reservation_table
      )

    line =
      Embervm.LogFormatter.format(
        # :logger metadata is a MAP, not a keyword list. Passing a list makes
        # whitelisted_meta/1 raise, and the formatter's rescue then emits its
        # plain-text fallback, so the assertion failure points at the JSON
        # decoder rather than at the real cause.
        %{level: :info, msg: {:string, "embervm capacity brick"}, meta: record},
        %{}
      )
      |> IO.iodata_to_binary()

    decoded = :json.decode(line)

    for key <- Map.keys(record) do
      assert Map.has_key?(decoded, Atom.to_string(key)),
             "record field #{inspect(key)} was dropped from structured JSON"
    end

    assert Map.has_key?(decoded, Atom.to_string(:guest_free?))
  end

  test "preserves every retention manifest field in structured JSON" do
    metadata = [
      node_id: "node-1",
      path: "/var/lib/embervm/scratch/bases/ref-1",
      size_bytes: 42,
      workload: "claude-runtime",
      vendor: "intel",
      age_seconds: 72_000,
      reason_unreferenced: "known workload superseded: not in current, CP snapshot, or active base_refs",
      base_generation: 17
    ]

    line =
      Embervm.LogFormatter.format(
        %{level: :info, msg: {:string, "embervm base retention candidate"}, meta: Map.new(metadata)},
        %{}
      )
      |> IO.iodata_to_binary()

    decoded = :json.decode(line)

    for {key, value} <- metadata do
      assert Map.get(decoded, Atom.to_string(key)) == value
    end
  end

  test "preserves StatefulSweeper pressure transition fields in structured JSON" do
    metadata = %{brick_id: "node-4/pod-a", from: :high, to: :shedding}

    line =
      Embervm.LogFormatter.format(
        %{
          level: :info,
          msg: {:string, "embervm stateful: pressure state change"},
          meta: metadata
        },
        %{}
      )
      |> IO.iodata_to_binary()

    decoded = :json.decode(line)

    assert decoded["brick_id"] == "node-4/pod-a"
    assert decoded["from"] == "high"
    assert decoded["to"] == "shedding"
  end

  test "preserves the parsed inert desired capacity value" do
    line =
      Embervm.LogFormatter.format(
        %{
          level: :info,
          msg: {:string, "embervm desired capacity parsed"},
          meta: %{desired_capacity: 12}
        },
        %{}
      )
      |> IO.iodata_to_binary()

    assert :json.decode(line)["desired_capacity"] == 12
  end

  test "preserves volume restore refusal fields in structured JSON" do
    metadata = %{
      workload: "wl-a",
      anchor: "node-dead",
      restore_target: "node-live",
      exported_generation: 3
    }

    line =
      Embervm.LogFormatter.format(
        %{
          level: :warning,
          msg: {:string, "embervm stateful: volume restore refused"},
          meta: metadata
        },
        %{}
      )
      |> IO.iodata_to_binary()

    decoded = :json.decode(line)

    for {key, value} <- metadata do
      assert Map.get(decoded, Atom.to_string(key)) == value
    end
  end
end
