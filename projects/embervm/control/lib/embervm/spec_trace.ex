defmodule Embervm.SpecTrace do
  @moduledoc "Debug-only, spec-shaped trace emitted to a Store."

  @writer Embervm.SpecTrace.Writer
  @enabled_key {__MODULE__, :enabled}
  @dropped_key {__MODULE__, :dropped}
  @run_id_key {__MODULE__, :run_id}
  @writer_key {__MODULE__, :writer_name}
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

  @doc """
  The CP incarnation id, stable across writer restarts.

  Deliberately NOT minted in the writer's `init/1`. The writer is supervised, so
  a crash (a store call timing out, say) restarts it, and a per-writer id would
  mint a fresh `run_id` while the CONTROL PLANE had not restarted at all. A
  checker keying on run_id would read that as a crash-restart, which is exactly
  the event `adoption.tla` reasons about: the trace would fabricate the
  phenomenon it exists to observe.

  Generated once per BEAM and only if absent, so repeated `configure/0` calls
  from supervisor restarts do not replace it.
  """
  @spec run_id() :: String.t()
  def run_id do
    case :persistent_term.get(@run_id_key, nil) do
      nil ->
        id = fresh_run_id()
        :persistent_term.put(@run_id_key, id)
        id

      id ->
        id
    end
  end

  defp fresh_run_id do
    16 |> :crypto.strong_rand_bytes() |> Base.encode16(case: :lower)
  end

  @spec emit(atom() | String.t(), atom() | String.t(), map()) :: :ok
  def emit(spec, action, vars) when is_map(vars) do
    if :persistent_term.get(@enabled_key, false) do
      mono = System.monotonic_time(:nanosecond)
      ts = System.system_time(:millisecond)

      case Process.whereis(writer_name()) do
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

  @doc """
  The registered name `emit/3` resolves the writer through.

  Defaults to the module, which is production's only shape. A test can point
  emissions at ITS OWN writer via `scope_writer/1`, which matters because the
  writer was a single global name: a scenario that started a writer captured
  emissions from every other test in the same BEAM, so its store was per-test
  while its TRACE was not.

  That is not a cosmetic test concern. The restart-wedge scenario asserted on its
  own trace and caught `health_monotonic` failing for a node it never created,
  and the same mixing can just as easily SUPPLY the record that satisfies an
  invariant's precondition, so a passing run is not evidence either. #4833.

  Still one `:persistent_term.get/2` on the emit path, and the disabled path short
  circuits before reaching it, so the hot path cost is unchanged.
  """
  @spec writer_name() :: atom()
  def writer_name, do: :persistent_term.get(@writer_key, @writer)

  @doc """
  Point `emit/3` at `name` for the rest of this BEAM, or back at the default with
  `nil`. Test-only: production never calls this, and the default is the module.
  """
  @spec scope_writer(atom() | nil) :: :ok
  def scope_writer(nil), do: :persistent_term.put(@writer_key, @writer)
  def scope_writer(name) when is_atom(name), do: :persistent_term.put(@writer_key, name)

  @doc "Wait until all events already sent to the named writer are written."
  @spec drain(GenServer.server()) :: :ok
  def drain(server \\ @writer), do: GenServer.call(server, :drain)

  def start_link(opts \\ []) do
    # NO configure/0 here. It re-reads the env and stomps the persistent_term, so
    # any writer restart would revert whatever set the gate. Harmless while the
    # env is the only source of truth, and a silent disarm the moment anything
    # else can set it. Boot calls configure/0 once (application.ex). #4841.
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
  @doc false
  def note_drop, do: :persistent_term.put(@dropped_key, :persistent_term.get(@dropped_key, 0) + 1)
end

defmodule Embervm.SpecTrace.Writer do
  use GenServer
  require Logger

  @default_batch_size 100
  @default_flush_ms 1000

  def start_link(opts \\ []) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @impl true
  def init(opts) do
    store_mod = Keyword.get(opts, :store_mod, Embervm.SpecTrace.Store.SQLite)
    store = Keyword.get(opts, :store, store_mod)
    batch_size = Keyword.get(opts, :batch_size, @default_batch_size)
    flush_ms = Keyword.get(opts, :flush_ms, @default_flush_ms)
    # Embervm.SpecTrace.run_id/0, NOT a fresh id per writer: this process is
    # supervised, so a crash would otherwise mint a new incarnation id while the
    # control plane kept running, and a checker would read that as a
    # crash-restart that never happened.
    state = %{
      store_mod: store_mod,
      store: store,
      batch_size: batch_size,
      flush_ms: flush_ms,
      records: [],
      # RESUME, do not restart at 0. seq is the PRIMARY KEY in both stores, the
      # writer is supervised, and a crash that reset this to 0 made every
      # subsequent insert collide with an existing row. flush/1 swallows the
      # error as a counted drop, so the trace silently recorded NOTHING until seq
      # climbed past the old maximum, while reading as enabled the whole time.
      # #4841.
      seq: safe_max_seq(store_mod, store),
      run_id: Embervm.SpecTrace.run_id()
    }
    {:ok, state, {:continue, :preamble}}
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
      # A REAL mono. It was hardcoded to 1, and reads are ORDER BY mono ASC, so
      # two writer starts in one control-plane incarnation produced two records
      # both claiming to be first and a consumer could not order them.
      "run_id" => state.run_id, "seq" => state.seq + 1, "mono" => System.monotonic_time(:nanosecond),
      "ts" => System.system_time(:millisecond),
      "spec" => "spec_trace",
      "action" => "preamble",
      "vars" => %{
        "schema_version" => Embervm.SpecTrace.schema_version(),
        "enabled" => Embervm.SpecTrace.enabled_now?(),
        "max_queue" => Embervm.SpecTrace.max_queue(),
        "ttl_ms" => 86_400_000
      }
    }
    # ADVANCE state.seq past the preamble. It consumes a seq like any other
    # record, and leaving the counter behind would make the first emitted record
    # collide with it on the primary key.
    {:noreply, schedule_flush(%{state | seq: state.seq + 1, records: [preamble]})}
  end

  # A store that cannot answer yields 0, which is the pre-fix behaviour rather
  # than a crash: worst case the writer collides as it did before, best case it
  # resumes correctly. A debug facility must not take down its own supervisor
  # over a bookkeeping query.
  defp safe_max_seq(store_mod, store) do
    store_mod.max_seq(store)
  rescue
    _ -> 0
  catch
    _, _ -> 0
  end

  @impl true
  def handle_info({:emit, spec, action, vars, mono, ts}, state) do
    seq = state.seq + 1
    record = %{"run_id" => state.run_id, "seq" => seq, "mono" => mono, "ts" => ts, "spec" => stringify(spec), "action" => stringify(action), "vars" => jsonable(vars)}
    state = %{state | seq: seq, records: [record | state.records]}
    if length(state.records) >= state.batch_size, do: {:noreply, flush(state)}, else: {:noreply, schedule_flush(state)}
  rescue
    error ->
      Logger.warning("embervm spec_trace write failed", error: inspect(error))
      {:noreply, state}
  end

  def handle_info(:flush, state), do: {:noreply, flush(state)}

  @impl true
  def handle_call(:drain, _from, state), do: {:reply, :ok, flush(state)}

  defp flush(%{records: []} = state), do: schedule_flush(state)

  # A HANGING store must be survivable, not just an erroring one. The backends
  # write via GenServer.call, whose timeout raises an EXIT rather than an
  # exception, so `rescue` alone does not catch it and the writer would die.
  # That matters beyond losing a batch: a supervised restart used to mint a new
  # run_id, so a stalled Postgres insert would fabricate a control-plane
  # incarnation in the trace. run_id is now stable (see SpecTrace.run_id/0) and
  # the exit is caught here, so a stall costs counted drops and nothing else.
  defp flush(state) do
    case state.store_mod.write(state.store, Enum.reverse(state.records)) do
      :ok -> schedule_flush(%{state | records: []})
      {:error, reason} -> Embervm.SpecTrace.note_drop(); Logger.warning("embervm spec_trace store write failed", reason: inspect(reason)); schedule_flush(%{state | records: []})
    end
  catch
    :exit, reason ->
      Embervm.SpecTrace.note_drop()
      Logger.warning("embervm spec_trace store write timed out or died", reason: inspect(reason))
      schedule_flush(%{state | records: []})
  rescue
    error -> Embervm.SpecTrace.note_drop(); Logger.warning("embervm spec_trace store write failed", error: inspect(error)); schedule_flush(%{state | records: []})
  end
  defp schedule_flush(state), do: (Process.send_after(self(), :flush, state.flush_ms); state)

  defp stringify(value) when is_atom(value), do: Atom.to_string(value)
  defp stringify(value), do: value

  # Booleans and nil MUST come before the is_atom/1 clause. In Elixir `true`,
  # `false` and `nil` are atoms, so a bare is_atom/1 clause converts them to the
  # strings "true"/"false"/"nil". That is silently destructive here: JSON has
  # native booleans and null, the SQLite backend round-trips via term_to_binary
  # and would have preserved the real value, and every consumer comparing
  # `vars["gate"] == false` gets a mismatch that reads as "the condition is not
  # met" rather than as a type error.
  #
  # It cost a false PASS before it was caught. The destroy invariants compare
  # `gate` and `node_confirmed` against booleans; against stringified data the
  # vacuous arm never fires and the violation filter never matches, so the
  # checker reported PASS on a trace it had not actually evaluated. A
  # verification tool returning green because its own transport corrupted the
  # data is the exact failure this facility exists to detect.
  #
  # Non-boolean atoms are still stringified on purpose: `:adopted`, `:warm`,
  # `:miss` are enum-like labels and consumers compare them as strings.
  defp jsonable(value) when is_boolean(value) or is_nil(value), do: value
  defp jsonable(value) when is_atom(value), do: Atom.to_string(value)
  defp jsonable(value) when is_list(value), do: Enum.map(value, &jsonable/1)
  defp jsonable(value) when is_map(value), do: Map.new(value, fn {key, value} -> {stringify(key), jsonable(value)} end)
  defp jsonable(value), do: value

  defp uuid do
    <<a::binary-size(4), b::binary-size(2), c::binary-size(2), d::binary-size(2), e::binary-size(6)>> = :crypto.strong_rand_bytes(16)
    Enum.join(Enum.map([a, b, c, d, e], &Base.encode16(&1, case: :lower)), "-")
  end
end
