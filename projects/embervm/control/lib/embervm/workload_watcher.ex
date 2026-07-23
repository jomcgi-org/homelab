defmodule Embervm.WorkloadWatcher do
  @moduledoc """
  Keeps `Embervm.WorkloadCatalog`'s ETS table in sync with the `Workload`
  custom resources in the cluster, and writes each CR's `status` subresource
  back to reflect what the control plane concluded.

  ## list-then-watch (an informer)

  This is a Kubernetes informer, not a periodic poll: LIST to establish
  current truth, then WATCH for deltas. Concretely:

    * On boot (and on any resync) it does ONE LIST, which fully reconciles the
      catalog (add/update every CR, sweep any it no longer sees) and records
      the collection `resourceVersion`.
    * It then opens a streaming WATCH from that RV. Each delta (ADDED,
      MODIFIED, DELETED, BOOKMARK) is applied incrementally; the RV advances on
      every event so a later reconnect resumes exactly where it left off.
    * When the stream ends cleanly (the apiserver closes watches after a few
      minutes by design) it simply re-watches from the last RV. No relist.
    * When the RV has aged out of the apiserver's history (a 410 Expired,
      delivered as a terminal ERROR event), or on any transport/parse error,
      it resyncs: a fresh LIST (which also catches any deletes missed while
      disconnected, because the reconcile sweeps names the LIST no longer
      returns) followed by a new watch. Transport errors back off
      exponentially so a wedged apiserver is not hammered.

  This replaced an earlier periodic LIST loop. The public surface is
  unchanged: `Embervm.WorkloadCatalog` + `Embervm.TaskStore.cfg_for/1` read the
  same ETS table, so this is purely an internals swap. The motivation is
  apiserver load: a full LIST every interval couples steady-state cost to the
  poll cadence (we already saw a 429 on the boot LIST), whereas a watch is
  served from the apiserver's cache at near-zero incremental cost no matter how
  many Workloads exist.

  ## why the watch runs in a separate process

  `Embervm.K8s.watch_workloads/2` is synchronous: `Finch.stream/5` blocks its
  caller for the stream's entire multi-minute lifetime. If that ran inside this
  GenServer, every catalog read would serialize behind it and freeze. So the
  GenServer spawns a monitored streamer process that owns the blocking call and
  forwards each event back as a message; the GenServer keeps sole ownership of
  the ETS table, the RV, and all reconnect decisions (a serialized,
  crash-isolated state machine). A streamer crash is reported as a watch error
  and handled like any other disconnect; it never takes the GenServer down.

  ## invariants

    * A reconcile pass NEVER crashes the process, no matter how malformed one
      CR is (each CR is reconciled inside a try/catch), and NEVER wipes the
      catalog on a transient LIST failure (fail-open: the last-known-good
      catalog is served until the next successful LIST).
    * The watcher writes `status` ONLY, never `spec`.
    * Only events from the CURRENT streamer mutate state; a straggler event or
      result from a superseded streamer is ignored, so the RV never regresses.

  ## periodic catalog resync to the BaseBuilder (self-heal, RCA H1)

  `Embervm.BaseBuilder.reconcile/2` is a fire-and-forget `cast_if_alive` with no
  internal re-assert: if a single cast is dropped (the BaseBuilder momentarily
  down, a transient error, a restart race landing between the watcher's cast
  and the builder coming back up), nothing re-drives that Workload's base build
  and it stays unprovisioned indefinitely. This bit in production: a demo
  Workload's base was never (re)cast after a registry blip during the rootfs
  bake, and nothing noticed for hours (the incident this module's `@moduledoc`
  omits no longer, RCA candidate H1).

  The fix is a PERIODIC INTERNAL RESYNC, independent of the watch stream: every
  `resync_interval_ms` (default 60s, `EMBERVM_WORKLOAD_RESYNC_INTERVAL_MS`) the
  watcher re-casts `base_reconcile_fun` for every entry currently in
  `Embervm.WorkloadCatalog`, exactly as if each had just been freshly LISTed.
  This is intentionally NOT a relist: it does not touch the K8s API, the RV, or
  the watch/streamer state at all, so it cannot race or interfere with the
  list-then-watch state machine above; it only re-drives the base-build trigger
  from whatever the catalog already holds. `BaseBuilder.reconcile/2` is
  idempotent (an already-built-and-recorded base, or a build already
  queued/in-flight for the same signature, is a no-op; see its `@moduledoc`),
  so the redundant re-casts on every healthy tick cost nothing but a cheap
  no-op cast; the value is entirely in the tick where a prior cast was lost,
  which now self-heals within one interval instead of wedging forever. A
  `resync_interval_ms` of `0` disables the timer (opt-out, e.g. for tests that
  do not want a background tick).
  """

  use GenServer
  require Logger

  alias Embervm.WorkloadCatalog

  @default_table :embervm_workloads
  @base_backoff_ms 1_000
  @max_backoff_ms 30_000
  # The default stateful TCP listenPort range, matching the chart's values
  # default (projects/embervm/chart, Task 6). Overridable via the
  # :stateful_listen_range app env or a start_link opt so the chart value flows
  # in and tests can inject a narrow range.
  @default_stateful_listen_range 5400..5409
  # The default composite-group entry TCP listenPort range, matching the chart's
  # values default (compositeTcpPortRange 5410-5419, R5). Overridable via the
  # :composite_listen_range app env or a start_link opt, exactly like the stateful
  # range; distinct from it so the two classes never collide on a port.
  @default_composite_listen_range 5410..5419
  # The default cap on a composite group's EXPANDED member count (sum of replicas),
  # matching the chart's values default (maxGroupSize 4, R5). Overridable via the
  # :max_group_size app env or a start_link opt. A capacity guardrail: a group is a
  # set of live microVMs, so an unbounded member count is an unbounded resource claim.
  @default_max_group_size 4
  # The periodic BaseBuilder catalog-resync cadence (see this module's
  # @moduledoc, "periodic catalog resync to the BaseBuilder"), matching the
  # session/serving/stateful reconcile cadences' order of magnitude but longer:
  # this is a self-heal safety net for a dropped cast, not a hot adoption path,
  # so a minute is plenty responsive without adding steady-state cast volume.
  # Overridable via EMBERVM_WORKLOAD_RESYNC_INTERVAL_MS (Embervm.Application) or
  # a start_link opt (tests); 0 disables the timer entirely.
  @default_resync_interval_ms 60_000
  # A healthy watch lives for the apiserver's full `timeoutSeconds` (~5 min);
  # a watch that ends far sooner is a signal of trouble, not a normal cycle. So
  # only a watch that stayed open at least this long earns an IMMEDIATE
  # re-watch on clean close. A faster close is treated as suspect and backs off
  # first, which turns a pathological "apiserver accepts then instantly closes"
  # into bounded exponential backoff instead of a hot reconnect loop.
  @min_watch_ms 1_000

  @retry_on_map %{
    "transport" => :transport,
    "timeout" => :timeout,
    "guest5xx" => :guest5xx
  }

  # -- Client API ------------------------------------------------------------

  # :name defaults to __MODULE__ for the application's supervised singleton;
  # tests pass name: nil to get several independent, PID-addressed instances
  # running concurrently (the same idiom as Embervm.OpLog.SQLite/TaskStore).
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Forces one LIST + full reconcile synchronously and waits for it to finish.
  Tests drive the reconcile path entirely through this (with
  `watch_startup: false` so no streamer runs and no timer fires). In production
  it is also a safe operational nudge (reconcile now rather than waiting for
  the next watch event), and it does not disturb the running watch: it neither
  starts nor stops a streamer, it only re-runs the reconcile against a fresh
  LIST. It intentionally leaves the resume RV untouched when the injected
  lister returns no RV (the test path), so it never rewinds a live watch.
  """
  @spec reconcile_now(GenServer.server()) :: :ok
  def reconcile_now(server \\ __MODULE__) do
    GenServer.call(server, :reconcile_now)
  end

  @doc """
  Forces one periodic BaseBuilder resync pass synchronously (see this module's
  @moduledoc, "periodic catalog resync to the BaseBuilder"): re-casts every
  currently-cataloged Workload's build descriptor to `base_reconcile_fun`, the
  same work the `:resync_bases` timer does on its own cadence. Tests use this
  to drive the self-heal deterministically instead of waiting on a real timer;
  it never touches the watch/streamer state or the K8s API, so (like
  `reconcile_now/1`) it is also a safe operational nudge in production.
  """
  @spec resync_bases_now(GenServer.server()) :: :ok
  def resync_bases_now(server \\ __MODULE__) do
    GenServer.call(server, :resync_bases_now)
  end

  # -- GenServer callbacks -----------------------------------------------------

  @impl true
  def init(opts) do
    table = Keyword.get(opts, :table, @default_table)
    lister = Keyword.get(opts, :lister, &Embervm.K8s.list_workloads/0)
    watcher_fun = Keyword.get(opts, :watcher_fun, &Embervm.K8s.watch_workloads/2)
    status_writer = Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3)
    # The base-builder trigger seams (Task 10): a valid CR is handed to the
    # BaseBuilder to drive its base build; an invalid or deleted one is
    # forgotten. Both default to whereis-guarded no-ops when the builder is not
    # running, so this watcher's own unit tests need not start one.
    base_reconcile_fun = Keyword.get(opts, :base_reconcile_fun, &Embervm.BaseBuilder.reconcile/1)
    base_forget_fun = Keyword.get(opts, :base_forget_fun, &Embervm.BaseBuilder.forget/1)
    clock = Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end)
    base_backoff = Keyword.get(opts, :base_backoff_ms, @base_backoff_ms)
    max_backoff = Keyword.get(opts, :max_backoff_ms, @max_backoff_ms)
    min_watch = Keyword.get(opts, :min_watch_ms, @min_watch_ms)
    watch_startup = Keyword.get(opts, :watch_startup, true)
    # The values-declared stateful TCP listenPort range (Task 6 wires the chart
    # value in via config; the default matches the chart default 5400-5409). The
    # watcher validates every stateful workload's listenPort against it.
    stateful_listen_range =
      Keyword.get(opts, :stateful_listen_range) ||
        Application.get_env(:embervm, :stateful_listen_range, @default_stateful_listen_range)

    # The values-declared composite entry listenPort range + expanded-member-count
    # cap (R5): a start_link opt (tests) or the app-env keys Embervm.Application
    # populates in start/2 from EMBERVM_COMPOSITE_LISTEN_PORT_RANGE /
    # EMBERVM_MAX_GROUP_SIZE (the chart env), falling back to the compile-time
    # defaults that match the chart defaults when the env is absent or malformed.
    # The watcher validates every composite workload's entry.listenPort against the
    # range and its expanded member count against the cap.
    composite_listen_range =
      Keyword.get(opts, :composite_listen_range) ||
        Application.get_env(:embervm, :composite_listen_range, @default_composite_listen_range)

    max_group_size =
      Keyword.get(opts, :max_group_size) ||
        Application.get_env(:embervm, :max_group_size, @default_max_group_size)

    # The periodic BaseBuilder resync cadence: a start_link opt (tests) or the
    # app-env key Embervm.Application populates from
    # EMBERVM_WORKLOAD_RESYNC_INTERVAL_MS, falling back to the compile-time
    # default. 0 disables the timer.
    resync_interval_ms =
      Keyword.get(opts, :resync_interval_ms) ||
        Application.get_env(:embervm, :workload_resync_interval_ms, @default_resync_interval_ms)

    WorkloadCatalog.create(table)

    state = %{
      table: table,
      stateful_listen_range: stateful_listen_range,
      composite_listen_range: composite_listen_range,
      max_group_size: max_group_size,
      lister: lister,
      watcher_fun: watcher_fun,
      status_writer: status_writer,
      base_reconcile_fun: base_reconcile_fun,
      base_forget_fun: base_forget_fun,
      clock: clock,
      base_backoff: base_backoff,
      max_backoff: max_backoff,
      min_watch_ms: min_watch,
      backoff_ms: base_backoff,
      # Monotonic ms when the current watch was opened, used to tell a healthy
      # long-lived close (resume immediately) from a suspect fast close (back
      # off). Set on every start_streamer.
      watch_started_at: 0,
      # The resume point for the watch: the collection RV from the last LIST,
      # advanced by every applied event. nil means "start from the watch
      # cache" (resourceVersion=0). Tests that only exercise reconcile_now
      # leave it nil forever, which is fine.
      resource_version: nil,
      # {pid, monitor_ref} of the live streamer, or nil when no watch is open.
      streamer: nil,
      # Set true when a watch delivered a terminal ERROR (RV expired): the next
      # watch end must resync via LIST rather than blindly re-watching a
      # resourceVersion the apiserver no longer knows.
      needs_relist: false,
      resync_interval_ms: resync_interval_ms
    }

    # watch_startup drives the informer from init; tests set it false and drive
    # reconcile_now/1 explicitly so no background watch or timer ever fires.
    if watch_startup, do: send(self(), :start)

    # Arm the periodic BaseBuilder resync timer (see this module's @moduledoc,
    # "periodic catalog resync to the BaseBuilder"). Armed via a message sent
    # from init/1, exactly like :start above, NEVER a direct call here: this
    # runs while Embervm.Application's children list is still being
    # constructed, before Finch has started, and this same pattern is what
    # keeps that path Finch-free (see Embervm.Application's boot-ordering
    # note). base_reconcile_fun casts (see cast_if_alive), so even if this
    # fires before Finch is up it cannot crash boot. watch_startup gates it
    # exactly like :start so watcher unit tests (watch_startup: false) never
    # get a background timer either; a 0 interval also disables it
    # independent of watch_startup, for a test that wants the watch but not
    # the resync tick.
    if watch_startup and resync_interval_ms > 0, do: Process.send_after(self(), :resync_bases, resync_interval_ms)

    {:ok, state}
  end

  # Boot: LIST + reconcile, then open the watch. A failed boot LIST retries
  # with backoff before any watch is attempted (a watch needs a valid RV).
  @impl true
  def handle_info(:start, state), do: {:noreply, relist_then_watch(state)}

  # Backoff timer fired: resync (LIST + reconcile) and re-open the watch.
  def handle_info(:resync, state), do: {:noreply, relist_then_watch(state)}

  # Backoff timer fired: re-open the watch WITHOUT a resync (the RV is still
  # valid; the prior close was just suspiciously fast).
  def handle_info(:rewatch, state), do: {:noreply, start_streamer(state)}

  # One watch delta from the CURRENT streamer. Events from a superseded
  # streamer (a reconnect race) are dropped so a stale event cannot rewind the
  # catalog or the RV.
  def handle_info({:watch_event, pid, event}, %{streamer: {spid, _ref}} = state) when pid == spid do
    {:noreply, apply_event(state, event)}
  end

  def handle_info({:watch_event, _pid, _event}, state), do: {:noreply, state}

  # The current streamer finished (stream closed or errored). Decide the next
  # move: resume-watch on a clean close, resync on an expired RV or error.
  def handle_info({:watch_result, pid, result}, %{streamer: {spid, _ref}} = state) when pid == spid do
    {:noreply, handle_watch_end(result, %{state | streamer: nil})}
  end

  def handle_info({:watch_result, _pid, _result}, state), do: {:noreply, state}

  # The current streamer died WITHOUT reporting a result (it always sends one
  # in the normal path, so a bare DOWN here means an abnormal exit). Treat it
  # as a watch error and resync with backoff. A DOWN from a superseded streamer
  # (ref no longer matches) is expected and ignored.
  def handle_info({:DOWN, ref, :process, _pid, reason}, %{streamer: {_spid, sref}} = state)
      when ref == sref do
    {:noreply, handle_watch_end({:error, {:streamer_down, reason}}, %{state | streamer: nil})}
  end

  def handle_info({:DOWN, _ref, :process, _pid, _reason}, state), do: {:noreply, state}

  # The periodic BaseBuilder resync tick (see this module's @moduledoc,
  # "periodic catalog resync to the BaseBuilder", RCA H1): re-cast every
  # currently-cataloged Workload's build descriptor to base_reconcile_fun,
  # self-healing a cast the BaseBuilder never received (it was momentarily
  # down, hit a transient error, or the cast landed in a restart race).
  # Touches ONLY the catalog table and the BaseBuilder cast; it never calls the
  # K8s API, never re-derives the RV, and never disturbs the watch/streamer
  # state above, so it cannot race or interfere with the list-then-watch state
  # machine. Reschedules itself unconditionally (even if the resync work
  # itself raised, though catalog_resync/1 is defensive so that should not
  # happen) so a single bad tick never silently ends the self-heal loop.
  def handle_info(:resync_bases, state) do
    catalog_resync(state)
    if state.resync_interval_ms > 0, do: Process.send_after(self(), :resync_bases, state.resync_interval_ms)
    {:noreply, state}
  end

  @impl true
  def handle_call(:reconcile_now, _from, state) do
    # Fail-open on a LIST error: keep the last-known-good catalog either way.
    state =
      case do_list_reconcile(state) do
        {:ok, s} -> s
        {:error, s} -> s
      end

    {:reply, :ok, state}
  end

  def handle_call(:resync_bases_now, _from, state) do
    catalog_resync(state)
    {:reply, :ok, state}
  end

  # Best-effort: stop an orphaned streamer from outliving the GenServer holding
  # a pooled connection open. Not load-bearing for correctness (a send to a
  # dead GenServer is a no-op and the apiserver closes the stream at
  # timeoutSeconds anyway), just tidy.
  @impl true
  def terminate(_reason, %{streamer: {pid, _ref}}) do
    Process.exit(pid, :shutdown)
    :ok
  end

  def terminate(_reason, _state), do: :ok

  # -- informer state machine --------------------------------------------------

  # LIST + full reconcile, then (on success) open a watch from the resulting
  # RV. On a LIST failure, keep the prior catalog (fail-open) and retry with
  # backoff. This is the single entry point for both boot and every resync.
  defp relist_then_watch(state) do
    case do_list_reconcile(state) do
      {:ok, state} -> start_streamer(%{state | backoff_ms: state.base_backoff})
      {:error, state} -> schedule(:resync, state)
    end
  end

  # Spawn the streamer that owns the blocking watch. spawn_monitor (not link):
  # a streamer crash surfaces as a DOWN we handle as a disconnect, and never
  # escalates to this GenServer. The streamer forwards each event tagged with
  # its own pid (so superseded-streamer events are droppable) and always
  # reports a final result, even if the watch call itself raises.
  defp start_streamer(state) do
    owner = self()
    rv = state.resource_version
    watcher_fun = state.watcher_fun

    {pid, ref} =
      spawn_monitor(fn ->
        streamer = self()

        result =
          try do
            watcher_fun.(rv, fn event -> send(owner, {:watch_event, streamer, event}) end)
          catch
            kind, reason -> {:error, {kind, reason}}
          end

        send(owner, {:watch_result, streamer, result})
      end)

    %{state | streamer: {pid, ref}, needs_relist: false, watch_started_at: now_ms()}
  end

  # A watch ended; decide how to resume. Four cases, gated on two axes: was the
  # close clean-and-long-lived (healthy) vs fast/errored, and does the RV need
  # a fresh LIST (an ERROR-flagged resync, a nil RV, or a non-clean end) vs is
  # it still valid to re-watch from.
  #
  #   healthy + valid RV  → resume the watch immediately (the ~5-min server
  #                         timeout path; reset backoff).
  #   healthy + must list → resync now (RV expired on a long-lived watch).
  #   fast/error + list   → back off, then resync.
  #   fast clean + valid  → back off, then RE-watch (no relist). This is the
  #                         hot-loop guard: an apiserver that accepts then
  #                         instantly closes backs off instead of spinning.
  defp handle_watch_end(result, state) do
    clean = match?({:ok, :closed}, result)
    long_lived = clean and now_ms() - state.watch_started_at >= state.min_watch_ms
    must_relist = state.needs_relist or is_nil(state.resource_version) or not clean

    cond do
      long_lived and not must_relist ->
        start_streamer(%{state | backoff_ms: state.base_backoff})

      long_lived ->
        Logger.info("embervm workload watcher: resyncing (watch RV invalid or expired)")
        relist_then_watch(%{state | backoff_ms: state.base_backoff})

      must_relist ->
        Logger.warning(
          "embervm workload watcher: watch ended (#{inspect(result)}), resync in #{state.backoff_ms}ms"
        )

        schedule(:resync, state)

      true ->
        Logger.warning(
          "embervm workload watcher: watch closed immediately, re-watch in #{state.backoff_ms}ms"
        )

        schedule(:rewatch, state)
    end
  end

  # Arm a backoff timer for the next reconnect attempt (:resync relists first,
  # :rewatch does not) and double the backoff (capped) for the next consecutive
  # failure. A success resets backoff to base (see relist_then_watch /
  # start_streamer / the healthy branch above).
  defp schedule(msg, state) do
    Process.send_after(self(), msg, state.backoff_ms)
    %{state | backoff_ms: min(state.backoff_ms * 2, state.max_backoff)}
  end

  defp now_ms, do: System.monotonic_time(:millisecond)

  # -- event application -------------------------------------------------------

  defp apply_event(state, %{"type" => type} = event) when type in ["ADDED", "MODIFIED"] do
    obj = Map.get(event, "object") || %{}
    catalog_cr(state, obj)
    advance_rv(state, obj)
  end

  defp apply_event(state, %{"type" => "DELETED"} = event) do
    obj = Map.get(event, "object") || %{}
    name = get_in(obj, ["metadata", "name"])

    if name do
      WorkloadCatalog.drop(state.table, name)
      state.base_forget_fun.(name)
    end

    advance_rv(state, obj)
  end

  # BOOKMARK carries only a fresh resourceVersion (empty object otherwise): it
  # exists precisely so an idle watch keeps advancing the RV we would resume
  # from, so applying it means updating the RV and nothing else.
  defp apply_event(state, %{"type" => "BOOKMARK"} = event) do
    advance_rv(state, Map.get(event, "object") || %{})
  end

  # ERROR is terminal: the apiserver emits it (typically a 410 Expired Status
  # object) and closes the stream. Flag a resync so the ensuing watch-end takes
  # the LIST path instead of re-watching an RV the apiserver has forgotten. Do
  # NOT advance the RV from the error object.
  defp apply_event(state, %{"type" => "ERROR"} = event) do
    Logger.info(
      "embervm workload watcher: watch ERROR event, will resync: #{inspect(Map.get(event, "object"))}"
    )

    %{state | needs_relist: true}
  end

  defp apply_event(state, _event), do: state

  # Advance the resume RV to this object's resourceVersion. Guarded so a delta
  # missing an RV (or a malformed object) leaves the last good RV in place
  # rather than nilling it out.
  defp advance_rv(state, obj) do
    case get_in(obj, ["metadata", "resourceVersion"]) do
      rv when is_binary(rv) and rv != "" -> %{state | resource_version: rv}
      _ -> state
    end
  end

  # -- periodic BaseBuilder resync ---------------------------------------------

  # Re-cast every currently-cataloged Workload's build descriptor to
  # base_reconcile_fun (see this module's @moduledoc, "periodic catalog resync
  # to the BaseBuilder"). Reads ONLY WorkloadCatalog (never the K8s API), so a
  # tick can never race the list-then-watch state machine, and iterates the
  # catalog fresh each call so a name dropped between ticks (a delete swept by
  # a LIST/WATCH reconcile) is simply absent, never re-cast. A name whose
  # fetch races a concurrent drop (vanishingly unlikely: this GenServer is the
  # sole writer to its own table) is skipped rather than crashing the tick.
  # BaseBuilder.reconcile/2 is idempotent (see its @moduledoc): an
  # already-built-and-recorded base, or a build already queued/in-flight for
  # the identical signature, is a no-op there, so re-casting a healthy
  # Workload costs one cheap no-op cast; the payoff is entirely in the tick
  # where an EARLIER cast for this same Workload was lost, which now
  # self-heals within one interval instead of wedging forever.
  defp catalog_resync(state) do
    state.table
    |> WorkloadCatalog.all_names()
    |> Enum.each(fn name -> resync_one(state, name) end)
  end

  # Re-cast a single cataloged name, guarded exactly like catalog_cr/2: ONE bad
  # entry (a fetch racing a concurrent drop, or a build_desc/base_reconcile_fun
  # that raises) must never crash this GenServer or abort the rest of the
  # tick, since a bad entry sitting in the catalog would otherwise permanently
  # wedge the self-heal for every OTHER, healthy Workload too.
  defp resync_one(state, name) do
    case WorkloadCatalog.fetch(state.table, name) do
      {:ok, entry} -> state.base_reconcile_fun.(build_desc(entry))
      :error -> :ok
    end
  catch
    kind, reason ->
      Logger.warning(
        "embervm workload watcher: periodic resync crashed on #{inspect(name)}, skipping: " <>
          inspect({kind, reason})
      )
  end

  # -- reconcile ---------------------------------------------------------------

  # LIST the whole collection and reconcile the catalog to match. Accepts the
  # injected lister returning either {:ok, items, rv} (production
  # Embervm.K8s.list_workloads/0) or {:ok, items} (test listers that do not
  # model RV); the latter leaves the resume RV untouched so reconcile_now never
  # rewinds a live watch.
  defp do_list_reconcile(state) do
    case state.lister.() do
      {:error, reason} ->
        # Fail-open: a transient LIST error (apiserver hiccup, network blip)
        # must never wipe the last-known-good catalog. TaskStore keeps
        # classifying failures against whatever config it already has.
        Logger.warning("embervm workload watcher: list failed, keeping prior catalog: #{inspect(reason)}")
        {:error, state}

      {:ok, crs} ->
        {:ok, reconcile_full(state, crs, nil)}

      {:ok, crs, rv} ->
        {:ok, reconcile_full(state, crs, rv)}
    end
  end

  # Reconcile every CR in a LIST, then sweep any cataloged name the LIST no
  # longer returns (this is how the informer recovers deletes it may have
  # missed while disconnected). Records the collection RV as the new resume
  # point when the lister supplied one.
  defp reconcile_full(state, crs, rv) do
    seen =
      crs
      |> Enum.map(&catalog_cr(state, &1))
      |> Enum.reject(&is_nil/1)

    (WorkloadCatalog.all_names(state.table) -- seen)
    |> Enum.each(fn name ->
      WorkloadCatalog.drop(state.table, name)
      state.base_forget_fun.(name)
    end)

    if rv, do: %{state | resource_version: rv}, else: state
  end

  # Reconciles exactly one CR (validate → catalog upsert-or-drop → status
  # write) and returns its name, so a full reconcile can build the "seen" set
  # for delete-sweeping, whether or not the CR was valid. Wrapped in try/catch:
  # ONE malformed CR (missing metadata, a spec shaped nothing like the CRD
  # schema) must never crash the loop or the GenServer itself, since a bad CR
  # sitting in the cluster would otherwise permanently wedge reconciliation for
  # every OTHER, valid Workload too.
  defp catalog_cr(state, cr) do
    try do
      name = get_in(cr, ["metadata", "name"])
      namespace = get_in(cr, ["metadata", "namespace"])
      generation = get_in(cr, ["metadata", "generation"])
      spec = Map.get(cr, "spec") || %{}

      case validate(state, name, spec) do
        {:ok, class, floor, cap, session_cfg, serving_cfg, stateful_cfg, group_cfg} ->
          entry =
            catalog_entry(
              name,
              namespace,
              generation,
              spec,
              class,
              floor,
              cap,
              session_cfg,
              serving_cfg,
              stateful_cfg,
              group_cfg
            )

          WorkloadCatalog.upsert(state.table, name, entry)

          # A valid Workload's Ready/BaseBuilt conditions are owned by the
          # BaseBuilder (Task 10): it drives the base build and writes those
          # conditions plus snapshotRef/snapshotDigest. The watcher writes only
          # its own disjoint keys here (observedGeneration, primedFloorSatisfied)
          # so the two merge-patches never clobber each other's conditions
          # array, and hands the build descriptor to the builder.
          write_valid_status(state, namespace, name, generation)
          state.base_reconcile_fun.(build_desc(entry))

        {:error, reason_code, message} ->
          # Never serve an invalid CR: drop it from the catalog (it may have
          # been valid before an edit made it invalid) rather than leaving a
          # stale entry that no longer matches spec. Forget any base build for it
          # too (a valid->invalid edit must stop building and clear its state);
          # the watcher owns the Ready condition for the invalid lane.
          WorkloadCatalog.drop(state.table, name)
          state.base_forget_fun.(name)
          write_status(state, namespace, name, generation, ready_condition(state, "False", reason_code, message))
      end

      name
    catch
      kind, reason ->
        Logger.warning(
          "embervm workload watcher: catalog_cr crashed on a malformed CR, skipping: " <>
            inspect({kind, reason})
        )

        get_in(cr, ["metadata", "name"])
    end
  end

  # Status for a VALID Workload: only the keys the watcher owns. Crucially it
  # does NOT include `conditions` (owned by the BaseBuilder) NOR
  # `primedFloorSatisfied` (owned by the PoolManager, Task 11): merge-patch
  # replaces arrays and overwrites keys wholesale, so the three writers coexist
  # only by keeping their key sets disjoint. The watcher owns observedGeneration
  # alone for the valid lane.
  defp write_valid_status(state, namespace, name, generation) do
    status_map = %{
      "observedGeneration" => generation
    }

    case state.status_writer.(namespace, name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        Logger.warning(
          "embervm workload watcher: status patch failed for #{namespace}/#{name}: #{inspect(reason)}"
        )
    end
  end

  # The build descriptor handed to the BaseBuilder for a valid Workload: the
  # spec fields that shape the base plus the identity to write status back
  # under. Built ENTIRELY from the already-parsed catalog entry (including
  # entry.init_env, computed once in catalog_entry/11) so this same function
  # faithfully rebuilds the identical descriptor both on a fresh LIST/WATCH
  # event AND from the periodic BaseBuilder resync (see this module's
  # @moduledoc, "periodic catalog resync"), which reads only the catalog and
  # never re-fetches the CR.
  defp build_desc(entry) do
    %{
      name: entry.name,
      namespace: entry.namespace,
      generation: entry.generation,
      # class lets the BaseBuilder mark a serving base's BuildBase so noded also
      # produces the cold-boot handler artifact (D-R3.11.2). Task/session bases carry
      # class but never set the serving flag.
      class: entry.class,
      # Exactly one of image_ref / zip is set, mirroring the CR's oneOf source.
      image_ref: entry.image_ref,
      zip: entry.zip,
      guest_port: entry.port,
      ready_path: entry.ready_path,
      vcpus: entry.vcpus,
      mem_mib: entry.mem_mib,
      init_env: entry.init_env
    }
  end

  defp write_status(state, namespace, name, generation, condition) do
    status_map = %{
      "observedGeneration" => generation,
      "conditions" => [condition],
      "primedFloorSatisfied" => false
    }

    case state.status_writer.(namespace, name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        # A status-write failure is visibility-only: it must not crash the
        # loop or block cataloging (the catalog write above already
        # happened), so it is logged and swallowed.
        Logger.warning(
          "embervm workload watcher: status patch failed for #{namespace}/#{name}: #{inspect(reason)}"
        )
    end
  end

  defp ready_condition(state, status, reason, message) do
    %{
      "type" => "Ready",
      "status" => status,
      "reason" => reason,
      "message" => message,
      "lastTransitionTime" => iso8601(state.clock.())
    }
  end

  defp iso8601(ms) do
    ms
    |> DateTime.from_unix!(:millisecond)
    |> DateTime.to_iso8601()
  end

  # -- validation ----------------------------------------------------------

  # Returns {:ok, class, floor, cap, session_cfg, serving_cfg, stateful_cfg,
  # group_cfg} with just the pieces catalog_entry needs (each *_cfg is nil except
  # for its own class), or {:error, reason_code, message} for a status condition.
  # The CRD schema (workload-crd.yaml) enforces shape (required fields, enums,
  # min/max) at admission; what's left for the watcher is the cross-field/semantic
  # rules the OpenAPI schema cannot express (class allow-list beyond the enum, the
  # oneOf source lane, cap >= floor, the class-conditional session/serving/stateful/
  # group blocks, the serving cap/maxInstances alias guard, cross-CR duplicate-host/
  # port rejection, and the composite member/entry/size cross-field checks).
  defp validate(state, name, spec) do
    with {:ok, class} <- validate_class(spec),
         :ok <- validate_source(spec),
         {:ok, floor, cap} <- validate_concurrency(spec, class),
         {:ok, session_cfg} <- validate_session(spec, class),
         {:ok, serving_cfg} <- validate_serving(state, name, spec, class, cap),
         {:ok, stateful_cfg} <- validate_stateful(state, name, spec, class),
         {:ok, group_cfg} <- validate_group(state, name, spec, class) do
      {:ok, class, floor, cap, session_cfg, serving_cfg, stateful_cfg, group_cfg}
    end
  end

  defp validate_class(%{"class" => "task"}), do: {:ok, "task"}
  defp validate_class(%{"class" => "session"}), do: {:ok, "session"}
  defp validate_class(%{"class" => "serving"}), do: {:ok, "serving"}
  defp validate_class(%{"class" => "stateful"}), do: {:ok, "stateful"}
  defp validate_class(%{"class" => "composite"}), do: {:ok, "composite"}

  defp validate_class(spec) do
    {:error, "ClassUnsupported",
     "class #{inspect(Map.get(spec, "class"))} is reserved for a later rung; only task, session, serving, stateful, and composite are valid in v1alpha1"}
  end

  # The session block is REQUIRED for the session class and FORBIDDEN for the
  # task class (the class-conditional requiredness the OpenAPI schema cannot
  # express). Each mismatch is a precise Ready=False, never a crash (R0 posture).
  # Session config numeric fields are re-defaulted here (mirroring the CRD) so
  # this code has no silent dependency on apiserver defaulting having run.
  @session_defaults %{
    idle_bank_seconds: 300,
    max_lifetime_seconds: 86_400,
    max_sessions: 16,
    invoke_queue_cap: 4
  }

  defp validate_session(spec, class) when class in ["task", "serving", "stateful", "composite"] do
    case Map.get(spec, "session") do
      nil -> {:ok, nil}
      _ -> {:error, "SessionSpecUnexpected", "spec.session is only valid for class session, not #{class}"}
    end
  end

  defp validate_session(spec, "session") do
    case Map.get(spec, "session") do
      s when is_map(s) ->
        {:ok, parse_session(s)}

      _ ->
        {:error, "SessionSpecMissing", "class session requires a spec.session block"}
    end
  end

  # bankedTtlSeconds defaults to maxLifetimeSeconds when omitted (the plan's
  # rule), which is why it is not defaulted in the CRD schema: the watcher needs
  # to see the omission to apply that class-coupled default.
  defp parse_session(s) do
    max_lifetime = Map.get(s, "maxLifetimeSeconds") || @session_defaults.max_lifetime_seconds

    %{
      idle_bank_seconds: Map.get(s, "idleBankSeconds") || @session_defaults.idle_bank_seconds,
      max_lifetime_seconds: max_lifetime,
      banked_ttl_seconds: Map.get(s, "bankedTtlSeconds") || max_lifetime,
      max_sessions: Map.get(s, "maxSessions") || @session_defaults.max_sessions,
      invoke_queue_cap: Map.get(s, "invokeQueueCap") || @session_defaults.invoke_queue_cap
    }
  end

  # The serving block is REQUIRED for the serving class and FORBIDDEN for task
  # and session (the class-conditional requiredness the OpenAPI schema cannot
  # express), mirroring validate_session/2 exactly. Two additional serving-only
  # rules beyond presence: the concurrency.cap/maxInstances alias guard (they
  # must agree when both are set), and cross-CR duplicate-host rejection (one
  # hostname, one workload, v1) checked against the LIVE catalog so it sees
  # every other already-cataloged serving workload regardless of LIST order.
  # Serving config numeric fields are re-defaulted here (mirroring the CRD),
  # the same "no silent dependency on apiserver defaulting" rule as sessions.
  @serving_defaults %{
    health_path: "/healthz",
    min_instances: 0,
    max_instances: 2,
    idle_bank_seconds: 300,
    drain_seconds: 5,
    max_lifetime_seconds: 86_400,
    # A banked serving snapshot untouched this long is GC'd (Task 9 banked-TTL
    # sweep), mirroring the session bankedTtlSeconds. Defaults to max_lifetime so
    # a workload that never sets it holds banked snapshots no longer than a live
    # instance would live, and the GC is a no-op relative to lifetime expiry.
    banked_ttl_seconds: 86_400
  }

  defp validate_serving(_state, _name, spec, class, _cap)
       when class in ["task", "session", "stateful", "composite"] do
    case Map.get(spec, "serving") do
      nil -> {:ok, nil}
      _ -> {:error, "ServingSpecUnexpected", "spec.serving is only valid for class serving, not #{class}"}
    end
  end

  defp validate_serving(state, name, spec, "serving", cap) do
    case Map.get(spec, "serving") do
      s when is_map(s) ->
        with :ok <- validate_serving_cap_alias(s, cap),
             :ok <- validate_serving_host_unique(state, name, s) do
          {:ok, parse_serving(s)}
        end

      _ ->
        {:error, "ServingSpecMissing", "class serving requires a spec.serving block"}
    end
  end

  # concurrency.cap is REQUIRED by the CRD schema (spec.concurrency.cap), so it
  # is always an integer here; maxInstances is optional (CRD-defaulted to 2).
  # When the CR sets maxInstances explicitly it MUST agree with cap, or the
  # two knobs silently disagree about the hard max (a config a human reading
  # only one of the two fields would misjudge). Omitting maxInstances (letting
  # it default) never conflicts, since cap alone is authoritative then.
  defp validate_serving_cap_alias(s, cap) do
    case Map.get(s, "maxInstances") do
      nil ->
        :ok

      max_instances when max_instances == cap ->
        :ok

      max_instances ->
        {:error, "ConcurrencyServingMismatch",
         "spec.serving.maxInstances (#{inspect(max_instances)}) must equal spec.concurrency.cap (#{inspect(cap)}) when both are set"}
    end
  end

  # Scans the live catalog for any OTHER serving workload already holding this
  # host (self excluded, so a CR re-validating against its own prior entry on
  # every LIST/watch cycle never collides with itself). One hostname, one
  # workload in v1: a second serving Workload declaring an already-owned host
  # is condition-rejected rather than admitted, so there is never an ambiguous
  # routing target. Checked against WorkloadCatalog (not watcher-local state)
  # so this is correct from both the single-CR watch-event path and the
  # full-LIST reconcile path, and self-heals if the collision is resolved
  # later (the losing CR's next reconcile re-validates and can now succeed).
  # The apiserver's LIST order is not guaranteed stable across reconciles, so
  # WHICH of two same-host CRs wins the first-listed tiebreak may differ
  # between passes; this is harmless, since both orderings reject exactly one
  # of the two CRs and there is never an ambiguous routing target either way,
  # it is an operator config error regardless of which CR loses.
  defp validate_serving_host_unique(state, name, s) do
    host = Map.get(s, "host")

    conflict =
      state.table
      |> WorkloadCatalog.all_names()
      |> Enum.reject(&(&1 == name))
      |> Enum.find(fn other_name ->
        case WorkloadCatalog.fetch(state.table, other_name) do
          {:ok, %{class: "serving", serving: %{host: ^host}}} -> true
          _ -> false
        end
      end)

    case conflict do
      nil -> :ok
      other -> {:error, "ServingHostConflict", "spec.serving.host #{inspect(host)} is already owned by workload #{inspect(other)}"}
    end
  end

  defp parse_serving(s) do
    %{
      port: Map.get(s, "port"),
      health_path: Map.get(s, "healthPath") || @serving_defaults.health_path,
      host: Map.get(s, "host"),
      min_instances: Map.get(s, "minInstances") || @serving_defaults.min_instances,
      max_instances: Map.get(s, "maxInstances") || @serving_defaults.max_instances,
      idle_bank_seconds: Map.get(s, "idleBankSeconds") || @serving_defaults.idle_bank_seconds,
      drain_seconds: Map.get(s, "drainSeconds") || @serving_defaults.drain_seconds,
      max_lifetime_seconds: Map.get(s, "maxLifetimeSeconds") || @serving_defaults.max_lifetime_seconds,
      # Defaults to maxLifetimeSeconds (see @serving_defaults) so an unset banked
      # TTL never outlives what a live instance would.
      banked_ttl_seconds:
        Map.get(s, "bankedTtlSeconds") || Map.get(s, "maxLifetimeSeconds") ||
          @serving_defaults.banked_ttl_seconds,
      # ADR embervm/018 Fork A. node_local_wake gates the brick activator per
      # workload: only a true workload is eligible for a node-local cold boot (it is
      # pushed to noded in the RegistryEntry and drives CP-side ACTIVATOR adoption).
      # metering_fail_open is the companion, EXPLICIT policy that a node_local_wake
      # workload wakes unmetered during a CP gap and reconciles best-effort on adopt
      # (named, not implied). Both default false: a workload that sets neither keeps
      # today's CP-only wake with synchronous metering.
      node_local_wake: Map.get(s, "nodeLocalWake") == true,
      metering_fail_open: Map.get(s, "meteringFailOpen") == true
    }
  end

  # The stateful block is REQUIRED for the stateful class and FORBIDDEN for every
  # other class (the class-conditional requiredness the OpenAPI schema cannot
  # express), mirroring validate_serving/5. Three stateful-only cross-field rules
  # beyond presence: listenPort must fall inside the chart's values-declared TCP
  # range (state.stateful_listen_range), listenPort must be unique across live
  # stateful workloads (one port, one workload, decision 5, checked against the
  # LIVE catalog like the serving host-uniqueness rule), and volumeSizeGiB is
  # immutable in v1 (an edit against an already-cataloged size is rejected).
  @stateful_defaults %{
    volume_mount_path: "/data",
    idle_bank_seconds: 300,
    max_lifetime_seconds: 86_400,
    banked_ttl_seconds: 604_800,
    wake_timeout_seconds: 60,
    # Opt in to the two-phase interruptible bank (ADR embervm/008). Off = the
    # atomic pause-snapshot-destroy bank, unchanged. A boolean for a single
    # alternative behavior; a future third bank strategy is expected to
    # supersede this with a `bankMode` enum.
    interruptible_bank: false,
    # Opt in to POST-BASE-READY auto-wake: when the CP observes this workload's
    # base transition to READY and it has no live or banked instance, the
    # StatefulManager reconcile triggers a wake (instance-key unification PR-B0a,
    # A2). Off = the workload wakes lazily on the first connection. Set for
    # demo-postgres so a chart bump's noded roll (which rebuilds the base) does
    # not leave the public /health demo wedged until a manual forced wake.
    auto_wake: false
  }
  # The minimum idleBankSeconds; the CRD schema also enforces this, re-checked
  # here for the LIST/WATCH path. Floor lowered 30 -> 1 for the demo-postgres
  # exhibit (the demos page deliberately banks ~a tick after each query so the
  # sleep/wake cycle is watchable). The original 30s guard kept a tenant from
  # ACCIDENTALLY configuring wake/bank thrash; a sub-30 value is now an informed
  # opt-in, the default (300s) still protects the unconfigured path, and zero
  # stays invalid (a 0 window would bank with no idle observation at all).
  @stateful_min_idle_bank_seconds 1

  defp validate_stateful(_state, _name, spec, class)
       when class in ["task", "session", "serving", "composite"] do
    case Map.get(spec, "stateful") do
      nil -> {:ok, nil}
      _ -> {:error, "StatefulSpecUnexpected", "spec.stateful is only valid for class stateful, not #{class}"}
    end
  end

  defp validate_stateful(state, name, spec, "stateful") do
    case Map.get(spec, "stateful") do
      s when is_map(s) ->
        with :ok <- validate_stateful_listen_port(state, name, s),
             :ok <- validate_stateful_idle_bank(s),
             :ok <- validate_stateful_volume_immutable(state, name, s) do
          {:ok, parse_stateful(s)}
        end

      _ ->
        {:error, "StatefulSpecMissing", "class stateful requires a spec.stateful block"}
    end
  end

  # listenPort must be inside the values-declared range AND unique across every
  # other live stateful workload. Out-of-range names the configured range in the
  # condition (operator-actionable); a duplicate names the conflicting workload.
  defp validate_stateful_listen_port(state, name, s) do
    listen_port = Map.get(s, "listenPort")
    range = state.stateful_listen_range

    cond do
      not is_integer(listen_port) ->
        {:error, "InvalidStatefulSpec", "spec.stateful.listenPort is required and must be an integer"}

      listen_port not in range ->
        {:error, "StatefulListenPortOutOfRange",
         "spec.stateful.listenPort #{listen_port} is outside the configured stateful range #{Enum.min(range)}-#{Enum.max(range)}"}

      true ->
        case stateful_listen_port_conflict(state, name, listen_port) do
          nil -> :ok
          other ->
            {:error, "StatefulListenPortConflict",
             "spec.stateful.listenPort #{listen_port} is already owned by workload #{inspect(other)}"}
        end
    end
  end

  defp stateful_listen_port_conflict(state, name, listen_port) do
    state.table
    |> WorkloadCatalog.all_names()
    |> Enum.reject(&(&1 == name))
    |> Enum.find(fn other_name ->
      case WorkloadCatalog.fetch(state.table, other_name) do
        {:ok, %{class: "stateful", stateful: %{listen_port: ^listen_port}}} -> true
        _ -> false
      end
    end)
  end

  defp validate_stateful_idle_bank(s) do
    case Map.get(s, "idleBankSeconds") do
      nil ->
        :ok

      idle when is_integer(idle) and idle >= @stateful_min_idle_bank_seconds ->
        :ok

      idle ->
        {:error, "InvalidStatefulSpec",
         "spec.stateful.idleBankSeconds #{inspect(idle)} must be >= #{@stateful_min_idle_bank_seconds}"}
    end
  end

  # volumeSizeGiB is immutable in v1 (resize is a recorded follow-on): an edit
  # against an already-cataloged size is rejected so a resize attempt surfaces
  # loudly rather than silently doing nothing (the volume file is not resized).
  # Compared against the LIVE catalog entry (self, by name), so the first
  # cataloging of a workload always passes and only a later size change trips.
  defp validate_stateful_volume_immutable(state, name, s) do
    new_size = Map.get(s, "volumeSizeGiB")

    case WorkloadCatalog.fetch(state.table, name) do
      {:ok, %{class: "stateful", stateful: %{volume_size_gib: existing}}}
      when is_integer(existing) and existing != new_size ->
        {:error, "StatefulVolumeSizeImmutable",
         "spec.stateful.volumeSizeGiB is immutable (was #{existing}, got #{inspect(new_size)}); volume resize is not supported in v1"}

      _ ->
        :ok
    end
  end

  defp parse_stateful(s) do
    %{
      port: Map.get(s, "port"),
      listen_port: Map.get(s, "listenPort"),
      volume_size_gib: Map.get(s, "volumeSizeGiB"),
      volume_mount_path: Map.get(s, "volumeMountPath") || @stateful_defaults.volume_mount_path,
      idle_bank_seconds: Map.get(s, "idleBankSeconds") || @stateful_defaults.idle_bank_seconds,
      max_lifetime_seconds: Map.get(s, "maxLifetimeSeconds") || @stateful_defaults.max_lifetime_seconds,
      banked_ttl_seconds: Map.get(s, "bankedTtlSeconds") || @stateful_defaults.banked_ttl_seconds,
      wake_timeout_seconds: Map.get(s, "wakeTimeoutSeconds") || @stateful_defaults.wake_timeout_seconds,
      # Boolean-|| is safe here ONLY because the default is false (false || false
      # == false; true || false == true). If a future bankMode default flips to a
      # truthy value this must switch to Map.get/3 with an explicit default to
      # avoid the Elixir truthiness trap.
      interruptible_bank: Map.get(s, "interruptibleBank") || @stateful_defaults.interruptible_bank,
      # Boolean-|| safe only because the default is false (same trap note as
      # interruptible_bank above): auto-wake this workload once its base is READY.
      auto_wake: Map.get(s, "autoWake") || @stateful_defaults.auto_wake,
      # secretRef (R4, D-R4.PR-7.1: MMDS-lite over boot-args): the NAME of a K8s
      # Secret in the workload's OWN namespace whose data keys/values become the
      # guest's first-boot process env (e.g. POSTGRES_PASSWORD). Optional; nil
      # when absent, matching every other optional stateful field's shape.
      # Embervm.StatefulManager reads it (via Embervm.K8s.get_secret) ONLY on a
      # FRESH/COLD wake, never on RELIGHT.
      secret_ref: Map.get(s, "secretRef"),
      # ADR embervm/018 Fork A Phase 2: node_local_wake gates the brick's L4
      # activator per stateful workload (pushed to noded in the RegistryEntry and
      # drives CP-side ACTIVATOR adoption). metering_fail_open is the companion
      # named policy. Both default false: a stateful workload that sets neither keeps
      # CP-only wake via the CP tcp_activator.
      node_local_wake: Map.get(s, "nodeLocalWake") == true,
      metering_fail_open: Map.get(s, "meteringFailOpen") == true
    }
  end

  # The group block is REQUIRED for the composite class and FORBIDDEN for every
  # other class (the class-conditional requiredness the OpenAPI schema cannot
  # express), mirroring validate_stateful/4. The composite-only cross-field rules
  # beyond presence: member names are unique DNS labels, the expanded member count
  # (sum of replicas) is within maxGroupSize, entry.member names a declared member
  # or an expanded replica name, entry.listenPort falls inside the values-declared
  # composite range AND is unique across live composite workloads (one port, one
  # group, checked against the LIVE catalog like the stateful port rule).
  @group_defaults %{
    idle_bank_seconds: 600,
    max_lifetime_seconds: 86_400,
    banked_ttl_seconds: 604_800,
    wake_timeout_seconds: 120
  }
  # The minimum idleBankSeconds (a too-eager bank thrashes wake/bank); the CRD
  # schema also enforces this, re-checked here for the LIST/WATCH path.
  @group_min_idle_bank_seconds 30

  defp validate_group(_state, _name, spec, class)
       when class in ["task", "session", "serving", "stateful"] do
    case Map.get(spec, "group") do
      nil -> {:ok, nil}
      _ -> {:error, "GroupSpecUnexpected", "spec.group is only valid for class composite, not #{class}"}
    end
  end

  defp validate_group(state, name, spec, "composite") do
    case Map.get(spec, "group") do
      g when is_map(g) ->
        with {:ok, member_names} <- validate_group_members(g, state.max_group_size),
             :ok <- validate_group_entry(g, member_names),
             :ok <- validate_group_listen_port(state, name, g),
             :ok <- validate_group_idle_bank(g) do
          {:ok, parse_group(g)}
        end

      _ ->
        {:error, "GroupSpecMissing", "class composite requires a spec.group block"}
    end
  end

  # Member names must be present, unique DNS labels; the expanded member count (sum
  # of replicas) must not exceed maxGroupSize. Returns the EXPANDED name set (every
  # `<name>` plus, when replicas > 1, each `<name>-<index>`) so entry validation can
  # accept an expanded replica name as the entry target.
  defp validate_group_members(g, max_group_size) do
    members = Map.get(g, "members")

    cond do
      not (is_list(members) and members != []) ->
        {:error, "InvalidGroupSpec", "spec.group.members must be a non-empty list"}

      not Enum.all?(members, &is_map/1) ->
        {:error, "InvalidGroupSpec", "each spec.group.members entry must be an object"}

      true ->
        names = Enum.map(members, &Map.get(&1, "name"))

        cond do
          Enum.any?(names, &(not (is_binary(&1) and &1 != ""))) ->
            {:error, "InvalidGroupSpec", "each spec.group.members entry requires a name"}

          length(Enum.uniq(names)) != length(names) ->
            {:error, "GroupMemberNameConflict", "spec.group.members names must be unique"}

          true ->
            expanded_count = Enum.reduce(members, 0, fn m, acc -> acc + member_replicas(m) end)

            if expanded_count > max_group_size do
              {:error, "GroupSizeExceeded",
               "spec.group expanded member count #{expanded_count} exceeds the configured maxGroupSize #{max_group_size}"}
            else
              {:ok, expanded_member_names(members)}
            end
        end
    end
  end

  # A member's replica count, clamped to at least 1. Guards the Elixir truthiness
  # trap: `0 || 1` is `0` (0 is truthy), so a `replicas: 0` that slipped past CRD
  # admission would under-count the expanded size here; an absent or non-positive
  # value bills as one replica. Used by BOTH the size-cap sum and the expanded-name
  # set so the two never disagree.
  defp member_replicas(m) do
    case Map.get(m, "replicas") do
      n when is_integer(n) and n > 0 -> n
      _ -> 1
    end
  end

  # The valid entry targets: each declared member name, plus (for a member with
  # replicas > 1) each expanded `<name>-<index>` (0-based). A replicas-1 member is
  # addressable by its bare name only (there is no `-0` form), matching the CRD doc.
  defp expanded_member_names(members) do
    Enum.flat_map(members, fn m ->
      name = Map.get(m, "name")
      replicas = member_replicas(m)

      if replicas > 1 do
        [name | Enum.map(0..(replicas - 1), fn i -> "#{name}-#{i}" end)]
      else
        [name]
      end
    end)
  end

  # entry.member must name a declared member or an expanded replica name; entry.port
  # is required (the CRD enforces its range, re-checked as an integer here).
  defp validate_group_entry(g, member_names) do
    entry = Map.get(g, "entry") || %{}
    member = Map.get(entry, "member")
    port = Map.get(entry, "port")

    cond do
      not (is_binary(member) and member != "") ->
        {:error, "InvalidGroupSpec", "spec.group.entry.member is required"}

      member not in member_names ->
        {:error, "GroupEntryMemberUnknown",
         "spec.group.entry.member #{inspect(member)} names no declared member or expanded replica"}

      not is_integer(port) ->
        {:error, "InvalidGroupSpec", "spec.group.entry.port is required and must be an integer"}

      true ->
        :ok
    end
  end

  # entry.listenPort must be inside the values-declared composite range AND unique
  # across every other live composite workload, mirroring the stateful listenPort
  # rule. Out-of-range names the configured range; a duplicate names the conflict.
  defp validate_group_listen_port(state, name, g) do
    listen_port = get_in(g, ["entry", "listenPort"])
    range = state.composite_listen_range

    cond do
      not is_integer(listen_port) ->
        {:error, "InvalidGroupSpec", "spec.group.entry.listenPort is required and must be an integer"}

      listen_port not in range ->
        {:error, "GroupListenPortOutOfRange",
         "spec.group.entry.listenPort #{listen_port} is outside the configured composite range #{Enum.min(range)}-#{Enum.max(range)}"}

      true ->
        case group_listen_port_conflict(state, name, listen_port) do
          nil -> :ok
          other ->
            {:error, "GroupListenPortConflict",
             "spec.group.entry.listenPort #{listen_port} is already owned by workload #{inspect(other)}"}
        end
    end
  end

  defp group_listen_port_conflict(state, name, listen_port) do
    state.table
    |> WorkloadCatalog.all_names()
    |> Enum.reject(&(&1 == name))
    |> Enum.find(fn other_name ->
      case WorkloadCatalog.fetch(state.table, other_name) do
        {:ok, %{class: "composite", group: %{entry: %{listen_port: ^listen_port}}}} -> true
        _ -> false
      end
    end)
  end

  defp validate_group_idle_bank(g) do
    case Map.get(g, "idleBankSeconds") do
      nil ->
        :ok

      idle when is_integer(idle) and idle >= @group_min_idle_bank_seconds ->
        :ok

      idle ->
        {:error, "InvalidGroupSpec",
         "spec.group.idleBankSeconds #{inspect(idle)} must be >= #{@group_min_idle_bank_seconds}"}
    end
  end

  # Parse the validated group block into the catalog shape. Members carry their
  # per-member source/resources/health/env; entry carries the ingress; the timers
  # apply their CRD-documented defaults here (no silent dependency on apiserver
  # defaulting). secretRef is nil when absent (EMBER_GROUP_SECRET is minted per
  # instance then).
  defp parse_group(g) do
    entry = Map.get(g, "entry") || %{}

    %{
      members: Enum.map(Map.get(g, "members") || [], &parse_group_member/1),
      entry: %{
        member: Map.get(entry, "member"),
        port: Map.get(entry, "port"),
        listen_port: Map.get(entry, "listenPort")
      },
      secret_ref: parse_group_secret_ref(Map.get(g, "secretRef")),
      idle_bank_seconds: Map.get(g, "idleBankSeconds") || @group_defaults.idle_bank_seconds,
      max_lifetime_seconds: Map.get(g, "maxLifetimeSeconds") || @group_defaults.max_lifetime_seconds,
      banked_ttl_seconds: Map.get(g, "bankedTtlSeconds") || @group_defaults.banked_ttl_seconds,
      wake_timeout_seconds: Map.get(g, "wakeTimeoutSeconds") || @group_defaults.wake_timeout_seconds
    }
  end

  defp parse_group_member(m) do
    %{
      name: Map.get(m, "name"),
      role: Map.get(m, "role"),
      start_order: Map.get(m, "startOrder") || 0,
      replicas: member_replicas(m),
      image_ref: get_in(m, ["source", "image", "ref"]),
      vcpus: get_in(m, ["resources", "vcpus"]),
      mem_mib: get_in(m, ["resources", "memMib"]),
      health_port: Map.get(m, "healthPort"),
      env: Map.get(m, "env") || %{}
    }
  end

  defp parse_group_secret_ref(nil), do: nil

  defp parse_group_secret_ref(ref) when is_map(ref) do
    %{name: Map.get(ref, "name"), key: Map.get(ref, "key")}
  end

  # The CRD schema enforces the structural oneOf (exactly one of image|zip) at
  # admission, but a CR observed via LIST/WATCH is trusted only as far as this
  # code re-checks it: enforce the same mutual exclusion here (both set, or
  # neither) plus the zip lane's required sha256 and known runtime, so a
  # malformed CR gets a precise Ready=False rather than a crash downstream.
  @known_runtimes ["python312"]

  defp validate_source(spec) do
    image = get_in(spec, ["source", "image"])
    zip = get_in(spec, ["source", "zip"])

    cond do
      is_map(image) and is_map(zip) ->
        {:error, "SourceAmbiguous", "exactly one of source.image or source.zip may be set, not both"}

      is_map(image) ->
        :ok

      is_map(zip) ->
        validate_zip(zip)

      true ->
        {:error, "SourceUnsupported", "exactly one of source.image or source.zip must be set"}
    end
  end

  defp validate_zip(zip) do
    runtime = Map.get(zip, "runtime")
    sha256 = Map.get(zip, "sha256")

    cond do
      not (is_binary(sha256) and sha256 != "") ->
        {:error, "InvalidZipSource", "source.zip.sha256 is required"}

      runtime not in @known_runtimes ->
        {:error, "InvalidZipSource",
         "source.zip.runtime #{inspect(runtime)} is unknown; only python312 is valid in v1alpha1"}

      not is_binary(Map.get(zip, "codeUri")) or Map.get(zip, "codeUri") == "" ->
        {:error, "InvalidZipSource", "source.zip.codeUri is required"}

      true ->
        :ok
    end
  end

  # The stateful class is a SINGLETON by construction (decision 3): exactly one
  # live VM, no maxInstances knob. spec.concurrency is condition-rejected, and the
  # class's internal floor/cap are fixed at scale-to-zero singleton (0, 1) so the
  # catalog entry keeps a uniform shape without a concurrency block.
  defp validate_concurrency(spec, "stateful") do
    case Map.get(spec, "concurrency") do
      nil -> {:ok, 0, 1}
      _ -> {:error, "ConcurrencyUnexpected", "spec.concurrency is not valid for class stateful (singleton by construction)"}
    end
  end

  # The composite class's size is fixed by its member set (sum of replicas), not a
  # concurrency knob, so spec.concurrency is condition-rejected exactly like the
  # stateful class. The internal floor/cap are fixed at scale-to-zero group (0, 1
  # group instance) so the catalog entry keeps a uniform shape without a concurrency
  # block; the expanded member count is bounded separately by maxGroupSize.
  defp validate_concurrency(spec, "composite") do
    case Map.get(spec, "concurrency") do
      nil -> {:ok, 0, 1}
      _ -> {:error, "ConcurrencyUnexpected", "spec.concurrency is not valid for class composite (group size is fixed by the member set)"}
    end
  end

  defp validate_concurrency(spec, _class) do
    cap = get_in(spec, ["concurrency", "cap"])
    floor = get_in(spec, ["concurrency", "floor"]) || 0

    if is_integer(cap) and cap >= floor do
      {:ok, floor, cap}
    else
      {:error, "InvalidConcurrency", "concurrency.cap (#{inspect(cap)}) must be >= floor (#{inspect(floor)})"}
    end
  end

  # -- catalog entry ---------------------------------------------------------

  # Builds the catalog entry from a spec already known-valid by validate/1
  # (image is a map, cap/floor already checked). Every optional field applies
  # its CRD-documented default here too, mirroring (not depending on) the
  # apiserver's own OpenAPI defaulting, since a CR observed via LIST/WATCH
  # reflects whatever the apiserver already defaulted at admission;
  # re-defaulting here just means this code has no silent dependency on that
  # having happened.
  defp catalog_entry(
         name,
         namespace,
         generation,
         spec,
         class,
         floor,
         cap,
         session_cfg,
         serving_cfg,
         stateful_cfg,
         group_cfg
       ) do
    resources = Map.get(spec, "resources") || %{}
    invocation = Map.get(spec, "invocation") || %{}
    source = parse_source(spec)

    # The guest's baked env. The image lane reads source.image.initEnv; the zip
    # lane passes the handler symbol as EMBER_HANDLER (the runtime shim reads it
    # to import the adopter's handler). A zip CR has no initEnv block. Stored on
    # the entry (not recomputed at build_desc time) so the periodic BaseBuilder
    # resync can rebuild the identical build descriptor from the catalog alone.
    init_env =
      case source.zip do
        nil -> get_in(spec, ["source", "image", "initEnv"]) || %{}
        %{handler: handler} -> %{"EMBER_HANDLER" => handler}
      end

    %{
      name: name,
      namespace: namespace,
      # Carried so the periodic BaseBuilder resync (see this module's
      # @moduledoc, "periodic catalog resync") can rebuild the exact same
      # build descriptor build_desc/1 sends on a fresh LIST/WATCH event,
      # purely from the catalog, without re-fetching the CR.
      generation: generation,
      init_env: init_env,
      class: class,
      # Stateful-class volume + L4 config (nil except for the stateful class),
      # carried so the StatefulStore/TcpActivator (Tasks 7/8) read listenPort,
      # port, volume sizing, and the idle/lifetime/ttl knobs from the catalog
      # exactly as SessionStore/EndpointPublisher read their class config.
      stateful: stateful_cfg,
      # Composite-class group config (nil except for the composite class), carried
      # so the future GroupStore/GroupManager read the member set, entry, secretRef,
      # and the idle/lifetime/ttl knobs from the catalog exactly as StatefulStore
      # reads its class config.
      group: group_cfg,
      # Session-class lifecycle config (nil for the task class), carried so the
      # SessionManager/SessionStore (later PRs) read idle-bank/lifetime/caps from
      # the catalog exactly as the dispatcher reads task caps.
      session: session_cfg,
      # Serving-class endpoint + elasticity config (nil except for the serving
      # class), carried so the future EndpointPublisher/Activator (Tasks 7/8)
      # read host/port/healthPath and the instance-count knobs from the
      # catalog exactly as SessionStore reads session config. Distinct from
      # `port`/`ready_path` below, which are the base-build guest contract
      # (vsock, used at BuildBase/Prime time), not the serving tap endpoint.
      serving: serving_cfg,
      # Zip-lane source (nil for the image lane), carried so the BaseBuilder can
      # map it to a proto ZipSource. The image lane leaves it nil.
      zip: source.zip,
      image_ref: source.image_ref,
      port: source.port,
      ready_path: source.ready_path,
      invoke_path: source.invoke_path,
      vcpus: Map.get(resources, "vcpus"),
      mem_mib: Map.get(resources, "memMib"),
      floor: floor,
      cap: cap,
      timeout_ms: (Map.get(invocation, "timeoutSeconds") || 90) * 1000,
      result_ttl_ms: (Map.get(invocation, "resultTtlSeconds") || 86_400) * 1000,
      result_max_bytes: Map.get(invocation, "resultMaxBytes") || 1_048_576,
      retry: parse_retry(Map.get(invocation, "retry") || %{}),
      # spec.triggers[] parsed for the cron TriggerAdapter (Task 11). Each entry is
      # %{cron, payload}; the daemon-agnostic control plane fires each as an
      # ordinary submit. An absent/empty list means no triggers.
      triggers: parse_triggers(Map.get(spec, "triggers"))
    }
  end

  # The frozen zip-lane guest contract port (the runtime shim's vsock HTTP
  # listener, node.proto GuestHTTPPort). The zip CR does not carry a port (it is
  # baked into the runtime image), so the catalog uses this constant.
  @zip_guest_port 1027

  # Normalize either source lane into a common shape the catalog and build
  # descriptor consume. The image lane fills image_ref (+ its port/paths); the
  # zip lane leaves image_ref nil, carries a :zip map, and uses the frozen
  # contract port plus the zip block's ready/invoke paths. validate_source/1 has
  # already guaranteed exactly one lane is present and, for zip, that
  # runtime/sha256/codeUri are valid.
  defp parse_source(spec) do
    image = get_in(spec, ["source", "image"])
    zip = get_in(spec, ["source", "zip"])

    cond do
      is_map(zip) ->
        %{
          zip: %{
            runtime: Map.get(zip, "runtime"),
            code_uri: Map.get(zip, "codeUri"),
            sha256: Map.get(zip, "sha256"),
            handler: Map.get(zip, "handler") || "app.handle"
          },
          image_ref: nil,
          port: @zip_guest_port,
          ready_path: Map.get(zip, "readyPath") || "/shim/ready",
          invoke_path: Map.get(zip, "invokePath") || "/invoke"
        }

      true ->
        %{
          zip: nil,
          image_ref: Map.get(image, "ref"),
          port: Map.get(image, "port"),
          ready_path: Map.get(image, "readyPath") || "/shim/ready",
          invoke_path: Map.get(image, "invokePath") || "/"
        }
    end
  end

  # spec.triggers[] -> [%{cron, payload}]. The CRD requires `cron` per item and
  # `payload` is arbitrary JSON (x-kubernetes-preserve-unknown-fields); a
  # non-list or missing field yields an empty list, so a malformed triggers block
  # simply schedules nothing rather than crashing the reconcile.
  defp parse_triggers(list) when is_list(list) do
    for t <- list, is_map(t), is_binary(Map.get(t, "cron")) do
      %{cron: Map.get(t, "cron"), payload: Map.get(t, "payload")}
    end
  end

  defp parse_triggers(_), do: []

  # Mirrors Embervm.Retry.retry_config()'s shape exactly (max_attempts,
  # backoff_ms, backoff_cap_ms, retry_on) so classify/2 and backoff_ms/2,3
  # accept a catalog-sourced config unchanged, with no adapter needed at the
  # TaskStore call site.
  defp parse_retry(r) do
    %{
      max_attempts: Map.get(r, "maxAttempts") || 3,
      backoff_ms: (Map.get(r, "backoffSeconds") || 1) * 1000,
      backoff_cap_ms: (Map.get(r, "backoffCapSeconds") || 60) * 1000,
      retry_on: parse_retry_on(Map.get(r, "retryOn"))
    }
  end

  # Unknown strings are dropped rather than raising: the CRD schema already
  # constrains retryOn to a fixed enum at admission, so an unrecognized
  # string here would only arise from a schema/watcher version skew, and
  # silently ignoring it (rather than crashing reconciliation) is the safer
  # failure mode for a config-reading loop.
  defp parse_retry_on(nil), do: [:transport, :timeout, :guest5xx]

  defp parse_retry_on(list) when is_list(list) do
    Enum.flat_map(list, fn s ->
      case Map.fetch(@retry_on_map, s) do
        {:ok, atom} -> [atom]
        :error -> []
      end
    end)
  end

  defp parse_retry_on(_), do: [:transport, :timeout, :guest5xx]
end
