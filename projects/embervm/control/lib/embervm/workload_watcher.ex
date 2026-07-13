defmodule Embervm.WorkloadWatcher do
  @moduledoc """
  Reconciles the `Workload` custom resources in the cluster into
  `Embervm.WorkloadCatalog`'s ETS table, and writes each CR's `status`
  subresource back to reflect what the control plane concluded.

  This is a periodic LIST + reconcile loop, not a streaming K8s watch. That
  is a deliberate choice, not a placeholder for a future upgrade: Workload
  CRs are low-churn declarative config (an operator edits one occasionally,
  nothing programmatic mutates spec), the only in-process reader of the
  catalog is `Embervm.TaskStore.cfg_for/1` at failure-classification time (not
  a hot dispatch path with sub-second staleness requirements), and a poll-
  based reconciler is trivially testable (inject a canned `lister`, call
  `reconcile_now/1`, assert the catalog) where a long-lived watch stream with
  resourceVersion bookkeeping and reconnect/resync logic is not. If churn or
  staleness requirements change later, swapping the `:lister` for a genuine
  watch is an internal implementation change behind the same public API.

  Invariant: a reconcile pass NEVER crashes the process, no matter how
  malformed one CR is, and NEVER wipes the catalog on a transient list
  failure (fail-open: the last-known-good catalog is served until the next
  successful list). The watcher writes status ONLY, never spec.
  """

  use GenServer
  require Logger

  alias Embervm.WorkloadCatalog

  @default_table :embervm_workloads
  @default_interval_ms 15_000

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
  Forces one reconcile pass synchronously and waits for it to finish. Tests
  drive the watcher entirely through this (with `reconcile_interval_ms: nil`
  so no timer fires on its own); in production it is also a safe operational
  nudge (e.g. after a CR edit, to reconcile without waiting up to the full
  interval).
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
    status_writer = Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3)
    interval_ms = Keyword.get(opts, :reconcile_interval_ms, @default_interval_ms)
    clock = Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end)

    WorkloadCatalog.create(table)

    state = %{
      table: table,
      lister: lister,
      status_writer: status_writer,
      interval_ms: interval_ms,
      clock: clock
    }

    # An immediate first reconcile is the relist-on-boot path: whatever the
    # cluster's Workload state is at startup, the catalog reflects it before
    # any task can fail against a stale (empty) config. Skipped entirely when
    # interval_ms is nil (tests drive reconciles explicitly instead).
    if interval_ms, do: send(self(), :reconcile)

    {:ok, state}
  end

  @impl true
  def handle_info(:reconcile, state) do
    do_reconcile(state)
    if state.interval_ms, do: Process.send_after(self(), :reconcile, state.interval_ms)
    {:noreply, state}
  end

  @impl true
  def handle_call(:reconcile_now, _from, state) do
    do_reconcile(state)
    {:reply, :ok, state}
  end

  # -- reconcile ---------------------------------------------------------------

  defp do_reconcile(state) do
    case state.lister.() do
      {:error, reason} ->
        # Fail-open: a transient list error (apiserver hiccup, network blip)
        # must never wipe the last-known-good catalog. TaskStore keeps
        # classifying failures against whatever config it already has.
        Logger.warning("embervm workload watcher: list failed, keeping prior catalog: #{inspect(reason)}")
        :ok

      {:ok, crs} ->
        seen = Enum.map(crs, fn cr -> reconcile_one(state, cr) end)

        (WorkloadCatalog.all_names(state.table) -- seen)
        |> Enum.each(&WorkloadCatalog.drop(state.table, &1))

        :ok
    end
  end

  # Reconciles exactly one CR and returns its name (so the caller can build
  # the "seen" set for deletion sweeping) whether or not the CR was valid.
  # Wrapped in try/catch: ONE malformed CR (missing metadata, a spec shaped
  # nothing like the CRD schema) must never crash this loop or the GenServer
  # itself, since a bad CR sitting in the cluster would otherwise permanently
  # wedge reconciliation for every OTHER, valid Workload too.
  defp reconcile_one(state, cr) do
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
          "embervm workload watcher: reconcile_one crashed on a malformed CR, skipping: " <>
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
  # apiserver's own OpenAPI defaulting, since a CR observed via LIST reflects
  # whatever the apiserver already defaulted at admission; re-defaulting here
  # just means this code has no silent dependency on that having happened.
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
