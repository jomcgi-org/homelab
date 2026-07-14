defmodule Embervm.Trigger.Cron do
  @moduledoc """
  The cron `Embervm.TriggerAdapter`: fires each Workload's `spec.triggers[].cron`
  as an ordinary submit with principal `system:cron:<workload>` and the trigger's
  inline `payload` as the (JSON) task body.

  ## how it schedules

  It reads the trigger list from `Embervm.WorkloadCatalog` (the watcher parses
  `spec.triggers` into each entry) and, per trigger, holds the NEXT fire time
  computed by `Embervm.Cron.next/2` from the current clock. A periodic tick
  reconciles the catalog (adds new triggers, drops removed ones) and fires every
  trigger whose next-fire time has passed, then recomputes each fired trigger's
  next time from now.

  ## misfires are skipped, not replayed (documented semantic)

  Next-fire is always computed FORWARD from the current time, and nothing is
  persisted, so a fire whose minute elapsed while the control plane was down is
  simply never scheduled: it is skipped, never backfilled. This is deliberate and
  safe for R0's cron workloads (the daily full-scan consumer tolerates a skipped
  tick; it re-scans the whole surface next run), and it keeps the adapter
  stateless across restarts. A trigger source that cannot tolerate a skip must
  bring its own delivery guarantee behind the same `Embervm.TriggerAdapter` seam
  (e.g. NATS acks); cron does not.

  The clock and the submit sink are injected, so a test drives the whole fire
  path deterministically: advance the clock, tick, assert the recorded submit.
  """

  @behaviour Embervm.TriggerAdapter

  use GenServer
  require Logger

  alias Embervm.{Cron, WorkloadCatalog}

  @tick_interval_ms 15_000
  @tenant "homelab"
  @default_ttl_ms 86_400_000

  # -- Client API ------------------------------------------------------------

  @impl Embervm.TriggerAdapter
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Reconcile the schedule against the current catalog: add a next-fire for every
  trigger not yet tracked, drop triggers whose Workload or trigger entry is gone.
  A no-op for unchanged triggers (their next-fire is preserved). Runs on the
  production tick; tests call it explicitly.
  """
  @spec reconcile(GenServer.server()) :: :ok
  def reconcile(server \\ __MODULE__), do: GenServer.call(server, :reconcile)

  @doc """
  Fire every tracked trigger whose next-fire time has passed (per the injected
  clock), submitting each and recomputing its next-fire. Returns the number
  fired. Runs on the production tick; tests drive it with an advanced clock.
  """
  @spec tick(GenServer.server()) :: {:ok, non_neg_integer()}
  def tick(server \\ __MODULE__), do: GenServer.call(server, :tick)

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      submit_fun: Keyword.get(opts, :submit_fun, &default_submit/1),
      tick_interval_ms: Keyword.get(opts, :tick_interval_ms, @tick_interval_ms),
      ttl_ms: Keyword.get(opts, :ttl_ms, @default_ttl_ms),
      # {workload, index} -> %{cron, payload, invoke_path, workload, next}
      schedules: %{}
    }

    state = reconcile_schedules(state)

    if Keyword.get(opts, :start_ticking, true) do
      schedule_tick(state)
    end

    {:ok, state}
  end

  @impl true
  def handle_call(:reconcile, _from, state) do
    {:reply, :ok, reconcile_schedules(state)}
  end

  def handle_call(:tick, _from, state) do
    {state, fired} = fire_due(state)
    {:reply, {:ok, fired}, state}
  end

  @impl true
  def handle_info(:tick, state) do
    state = reconcile_schedules(state)
    {state, _fired} = fire_due(state)
    schedule_tick(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- reconcile -------------------------------------------------------------

  # Rebuild the schedule map from the catalog: keep an existing trigger's
  # next-fire, compute one for a newly-seen trigger, drop any whose Workload or
  # trigger is gone. Triggers are keyed {workload, index} so editing one
  # workload's trigger list never disturbs another's schedule.
  defp reconcile_schedules(state) do
    now = state.clock.()
    desired = catalog_triggers(state)

    schedules =
      for {key, spec} <- desired, into: %{} do
        case Map.get(state.schedules, key) do
          %{cron: existing_cron} = kept when spec.cron_expr == existing_cron.expr ->
            # Unchanged trigger: keep its pending next-fire.
            {key, %{kept | payload: spec.payload, invoke_path: spec.invoke_path, workload: spec.workload}}

          _ ->
            # New or changed cron expression: compute a fresh next-fire from now.
            case Cron.parse(spec.cron_expr) do
              {:ok, cron} ->
                {key, build_schedule(spec, cron, now)}

              {:error, reason} ->
                Logger.warning(
                  "embervm cron: skipping invalid cron #{inspect(spec.cron_expr)} for #{spec.workload}: #{inspect(reason)}"
                )

                {key, :invalid}
            end
        end
      end
      |> Enum.reject(fn {_k, v} -> v == :invalid end)
      |> Map.new()

    %{state | schedules: schedules}
  end

  defp build_schedule(spec, cron, now) do
    next =
      case Cron.next(cron, now) do
        {:ok, dt} -> dt
        {:error, _} -> nil
      end

    %{
      cron: Map.put(cron, :expr, spec.cron_expr),
      payload: spec.payload,
      invoke_path: spec.invoke_path,
      workload: spec.workload,
      next: next
    }
  end

  # Every (workload, trigger-index) currently cataloged, flattened with the
  # invoke_path the trigger submits to.
  defp catalog_triggers(state) do
    # Uses Map.get, not entry.triggers/entry.invoke_path: the catalog is shared,
    # and a partial entry (a test fixture, or a not-yet-fully-reconciled row)
    # without those keys must yield "no triggers", never a KeyError that would
    # crash this supervised adapter.
    for name <- WorkloadCatalog.all_names(state.catalog_table),
        {:ok, entry} <- [WorkloadCatalog.fetch(state.catalog_table, name)],
        {trigger, index} <- Enum.with_index(Map.get(entry, :triggers) || []),
        is_map(trigger),
        is_binary(Map.get(trigger, :cron)) do
      {{name, index},
       %{
         workload: name,
         cron_expr: Map.get(trigger, :cron),
         payload: Map.get(trigger, :payload),
         invoke_path: Map.get(entry, :invoke_path) || "/"
       }}
    end
  end

  # -- fire ------------------------------------------------------------------

  defp fire_due(state) do
    now = state.clock.()

    {schedules, fired} =
      Enum.reduce(state.schedules, {%{}, 0}, fn {key, sched}, {acc, count} ->
        cond do
          is_nil(sched.next) ->
            {Map.put(acc, key, sched), count}

          not future?(sched.next, now) ->
            submit_trigger(state, sched)
            {Map.put(acc, key, %{sched | next: recompute_next(sched, now)}), count + 1}

          true ->
            {Map.put(acc, key, sched), count}
        end
      end)

    {%{state | schedules: schedules}, fired}
  end

  defp recompute_next(sched, now) do
    case Cron.next(sched.cron, now) do
      {:ok, dt} -> dt
      {:error, _} -> nil
    end
  end

  # future?(t, now) is true when t is strictly after now (not yet due).
  defp future?(t, now), do: DateTime.compare(t, now) == :gt

  defp submit_trigger(state, sched) do
    now_ms = DateTime.to_unix(state.clock.(), :millisecond)

    attrs = %{
      tenant: @tenant,
      principal: "system:cron:" <> sched.workload,
      workload: sched.workload,
      idempotency_key: nil,
      expires_at: now_ms + state.ttl_ms,
      request: %{
        path: sched.invoke_path,
        headers: %{},
        body_b64: Base.encode64(encode_payload(sched.payload)),
        content_type: "application/json"
      }
    }

    case safe(fn -> state.submit_fun.(attrs) end) do
      {:ok, _} ->
        :ok

      other ->
        Logger.warning("embervm cron: submit for #{sched.workload} failed: #{inspect(other)}")
    end
  end

  # The inline payload is arbitrary JSON in the CR; encode it back to a JSON body.
  # nil payload -> empty body (a bodyless POST).
  defp encode_payload(nil), do: ""
  defp encode_payload(payload), do: payload |> :json.encode() |> :erlang.iolist_to_binary()

  # -- helpers ---------------------------------------------------------------

  defp schedule_tick(state) do
    Process.send_after(self(), :tick, state.tick_interval_ms)
    state
  end

  defp safe(fun) do
    try do
      {:ok, fun.()}
    rescue
      e -> {:error, e}
    catch
      kind, reason -> {:error, {kind, reason}}
    end
  end

  defp default_clock, do: DateTime.utc_now()

  defp default_submit(attrs), do: Embervm.TaskStore.submit(attrs)
end
