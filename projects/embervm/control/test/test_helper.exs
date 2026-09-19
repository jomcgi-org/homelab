# assert_receive_timeout raised from ExUnit's 100ms default. The suite runs on a
# shared BuildBuddy executor, so 100ms is a wall-clock bet on scheduler latency
# rather than a statement about the code: DrainCoordinatorTest's
# `assert_receive {:drained, :serving, "node-4"}` failed CI on a PR that touched
# no Elixir, and took the whole Test check red with it (issue #4078). Raising the
# default costs nothing on the passing path (assert_receive returns the moment the
# message lands) and only lengthens how long a genuinely failing assertion waits
# before reporting. refute_receive is unaffected: it reads refute_receive_timeout,
# which stays at 100ms, so no test that must wait out a full window got slower.
# capture_log attributes log output to the test that produced it. The suite runs with
# max_cases: 8, so without it eight concurrent tests interleave into one stream and the
# lines next to a failure usually belong to some other test. That cost real debugging
# time on #4078: an "expired; tearing down" line sat directly above a "registered" line
# and read as one instance being resurrected, when they came from different tests.
# Adjacency in a shared stream is not causality; captured logs make it causality again.
ExUnit.start(assert_receive_timeout: 2_000, capture_log: true)

defmodule Embervm.TestProcess do
  @moduledoc """
  Test-teardown helpers shared by the control-plane suite.

  Defined here rather than in a support/ tree because `mix test` evaluates
  `test_helper.exs` before it compiles the test files, so every test module can
  call this without a build-graph change.
  """

  @doc """
  Stop `pid` gracefully, tolerating a process that has already exited.

  The pattern this replaces, `if Process.alive?(pid), do: GenServer.stop(pid)`,
  reads as if it handles the dead-process case, but check and stop are two steps:
  the process can exit in the gap, and then `GenServer.stop/1` raises `:noproc`
  INSIDE the teardown callback. ExUnit attributes that to the test whose body
  already passed, so the failure surfaces as an unrelated test failing with a
  stack ending in `ExUnit.OnExitHandler` and no assertion diff (issue #4078,
  observed on `Embervm.Scheduler.RetryTest` and `Embervm.BrickControllerTest`).

  Catching the exit closes the race instead of narrowing it. Graceful shutdown is
  preserved, so a `terminate/2` a test depends on still runs; only the
  already-dead case is swallowed. Works for `Agent` too, whose `stop/1` is
  `GenServer.stop/1`.
  """
  @spec stop_safely(pid() | GenServer.name()) :: :ok
  def stop_safely(pid) do
    GenServer.stop(pid)
  catch
    :exit, _ -> :ok
  end
end

defmodule Embervm.TestSpanExporter do
  @moduledoc """
  Focused access to the SDK's in-memory exporter for span-boundary tests.

  Callers must be synchronous ExUnit modules because the configured processor
  is global. Capture is kept opt-in so the rest of the suite retains its normal
  no-exporter configuration.
  """

  @name_index 6
  @parent_span_id_index 4
  @trace_id_index 1
  @span_id_index 2
  @end_time_index 9
  @attributes_index 10
  @status_index 13

  def capture(fun, expected_names) when is_function(fun, 0) do
    table = :ets.new(:embervm_test_spans, [:bag, :public])
    :ok = :otel_batch_processor.set_exporter(:otel_exporter_tab, table)

    try do
      result = fun.()
      spans = await_spans(table, expected_names)
      {result, spans}
    after
      :ok = :otel_batch_processor.set_exporter(:none, [])
      :ets.delete(table)
    end
  end

  def named(spans, name), do: Enum.filter(spans, &(span_name(&1) == name))

  def span_name(span), do: elem(span, @name_index)
  def trace_id(span), do: elem(span, @trace_id_index)
  def span_id(span), do: elem(span, @span_id_index)
  def parent_span_id(span), do: elem(span, @parent_span_id_index)
  def end_time(span), do: elem(span, @end_time_index)
  def attributes(span), do: span |> elem(@attributes_index) |> :otel_attributes.map()
  def status_code(span) do
    case elem(span, @status_index) do
      :undefined -> :unset
      status -> elem(status, 1)
    end
  end

  defp await_spans(table, expected_names) do
    deadline = System.monotonic_time(:millisecond) + 2_000
    await_spans_until(table, MapSet.new(expected_names), deadline)
  end

  defp await_spans_until(table, expected_names, deadline) do
    :ok = :otel_tracer_provider.force_flush()
    spans = :ets.tab2list(table)
    names = spans |> Enum.map(&span_name/1) |> MapSet.new()

    if MapSet.subset?(expected_names, names) do
      spans
    else
      if System.monotonic_time(:millisecond) >= deadline do
        raise "timed out waiting for exported spans #{inspect(MapSet.to_list(expected_names))}; " <>
                "got #{inspect(MapSet.to_list(names))}"
      end

      Process.sleep(10)
      await_spans_until(table, expected_names, deadline)
    end
  end
end
