defmodule Embervm.SpecTrace do
  @moduledoc "Debug-only, spec-shaped NDJSON trace."

  @writer Embervm.SpecTrace.Writer
  @enabled_key {__MODULE__, :enabled}
  @dropped_key {__MODULE__, :dropped}
  # Mailbox depth past which emitters stop enqueueing. Generous relative to any
  # real burst (a sweep emits one Checkpoint), small enough that a stalled
  # writer cannot accumulate meaningful heap.
  @max_queue 10_000
  # Bumped whenever the record shape changes, so a consumer reading an old
  # segment knows what it is parsing rather than guessing from the fields.
  @schema_version 1

  @spec configure() :: boolean()
  def configure do
    enabled = System.get_env("EMBERVM_SPEC_TRACE", "off") |> enabled?()
    :persistent_term.put(@enabled_key, enabled)
    enabled
  end

  @spec emit(atom() | String.t(), atom() | String.t(), map()) :: :ok
  def emit(spec, action, vars) when is_map(vars) do
    if :persistent_term.get(@enabled_key, false) do
      mono = System.monotonic_time(:nanosecond)
      ts = System.system_time(:millisecond)

      case Process.whereis(@writer) do
        pid when is_pid(pid) -> maybe_send(pid, {:emit, spec, action, vars, mono, ts})
        _ -> :ok
      end
    end

    :ok
  end

  # BOUNDED fire-and-forget. `send/2` never blocks, which is the property we
  # want, but an unbounded mailbox is how a debug facility OOMs a control
  # plane: a writer stalled on disk would grow this queue without limit while
  # every emitter happily continued. So refuse to enqueue past a depth and
  # count the refusals instead.
  #
  # Dropping MUST be visible. A silently lossy trace is the same failure as a
  # sampled one: it looks like coverage while missing records, and a checker
  # would read the gap as the system not acting. The dropped count rides the
  # next record that does get through, so a consumer can tell "nothing
  # happened" from "we stopped looking".
  defp maybe_send(pid, msg) do
    case Process.info(pid, :message_queue_len) do
      {:message_queue_len, len} when len < @max_queue ->
        dropped = :persistent_term.get(@dropped_key, 0)
        if dropped > 0, do: :persistent_term.put(@dropped_key, 0)
        send(pid, put_dropped(msg, dropped))

      _ ->
        # Counter only, no send: the queue is already the problem.
        :persistent_term.put(@dropped_key, :persistent_term.get(@dropped_key, 0) + 1)
    end

    :ok
  end

  defp put_dropped(msg, 0), do: msg
  defp put_dropped({:emit, spec, action, vars, mono, ts}, n),
    do: {:emit, spec, action, Map.put(vars, :spec_trace_dropped_before, n), mono, ts}

  @doc "Wait until all events already sent to the named writer are written."
  @spec drain(GenServer.server()) :: :ok
  def drain(server \\ @writer), do: GenServer.call(server, :drain)

  def start_link(opts \\ []) do
    configure()

    if :persistent_term.get(@enabled_key, false) do
      @writer.start_link(opts)
    else
      :ignore
    end
  end

  def child_spec(opts) do
    %{id: __MODULE__, start: {__MODULE__, :start_link, [opts]}, type: :worker}
  end

  defp enabled?(value), do: String.downcase(String.trim(value)) in ["on", "true", "1"]

  # Exposed for the Writer, which is a SEPARATE top-level module and therefore
  # cannot see these attributes. Kept as functions rather than duplicating the
  # literals, so the preamble can never disagree with the behaviour it claims
  # to describe.
  @doc false
  def schema_version, do: @schema_version
  @doc false
  def max_queue, do: @max_queue
  @doc false
  def enabled_now?, do: :persistent_term.get(@enabled_key, false)
end

defmodule Embervm.SpecTrace.Writer do
  use GenServer
  require Logger

  @default_segment_bytes 64 * 1024 * 1024
  @default_segments 3
  @default_dir "/var/lib/embervm/scratch/spec-trace"

  def start_link(opts \\ []) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @impl true
  def init(opts) do
    Process.flag(:trap_exit, true)
    dir = Keyword.get(opts, :dir, System.get_env("EMBERVM_SPEC_TRACE_DIR", @default_dir))
    cap = Keyword.get(opts, :segment_bytes, env_integer("EMBERVM_SPEC_TRACE_SEGMENT_BYTES", @default_segment_bytes))
    segments = Keyword.get(opts, :segments, env_integer("EMBERVM_SPEC_TRACE_SEGMENTS", @default_segments))
    state = %{dir: dir, cap: max(cap, 1), segments: max(segments, 1), index: 0, size: 0, seq: 0, run_id: uuid(), io: nil}
    {:ok, open_segment(state), {:continue, :preamble}}
  end

  @impl true
  def handle_continue(:preamble, state) do
    # The preamble states what a consumer is reading rather than letting it
    # assume. #4758 is the lesson: a gate defaulted off and rendered nowhere
    # made two modeled invariants describe behaviour production never ran, and
    # nothing in the data said so. A harness preflight reads this record and
    # refuses to emit a PASS/FAIL verdict against a trace whose gate it cannot
    # confirm.
    preamble = %{
      "run_id" => state.run_id,
      "seq" => 0,
      "mono" => nil,
      "ts" => System.system_time(:millisecond),
      "spec" => "spec_trace",
      "action" => "preamble",
      "vars" => %{
        "schema_version" => Embervm.SpecTrace.schema_version(),
        "enabled" => Embervm.SpecTrace.enabled_now?(),
        "max_queue" => Embervm.SpecTrace.max_queue(),
        "segment_bytes" => state.cap,
        "segments" => state.segments
      }
    }
    {:noreply, write_record(state, preamble)}
  end

  @impl true
  def handle_info({:emit, spec, action, vars, mono, ts}, state) do
    seq = state.seq + 1
    record = %{"run_id" => state.run_id, "seq" => seq, "mono" => mono, "ts" => ts, "spec" => stringify(spec), "action" => stringify(action), "vars" => jsonable(vars)}
    {:noreply, write_record(%{state | seq: seq}, record)}
  rescue
    error ->
      Logger.warning("embervm spec_trace write failed", error: inspect(error))
      {:noreply, state}
  end

  @impl true
  def handle_call(:drain, _from, state), do: {:reply, :ok, state}

  @impl true
  def terminate(_reason, %{io: io}) when not is_nil(io), do: File.close(io)
  def terminate(_reason, _state), do: :ok

  defp write_record(state, record) do
    line = Jason.encode!(record) <> "\n"
    state = if state.size > 0 and state.size + byte_size(line) > state.cap, do: rotate(state), else: state

    case IO.binwrite(state.io, line) do
      :ok -> %{state | size: state.size + byte_size(line)}
      {:error, reason} ->
        Logger.warning("embervm spec_trace disk write failed", reason: inspect(reason))
        state
    end
  rescue
    error ->
      Logger.warning("embervm spec_trace serialization failed", error: inspect(error))
      state
  end

  defp rotate(state) do
    File.close(state.io)
    index = rem(state.index + 1, state.segments)
    {:ok, io} = File.open(segment_path(state.dir, index), [:write, :binary])
    %{state | index: index, size: 0, io: io}
  end

  defp open_segment(state) do
    File.mkdir_p!(state.dir)
    {:ok, io} = File.open(segment_path(state.dir, state.index), [:write, :binary])
    %{state | io: io}
  end

  defp segment_path(dir, index), do: Path.join(dir, "segment-#{String.pad_leading(Integer.to_string(index), 3, "0")}.ndjson")

  defp env_integer(name, default) do
    case Integer.parse(System.get_env(name, "")) do
      {value, ""} when value > 0 -> value
      _ -> default
    end
  end

  defp stringify(value) when is_atom(value), do: Atom.to_string(value)
  defp stringify(value), do: value

  defp jsonable(value) when is_atom(value), do: Atom.to_string(value)
  defp jsonable(value) when is_list(value), do: Enum.map(value, &jsonable/1)
  defp jsonable(value) when is_map(value), do: Map.new(value, fn {key, value} -> {stringify(key), jsonable(value)} end)
  defp jsonable(value), do: value

  defp uuid do
    <<a::binary-size(4), b::binary-size(2), c::binary-size(2), d::binary-size(2), e::binary-size(6)>> = :crypto.strong_rand_bytes(16)
    Enum.join(Enum.map([a, b, c, d, e], &Base.encode16(&1, case: :lower)), "-")
  end
end
