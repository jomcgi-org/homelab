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
  """

  use GenServer
  require Logger

  alias Embervm.WorkloadCatalog

  @default_table :embervm_workloads
  @base_backoff_ms 1_000
  @max_backoff_ms 30_000
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

  # -- GenServer callbacks -----------------------------------------------------

  @impl true
  def init(opts) do
    table = Keyword.get(opts, :table, @default_table)
    lister = Keyword.get(opts, :lister, &Embervm.K8s.list_workloads/0)
    watcher_fun = Keyword.get(opts, :watcher_fun, &Embervm.K8s.watch_workloads/2)
    status_writer = Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3)
    clock = Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end)
    base_backoff = Keyword.get(opts, :base_backoff_ms, @base_backoff_ms)
    max_backoff = Keyword.get(opts, :max_backoff_ms, @max_backoff_ms)
    min_watch = Keyword.get(opts, :min_watch_ms, @min_watch_ms)
    watch_startup = Keyword.get(opts, :watch_startup, true)

    WorkloadCatalog.create(table)

    state = %{
      table: table,
      lister: lister,
      watcher_fun: watcher_fun,
      status_writer: status_writer,
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
      needs_relist: false
    }

    # watch_startup drives the informer from init; tests set it false and drive
    # reconcile_now/1 explicitly so no background watch or timer ever fires.
    if watch_startup, do: send(self(), :start)

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
    if name, do: WorkloadCatalog.drop(state.table, name)
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
    |> Enum.each(&WorkloadCatalog.drop(state.table, &1))

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

      case validate(spec) do
        {:ok, floor, cap} ->
          entry = catalog_entry(name, namespace, spec, floor, cap)
          WorkloadCatalog.upsert(state.table, name, entry)

          # Valid in R0 is never Ready=True: the base builder that would make
          # a snapshot ready is Task 10, not this watcher. Reporting False
          # here is the honest status, not a placeholder bug.
          write_status(
            state,
            namespace,
            name,
            generation,
            ready_condition(state, "False", "BaseNotBuilt", "base snapshot not built yet (Task 10)")
          )

        {:error, reason_code, message} ->
          # Never serve an invalid CR: drop it from the catalog (it may have
          # been valid before an edit made it invalid) rather than leaving a
          # stale entry that no longer matches spec.
          WorkloadCatalog.drop(state.table, name)
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

  # Returns {:ok, fields} with just the pieces catalog_entry/3 needs, or
  # {:error, reason_code, message} for a status condition. The CRD schema
  # (workload-crd.yaml) enforces shape (required fields, enums, min/max) at
  # admission; what's left for the watcher is the cross-field/semantic rules
  # the OpenAPI schema cannot express (class allow-list beyond the enum,
  # oneOf source lane, cap >= floor).
  defp validate(spec) do
    with :ok <- validate_class(spec),
         :ok <- validate_source(spec),
         {:ok, floor, cap} <- validate_concurrency(spec) do
      {:ok, floor, cap}
    end
  end

  defp validate_class(%{"class" => "task"}), do: :ok

  defp validate_class(spec) do
    {:error, "ClassUnsupported",
     "class #{inspect(Map.get(spec, "class"))} is reserved for a later rung; only task is valid in v1alpha1"}
  end

  defp validate_source(spec) do
    case get_in(spec, ["source", "image"]) do
      image when is_map(image) -> :ok
      _ -> {:error, "SourceUnsupported", "only source.image is implemented in v1alpha1"}
    end
  end

  defp validate_concurrency(spec) do
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
  defp catalog_entry(name, namespace, spec, floor, cap) do
    image = get_in(spec, ["source", "image"])
    resources = Map.get(spec, "resources") || %{}
    invocation = Map.get(spec, "invocation") || %{}

    %{
      name: name,
      namespace: namespace,
      class: "task",
      image_ref: Map.get(image, "ref"),
      port: Map.get(image, "port"),
      ready_path: Map.get(image, "readyPath") || "/shim/ready",
      invoke_path: Map.get(image, "invokePath") || "/",
      vcpus: Map.get(resources, "vcpus"),
      mem_mib: Map.get(resources, "memMib"),
      floor: floor,
      cap: cap,
      timeout_ms: (Map.get(invocation, "timeoutSeconds") || 90) * 1000,
      result_ttl_ms: (Map.get(invocation, "resultTtlSeconds") || 86_400) * 1000,
      result_max_bytes: Map.get(invocation, "resultMaxBytes") || 1_048_576,
      retry: parse_retry(Map.get(invocation, "retry") || %{})
    }
  end

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
