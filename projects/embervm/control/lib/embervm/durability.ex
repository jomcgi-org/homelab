defmodule Embervm.Durability do
  @moduledoc """
  EmberVM durability health, classified by time-to-impact into the two ADR
  embervm/031 tiers (#4338). Both tiers end in the health surface, not
  alert-only: #4317 proved an alert-only signal goes unread (the S3 warmth GC
  aborted silently for 11 days) and that create/send synthetic probes stay
  green straight through a fleet-wide artifact-export outage.

  ## Tier 1: sustained artifact export failures (unhealthy NOW)

  While SeaweedFS was 500ing every PUT in #4317, noded kept reporting banked
  session/serving/stateful bundles and group sets whose store copy never became
  current (`exported == true` only on a confirmed off-node copy), and bases
  stayed present-but-unexported while BaseBuilder re-issued ExportArtifact.
  This module aggregates those per-kind facts across every dispatchable node's
  NodeCapacity projection: a kind is PENDING in a round when any reported
  artifact of that kind has its local copy present but no confirmed store copy.
  Pending rounds accumulate per kind; crossing `:streak_threshold` consecutive
  rounds (~minutes, not hours) marks ember unhealthy immediately, because a
  session parking inside such a window rehydrates blank: user-visible data loss
  happening now, even while create/send keeps serving.

  Known gap, documented deliberately: SessionVolume carries no exported flag on
  the wire yet, so a WORKSPACE-only export failure (the #4317 victim named in
  the issue) is not directly visible to the CP; the incident itself would have
  fired this detector within minutes because BASE/VOLUME/STATEFUL/session
  exports were failing fleet-wide at the same time.

  ## Tier 2: GC sweep stall older than 24h + sweep interval

  An aborted warmth-GC sweep produces no `gc-manifests/<ts>.json` audit object
  for that cycle, whatever the abort reason (fleet-freshness gate, empty-store
  guard, list failure), so the age of the NEWEST gc-manifests object is a
  complete stall signal that survives CP restarts by construction (it reads a
  durable S3 object, not an in-process counter). Age beyond 24h PLUS the sweep
  interval marks ember unhealthy: days of runway, so it must not page like a
  live outage, but it can never again be alert-only.

  ## Vacuous-green guards (the failure class this repo keeps hitting)

  Missing data NEVER reads healthy:

    * expectedNodes unset or no expected node reporting fresh -> tier 1 reads
      `unknown` (not ok), because absence of node reports is not evidence of
      exports succeeding.
    * no gc-manifests objects, a failed listing, or no store endpoint -> tier 2
      reads `unknown` (not ok).

  ## Dark landing (#4338 standing rule)

  The detector ships suspend:true/dark: `enabled` defaults false (chart value
  `durabilityHealth.enabled`, env EMBERVM_DURABILITY_HEALTH). While suspended
  the supervised process holds no timer and evaluates nothing, and
  GET /v1/health/durability answers 404. It flips on only after live
  verification. Streak counters are in-memory and rebuild from live reports
  within one streak window after a CP restart; tier 2 needs no rebuild (S3).
  """

  use GenServer
  require Logger

  alias Embervm.NodeCapacity

  # -- Tier 1 knobs ------------------------------------------------------------
  # One round ~= one noded status-report interval (the GC's freshness contract
  # assumes ~30s between projections). The threshold is deliberately "a handful
  # of consecutive failures, minutes not hours" (issue #4338): 10 rounds x 30s
  # = ~5 minutes of sustained failure before latching, which absorbs a fresh
  # bank's normal seconds-to-minutes export latency and the queue-full drop +
  # reconcile retry cycle, but cannot absorb an outage like #4317's hours.
  @round_interval_ms 30_000
  @streak_threshold 10
  # Same freshness window the S3 GC uses for its fleet-freshness precondition:
  # ~2 status-report intervals.
  @freshness_window_ms 120_000

  # The artifact kinds whose store-copy confirmation rides NodeStatus today.
  # VOLUMES are excluded on purpose: exported_generation routinely LAGS the
  # live generation between banks (banks outnumber wakes), so a lagging volume
  # is normal operation, not a failing export.
  @kinds [:base, :session, :serving, :stateful, :group_set]

  # -- Tier 2 knobs ------------------------------------------------------------
  @manifest_prefix "gc-manifests/"
  # The ADR floor: unhealthy only after >24h sustained. Deliberately generous;
  # not a tuned alarm threshold (ADR embervm/031 Consequences).
  @tier2_max_age_ms 86_400_000
  # Grace added on top of the floor, stated RELATIVE to the sweep cadence so it
  # tracks the interval rather than needing a manual update alongside it (ADR
  # risk row 3). Default matches S3WarmthGc's own @sweep_interval_ms.
  @default_sweep_interval_ms 3_600_000

  # -- Client API --------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  The latest durability evaluation, computing one synchronously when the first
  round has not run yet. `:suspended` while the detector is dark
  (`enabled: false`), which the router surfaces as 404: a dark detector must
  neither read green nor page anybody.
  """
  @spec snapshot(GenServer.server()) :: map() | :suspended
  def snapshot(server \\ __MODULE__) do
    GenServer.call(server, :snapshot, 30_000)
  end

  # -- Pure evaluation (public for tests) --------------------------------------

  @doc """
  Evaluate both tiers from one capacity-table read. PURE: every input is
  injected, so tests drive each boundary deterministically. Contract: called
  once per round (~`:round_interval_ms` apart); `prev_streaks` carries the
  previous round's per-kind counters (an empty map on the first round).

  Options:

    * `:now_mono` - monotonic ms now (node-fact freshness)
    * `:now_wall` - wall-clock unix ms (tier 2 manifest age)
    * `:freshness_window_ms`, `:streak_threshold`, `:sweep_interval_ms`
    * `:s3` - nil or `%{list: fun}` where list returns
      `{:ok, [%{key:, size:, last_modified_ms:}]}` | `{:error, term}`
  """
  def evaluate(facts, expected_nodes, prev_streaks, opts \\ []) do
    now_mono = Keyword.get(opts, :now_mono, System.monotonic_time(:millisecond))
    now_wall = Keyword.get(opts, :now_wall, System.system_time(:millisecond))
    freshness_window_ms = Keyword.get(opts, :freshness_window_ms, @freshness_window_ms)
    threshold = Keyword.get(opts, :streak_threshold, @streak_threshold)
    sweep_interval_ms = Keyword.get(opts, :sweep_interval_ms, @default_sweep_interval_ms)
    s3 = Keyword.get(opts, :s3)

    {fresh, missing} = split_fresh(facts, expected_nodes, now_mono, freshness_window_ms)
    tier1 = evaluate_tier1(facts, fresh, missing, prev_streaks, threshold)
    tier2 = evaluate_tier2(s3, now_wall, sweep_interval_ms)

    %{
      ok: tier1.ok and tier2.ok,
      evaluated_at_unix_ms: now_wall,
      tier1: Map.merge(tier1, %{threshold_rounds: threshold, fresh_nodes: fresh, missing_nodes: missing}),
      tier2: Map.put(tier2, :stall_bound_ms, bound_ms(sweep_interval_ms))
    }
  end

  @doc """
  The kinds with at least one present-but-unexported artifact in these facts,
  as `%{kind => pending_artifact_count}`. One fact map per dispatchable
  instance (NodeCapacity.all shape); missing lists read as empty.
  """
  def pending_kinds(facts) when is_list(facts) do
    Enum.reduce(facts, %{}, fn fact, acc ->
      acc
      |> count_unexported(fact, :session, :session_snapshots, :snapshot_ref)
      |> count_unexported(fact, :serving, :serving_snapshots, :snapshot_ref)
      |> count_unexported(fact, :stateful, :stateful_bundles, :snapshot_ref)
      |> count_unexported(fact, :group_set, :group_bundle_sets, :set_id)
      |> count_unexported_bases(fact)
    end)
  end

  defp count_unexported(acc, fact, kind, list_key, id_key) do
    entries = Map.get(fact, list_key, []) || []

    pending =
      Enum.count(entries, fn entry ->
        ref = Map.get(entry, id_key)
        is_binary(ref) and ref != "" and Map.get(entry, :exported) != true
      end)

    if pending > 0, do: Map.update(acc, kind, pending, &(&1 + pending)), else: acc
  end

  # A base counts when it is READY (built) with a current snapshot_ref but no
  # confirmed store copy: exactly the state BaseBuilder's bounded reconcile
  # keeps trying to export. Building/priming states are not pending exports.
  defp count_unexported_bases(acc, fact) do
    workloads = Map.get(fact, :workloads, %{}) || %{}

    pending =
      Enum.count(workloads, fn {_workload, wc} ->
        is_map(wc) and Map.get(wc, :base_state) == :BASE_BUILD_STATE_READY and
          is_binary(Map.get(wc, :snapshot_ref)) and Map.get(wc, :snapshot_ref) != "" and
          Map.get(wc, :exported) != true
      end)

    if pending > 0, do: Map.update(acc, :base, pending, &(&1 + pending)), else: acc
  end

  # Fresh = present AND updated within the window, mirroring the S3 GC's
  # fleet-freshness semantics: NodeCapacity DROPS a non-dispatchable node's
  # row, so absence is indistinguishable from staleness here and both are
  # "missing". An EMPTY expected-node list leaves everything missing: no fleet
  # contract means no basis to call tier 1 green.
  defp split_fresh(_facts, [], _now, _window), do: {[], []}

  defp split_fresh(facts, expected_nodes, now, window) do
    by_node =
      for fact <- facts,
          node = Map.get(fact, :node_id),
          is_binary(node),
          into: %{},
          do: {node, now - (Map.get(fact, :updated_at, now - window - 1) || now - window - 1)}

    Enum.split_with(expected_nodes, fn node ->
      case Map.fetch(by_node, node) do
        {:ok, age} when is_integer(age) -> age <= window
        :error -> false
      end
    end)
  end

  # No fresh expected node at all (none configured, or none reporting): tier 1
  # cannot judge. A streak that has ALREADY latched stays latched (the health
  # surface must not heal just because the witnesses went silent); otherwise
  # this reads UNKNOWN, never green (vacuous-green guard).
  defp evaluate_tier1(_facts, [], missing, prev_streaks, threshold) do
    latched =
      prev_streaks
      |> Enum.filter(fn {_kind, count} -> count >= threshold end)
      |> Enum.map(fn {kind, _count} -> kind end)
      |> Enum.sort()

    cond do
      latched != [] ->
        %{
          ok: false,
          verdict: :export_failure_streak,
          detail:
            "export failure streak already latched for kinds: " <>
              "#{Enum.join(Enum.map(latched, &Atom.to_string/1), ", ")}; no fresh fleet " <>
              "facts to prove recovery (missing/stale nodes: #{inspect(missing)})",
          streaks: prev_streaks,
          failing_kinds: latched
        }

      missing == [] ->
        unknown_tier1(
          "no expected nodes configured; tier 1 cannot judge export durability",
          prev_streaks,
          threshold
        )

      true ->
        unknown_tier1(
          "none of the expected nodes are reporting fresh capacity facts: #{inspect(missing)}",
          prev_streaks,
          threshold
        )
    end
  end

  defp evaluate_tier1(facts, fresh, missing, prev_streaks, threshold) do
    pending = pending_kinds(facts)

    streaks =
      Map.new(@kinds, fn kind ->
        if Map.has_key?(pending, kind) do
          {kind, Map.get(prev_streaks, kind, 0) + 1}
        else
          {kind, 0}
        end
      end)

    failing =
      streaks
      |> Enum.filter(fn {_kind, count} -> count >= threshold end)
      |> Enum.map(fn {kind, _count} -> kind end)
      |> Enum.sort()

    cond do
      failing != [] ->
        %{
          ok: false,
          verdict: :export_failure_streak,
          detail:
            "artifact exports have been failing for #{threshold} consecutive rounds (~" <>
              "#{div(threshold * @round_interval_ms, 60_000)} min) for kinds: " <>
              "#{Enum.join(Enum.map(failing, &Atom.to_string/1), ", ")}; " <>
              "sessions parking in this window rehydrate blank",
          streaks: streaks,
          failing_kinds: failing
        }

      # Some expected nodes missing/stale but nothing firing: the silence could
      # be the outage, so this is unknown, never green (vacuous-green guard).
      missing != [] ->
        unknown_tier1(
          "expected nodes missing or stale in capacity facts: #{inspect(missing)}",
          streaks,
          threshold
        )

      true ->
        %{
          ok: true,
          verdict: :ok,
          detail: "all tracked artifact kinds have confirmed store copies",
          streaks: streaks,
          failing_kinds: []
        }
    end
  end

  defp unknown_tier1(detail, streaks_or_prev, _threshold) do
    %{
      ok: false,
      verdict: :unknown,
      detail: detail,
      # Keep whatever counters exist so a streak survives a transient
      # fleet-stale round instead of resetting on it.
      streaks: streaks_or_prev,
      failing_kinds: []
    }
  end

  defp evaluate_tier2(s3, now_wall, sweep_interval_ms) do
    case s3 do
      nil ->
        unknown_tier2("no object store configured; gc-manifests are unreadable")

      %{list: list} ->
        case safe_list(list) do
          {:ok, []} ->
            unknown_tier2("no #{trim_prefix()} objects: no sweep has ever persisted a manifest")

          {:ok, entries} ->
            newest =
              entries
              |> Enum.map(&Map.get(&1, :last_modified_ms, 0))
              |> Enum.max()

            age = now_wall - newest
            bound = bound_ms(sweep_interval_ms)

            if age > bound do
              %{
                ok: false,
                verdict: :gc_sweep_stalled,
                detail:
                  "newest gc-manifests object is #{age}ms old, beyond the " <>
                    "#{bound}ms stall bound (24h + sweep interval): the warmth GC has " <>
                    "not completed a sweep, for any reason, since then",
                newest_manifest_age_ms: age
              }
            else
              %{
                ok: true,
                verdict: :ok,
                detail: "newest gc-manifests object is fresh",
                newest_manifest_age_ms: age
              }
            end

          {:error, reason} ->
            unknown_tier2("gc-manifests listing failed: #{inspect(reason)}")
        end

      other ->
        unknown_tier2("malformed s3 seam: #{inspect(other)}")
    end
  end

  # A raising list fun is an evaluation failure, not a healthy signal.
  defp safe_list(list) do
    try do
      list.(@manifest_prefix)
    rescue
      e -> {:error, e}
    catch
      kind, reason -> {:error, {kind, reason}}
    end
  end

  defp unknown_tier2(detail) do
    %{ok: false, verdict: :unknown, detail: detail, newest_manifest_age_ms: nil}
  end

  defp bound_ms(sweep_interval_ms), do: @tier2_max_age_ms + sweep_interval_ms

  defp trim_prefix, do: String.trim_trailing(@manifest_prefix, "/")

  # -- GenServer ---------------------------------------------------------------

  @impl true
  def init(opts) do
    endpoint = Keyword.get(opts, :endpoint, "")
    bucket = Keyword.get(opts, :bucket, "embervm")
    access_key_id = Keyword.get(opts, :access_key_id, "") || ""
    secret_access_key = Keyword.get(opts, :secret_access_key, "") || ""

    credential_opts =
      cond do
        access_key_id == "" and secret_access_key == "" -> []
        access_key_id == "" or secret_access_key == "" -> []
        true -> [access_key_id: access_key_id, secret_access_key: secret_access_key]
      end

    client =
      if endpoint != "",
        do: Embervm.S3Client.new(endpoint, bucket, credential_opts),
        else: nil

    # Only the listing seam is needed: tier 2 measures the NEWEST manifest's
    # age from the listing itself, so it never fetches object bodies.
    s3 =
      Keyword.get_lazy(opts, :s3, fn ->
        if client, do: %{list: fn prefix -> Embervm.S3Client.list_all(client, prefix) end}
      end)

    state = %{
      enabled: Keyword.get(opts, :enabled, false),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      expected_nodes: Keyword.get(opts, :expected_nodes, []),
      round_interval_ms: Keyword.get(opts, :round_interval_ms, @round_interval_ms),
      streak_threshold: Keyword.get(opts, :streak_threshold, @streak_threshold),
      freshness_window_ms: Keyword.get(opts, :freshness_window_ms, @freshness_window_ms),
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, @default_sweep_interval_ms),
      s3: s3,
      clock: Keyword.get(opts, :clock, fn -> System.monotonic_time(:millisecond) end),
      wall_clock: Keyword.get(opts, :wall_clock, fn -> System.system_time(:millisecond) end),
      streaks: %{},
      last: nil
    }

    if state.enabled and state.round_interval_ms > 0 do
      # First round asynchronously (never delay boot behind an S3 listing);
      # a snapshot arriving first computes synchronously instead.
      Process.send_after(self(), :round, 0)
    end

    {:ok, state}
  end

  @impl true
  def handle_call(:snapshot, _from, %{enabled: false} = state), do: {:reply, :suspended, state}

  def handle_call(:snapshot, _from, %{last: nil} = state) do
    {report, streaks} = run_round(state)
    {:reply, report, %{state | last: report, streaks: streaks}}
  end

  def handle_call(:snapshot, _from, state), do: {:reply, state.last, state}

  def handle_call(:round_now, _from, state) do
    {report, streaks} = run_round(state)
    {:reply, report, %{state | last: report, streaks: streaks}}
  end

  @impl true
  def handle_info(:round, %{enabled: true, round_interval_ms: ms} = state) do
    {report, streaks} = run_round(state)
    Process.send_after(self(), :round, ms)
    {:noreply, %{state | last: report, streaks: streaks}}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  defp run_round(state) do
    opts = [
      now_mono: state.clock.(),
      now_wall: state.wall_clock.(),
      freshness_window_ms: state.freshness_window_ms,
      streak_threshold: state.streak_threshold,
      sweep_interval_ms: state.sweep_interval_ms,
      s3: state.s3
    ]

    report = evaluate(NodeCapacity.all(state.capacity_table), state.expected_nodes, state.streaks, opts)
    log_transitions(state.last, report)
    {report, get_in(report, [:tier1, :streaks]) || %{}}
  end

  # Log only EDGES, not every round: a sustained outage logs once on latch and
  # once on recovery, keeping the log readable during exactly the incidents
  # this module exists for.
  defp log_transitions(nil, report) do
    unless report.ok, do: log_not_ok(report)
  end

  defp log_transitions(%{ok: true}, %{ok: false} = report), do: log_not_ok(report)

  defp log_transitions(%{ok: false}, %{ok: true} = report) do
    Logger.info("embervm durability health: RECOVERED: both durability tiers ok")
    Logger.info("embervm durability health: #{format_report(report)}")
  end

  defp log_transitions(_was, _now), do: :ok

  defp log_not_ok(report) do
    Logger.warning("embervm durability health: NOT OK: #{format_report(report)}")
  end

  defp format_report(report) do
    t1 = report.tier1
    t2 = report.tier2

    "tier1=#{t1.verdict} tier1_detail=\"#{t1.detail}\" " <>
      "tier2=#{t2.verdict} tier2_detail=\"#{t2.detail}\""
  end
end
