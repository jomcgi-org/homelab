defmodule Embervm.S3WarmthGc do
  @moduledoc """
  S3-direct warmth GC (task #39): the reconciled sweep that reclaims orphaned
  warmth prefixes DIRECTLY from the object store, enumerating from S3 itself
  rather than from any node's reported inventory.

  ## why a third GC arm exists

  The event-driven sweepers (Stateful/GroupSweeper) evict warmth for instances
  the CP still tracks; the WarmthReaper reconciles each node's REPORTED on-disk
  inventory against the CP's desired set. Neither can reach the pre-sidecar
  backlog (~120G: 143 stateful bundles + leaked group sets) whose
  (workload / group_instance_id) binding was lost on a post-restart boot scan:
  noded refuses to compose an empty-workload S3 prefix (fail-closed, #38 fix C),
  its store client has no List, and nothing node-side can even enumerate the
  stranded keys. Only a CP-side client listing the bucket can find them, so this
  module owns exactly that: List S3, prove each prefix orphaned against every
  in-process truth source, and (gated) delete it.

  ## DESTRUCTIVE, therefore fail-closed everywhere

  S3 deletion is irreversible. The design (docs/plans/2026-07-22-embervm-s3-
  warmth-gc.md) is dry-run-first and fail-closed at every fork:

    * The periodic sweep ALWAYS computes and logs the plan (and persists a
      manifest to `gc-manifests/<ts>.json` for audit); the destructive arm is
      gated by `EMBERVM_WARMTH_S3_GC` (default off, values-flipped like every
      other retention gate).
    * ANY list/pagination error aborts the whole sweep: a partial listing is
      indistinguishable from absence, and absence is what we delete on.
    * Fleet-freshness precondition: EVERY expected node (values-configured;
      empty = never sweep) must be present in NodeCapacity with a fresh
      `updated_at`. NodeCapacity DROPS a non-dispatchable node's row, so a down
      node's unreported disk bundles would otherwise look orphaned.
    * Empty-CP-state guard: candidates in S3 while the corresponding store
      tracks NOTHING aborts (a sweep firing mid-rebuild after a CP restart must
      never read "empty desired set" as "everything is orphaned"). A minimum CP
      uptime is additionally required before any sweep.
    * Ambiguous key parses (a workload literally named like a vendor) are
      skipped, never guessed.
    * Deletes: hard allowlist {stateful/, session/, serving/,
      session-workspace/, group_set/}; every DELETE targets one
      fully-qualified LISTED key (never a prefix, never bucket-wide); meta.json
      goes FIRST so a crashed half-delete reads as incomplete, not stale-valid;
      each prefix is rechecked against live ETS immediately before its deletes;
      per-sweep caps bound prefix count and bytes; any delete failure aborts
      the remainder of the sweep.

  ## the orphan predicate (ALL must hold)

  For a stateful prefix `stateful/[<vendor>/]<workload>/<ref>`:
    1. `ref` is not any non-terminal StatefulStore instance's snapshot_ref
       (desired, across vendors: refs are globally unique).
    2. `ref` is not in any node's reported stateful_bundles.
    3. Tier gate: Tier 1 (dead workload: no non-terminal instance AND no volume
       row) makes the whole namespace eligible; Tier 2 (live workload) protects
       the current snapshot_ref(s) plus the newest ref per (vendor, workload)
       and trims only strictly older predecessors (the S3 newest-1 retention).
    4. Age: meta.json createdAtUnixMs (or newest Last-Modified when meta is
       absent) older than the per-prefix TTL: 8 hours for stateful/ and 7 days
       for the other allowlisted artifact kinds.
    5. The key parse was unambiguous.
    6. The fleet-freshness precondition held for the whole sweep.

  Group prefixes (`group_set/...`) are Tier 1 only: set_id not desired, not
  node-reported, owning group instance absent or terminal, plus the age gate.

  Session and serving prefixes have no predecessor guard. They are held only
  while their instance is actively live or node-reported. Parked sessions and
  their workspace lineages use the age gate once the CP expiry deadline has
  passed; an orphan with no registry row uses the configured age gate directly.

  Coexistence with the WarmthReaper (#36) is disjoint by construction: this GC
  excludes anything node-reported, the reaper enumerates only what nodes report,
  and both are idempotent on 404.
  """

  use GenServer
  require Logger

  alias Embervm.{GroupState, GroupStore, NodeCapacity, S3Client, SessionState, SessionStore, ServingState, ServingStore, StatefulState, StatefulStore}

  # Slow-moving reconcile: an S3 orphan is born rarely (a binding lost across a
  # restart), so hourly dry-run visibility is ample and keeps manifest churn low.
  @sweep_interval_ms 3_600_000

  # Per-prefix age floors: a prefix younger than its TTL is NEVER eligible,
  # absorbing any skew between an in-flight export and the CP's view of it.
  @default_ttls %{
    stateful: 8 * 60 * 60 * 1000,
    session: 7 * 24 * 60 * 60 * 1000,
    serving: 7 * 24 * 60 * 60 * 1000,
    session_workspace: 7 * 24 * 60 * 60 * 1000,
    group: 7 * 24 * 60 * 60 * 1000
  }

  # Fleet-freshness window: ~2 noded status-report intervals (30s each). A node
  # present in NodeCapacity but not updated within this window aborts the sweep.
  @freshness_window_ms 120_000

  # Minimum CP uptime before ANY sweep: the stores rebuild from the op-log and
  # the fleet re-registers via dial-home within seconds, but deletion earns no
  # speed from running early, so wait out the whole settling window.
  @min_uptime_ms 300_000

  # Per-sweep caps (values-overridable): the supervised-first-run defaults.
  @max_prefixes 10
  @max_bytes 20 * 1024 * 1024 * 1024

  # The vendor tokens noded can stamp (config.go detectCpuVendor): segment-2
  # membership here disambiguates the vendored (5-segment) from the legacy
  # (4-segment) key layout.
  @vendors ["amd", "intel"]

  # The ONLY prefixes this GC may ever touch. Each rule is explicit: base/ and
  # volume/ remain hard-excluded.
  @allowlist ["stateful/", "session/", "serving/", "session-workspace/", "group_set/"]

  # -- Client API --------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Run one sweep synchronously and return `{:ok, result}` (the plan that was
  logged, plus what was deleted with the gate on) or `{:error, reason}` (an
  abort; nothing was deleted). The supervised first live run drives this from a
  remote console; tests drive it deterministically without the timer.
  """
  @spec sweep_now(GenServer.server()) :: {:ok, map()} | {:error, term()}
  def sweep_now(server \\ __MODULE__) do
    GenServer.call(server, :sweep_now, 120_000)
  end

  # -- GenServer callbacks -----------------------------------------------------

  @impl true
  def init(opts) do
    endpoint = Keyword.get(opts, :endpoint, "")
    bucket = Keyword.get(opts, :bucket, "embervm")
    client = S3Client.new(endpoint, bucket)

    # The S3 seam: four funs over the real client by default; tests inject a
    # fake map. A nil client (empty endpoint) leaves the seam nil and every
    # sweep skips inert.
    s3 =
      Keyword.get_lazy(opts, :s3, fn ->
        if client do
          %{
            list: fn prefix -> S3Client.list_all(client, prefix) end,
            get: fn key -> S3Client.get(client, key) end,
            delete: fn key -> S3Client.delete(client, key) end,
            put: fn key, body -> S3Client.put(client, key, body) end
          }
        end
      end)

    clock = Keyword.get(opts, :clock, fn -> System.monotonic_time(:millisecond) end)
    stateful_store = Keyword.get(opts, :stateful_store, StatefulStore)

    state = %{
      s3: s3,
      enabled: Keyword.get(opts, :enabled, false),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      stateful_store: stateful_store,
      session_store: Keyword.get(opts, :session_store, SessionStore),
      serving_store: Keyword.get(opts, :serving_store, ServingStore),
      group_store: Keyword.get(opts, :group_store, GroupStore),
      # The volume-ledger read for the Tier-1 dead-workload check; injectable so
      # tests do not need a real StatefulStore volume projection.
      volume_fun: Keyword.get(opts, :volume_fun, fn workload -> StatefulStore.get_volume(stateful_store, workload) end),
      expected_nodes: Keyword.get(opts, :expected_nodes, []),
      freshness_window_ms: Keyword.get(opts, :freshness_window_ms, @freshness_window_ms),
      min_uptime_ms: Keyword.get(opts, :min_uptime_ms, @min_uptime_ms),
      ttls: Map.merge(@default_ttls, Keyword.get(opts, :ttls, %{})),
      max_prefixes: Keyword.get(opts, :max_prefixes, @max_prefixes),
      max_bytes: Keyword.get(opts, :max_bytes, @max_bytes),
      vendors: Keyword.get(opts, :vendors, @vendors),
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, @sweep_interval_ms),
      # Monotonic clock (NodeCapacity.updated_at is monotonic ms) + wall clock
      # (age gate against meta createdAtUnixMs / S3 Last-Modified, manifest ts).
      clock: clock,
      wall_clock: Keyword.get(opts, :wall_clock, fn -> System.system_time(:millisecond) end),
      started_at: clock.()
    }

    schedule_sweep(state)
    {:ok, state}
  end

  @impl true
  def handle_call(:sweep_now, _from, state) do
    {:reply, run_sweep(state), state}
  end

  @impl true
  def handle_info(:sweep, state) do
    run_sweep(state)
    schedule_sweep(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  defp schedule_sweep(%{sweep_interval_ms: ms}) when ms > 0 do
    Process.send_after(self(), :sweep, ms)
    :ok
  end

  defp schedule_sweep(_state), do: :ok

  # -- the sweep ---------------------------------------------------------------

  defp run_sweep(%{s3: nil}) do
    # No store endpoint configured: inert by construction, mirroring noded's
    # disabled-store degrade. Not an ABORT (nothing was expected to run).
    {:error, :store_disabled}
  end

  defp run_sweep(state) do
    with :ok <- check_uptime(state),
         :ok <- check_fleet_fresh(state),
         {:ok, stateful_keys} <- list_or_abort(state, "stateful/"),
         {:ok, session_keys} <- list_or_abort(state, "session/"),
         {:ok, serving_keys} <- list_or_abort(state, "serving/"),
         {:ok, workspace_keys} <- list_or_abort(state, "session-workspace/"),
         {:ok, group_keys} <- list_or_abort(state, "group_set/"),
         {:ok, snapshot} <- cp_snapshot(state),
         :ok <- check_empty_cp_state(snapshot, stateful_keys, session_keys, serving_keys, workspace_keys, group_keys) do
      {candidates, ambiguous} = parse_candidates(state, stateful_keys, session_keys, serving_keys, workspace_keys, group_keys)
      {eligible, held} = build_plan(state, snapshot, candidates)
      plan = apply_caps(state, eligible)
      log_plan(state, plan, eligible, held, ambiguous)

      case persist_manifest(state, plan, eligible, held, ambiguous) do
        :ok ->
          deleted = if state.enabled, do: apply_deletes(state, snapshot, plan), else: []
          {:ok, %{plan: plan, eligible: eligible, held: held, ambiguous: ambiguous, deleted: deleted}}

        {:error, reason} ->
          # No manifest, no delete: the audit record must exist BEFORE anything
          # is destroyed. The dry-run plan was still logged above.
          abort(:manifest_persist_failed, reason)
      end
    end
  end

  # -- preconditions -----------------------------------------------------------

  defp check_uptime(state) do
    up = state.clock.() - state.started_at

    if up >= state.min_uptime_ms do
      :ok
    else
      abort(:cp_too_young, "uptime #{up}ms < required #{state.min_uptime_ms}ms (stores may still be rebuilding)")
    end
  end

  # THE load-bearing safety check. NodeCapacity's per-node staleness contract:
  # every fact carries `updated_at` (monotonic ms, stamped by NodeRegistry on
  # each NodeStatus projection), and the registry DROPS a node's row the moment
  # it stops being dispatchable. So "this node's disk inventory is represented
  # in the facts we exclude against" requires BOTH presence and freshness for
  # EVERY expected node; a down/stale node's unreported bundles must never look
  # orphaned. An EMPTY expected-node list aborts too: no fleet contract
  # configured means no basis to claim the facts are complete.
  defp check_fleet_fresh(%{expected_nodes: []}) do
    abort(:no_expected_nodes, "expectedNodes is empty; cannot prove node-reported inventory is complete")
  end

  defp check_fleet_fresh(state) do
    now = state.clock.()

    stale =
      Enum.filter(state.expected_nodes, fn node ->
        case NodeCapacity.fetch(state.capacity_table, node) do
          {:ok, facts} -> now - Map.get(facts, :updated_at, now - state.freshness_window_ms - 1) > state.freshness_window_ms
          :error -> true
        end
      end)

    if stale == [] do
      :ok
    else
      abort(:fleet_stale, "nodes missing or stale in NodeCapacity: #{inspect(stale)}")
    end
  end

  defp list_or_abort(state, prefix) do
    case state.s3.list.(prefix) do
      {:ok, entries} ->
        if Enum.all?(entries, fn entry -> allowlisted?(entry.key) and String.starts_with?(entry.key, prefix) end) do
          {:ok, entries}
        else
          abort(:allowlist_violation, "listing #{prefix} returned a key outside the hard allowlist or requested prefix")
        end
      {:error, reason} -> abort(:list_failed, "listing #{prefix} failed: #{inspect(reason)} (partial listings delete on absence, so the whole sweep aborts)")
    end
  end

  # One consistent read of every CP truth source. Store call failures abort (a
  # dead store must read as "unknown", never as "empty").
  defp cp_snapshot(state) do
    stateful_rows = StatefulStore.all(state.stateful_store)
    group_rows = GroupStore.all(state.group_store)
    session_rows = SessionStore.all(state.session_store)
    serving_rows = ServingStore.all(state.serving_store)
    facts = NodeCapacity.all(state.capacity_table)

    reported_refs =
      for fact <- facts,
          bundle <- Map.get(fact, :stateful_bundles, []) || [],
          is_binary(bundle.snapshot_ref),
          bundle.snapshot_ref != "",
          into: MapSet.new(),
          do: bundle.snapshot_ref

    reported_set_ids =
      for fact <- facts,
          set <- Map.get(fact, :group_bundle_sets, []) || [],
          is_binary(set.set_id),
          set.set_id != "",
          into: MapSet.new(),
          do: set.set_id

    reported_session_refs =
      for fact <- facts,
          ref <- Map.get(fact, :session_snapshots, []) || [],
          is_binary(ref.snapshot_ref), ref.snapshot_ref != "",
          into: MapSet.new(), do: ref.snapshot_ref

    reported_serving_refs =
      for fact <- facts,
          ref <- Map.get(fact, :serving_snapshots, []) || [],
          is_binary(ref.snapshot_ref), ref.snapshot_ref != "",
          into: MapSet.new(), do: ref.snapshot_ref

    reported_lineages =
      for fact <- facts,
          volume <- Map.get(fact, :session_volumes, []) || [],
          is_binary(volume.lineage_id), volume.lineage_id != "",
          into: MapSet.new(), do: volume.lineage_id

    active_session_rows = Enum.filter(session_rows, &session_actively_live?/1)
    active_serving_rows = Enum.filter(serving_rows, &serving_actively_live?/1)

    referenced_lineages =
      for row <- active_session_rows,
          is_binary(row.lineage_id), row.lineage_id != "",
          into: MapSet.new(), do: row.lineage_id

    referenced_session_refs =
      for row <- active_session_rows,
          is_binary(row.snapshot_ref), row.snapshot_ref != "",
          into: MapSet.new(), do: row.snapshot_ref

    referenced_serving_refs =
      for row <- active_serving_rows,
          is_binary(row.snapshot_ref), row.snapshot_ref != "",
          into: MapSet.new(), do: row.snapshot_ref

    non_terminal_stateful = Enum.reject(stateful_rows, &StatefulState.terminal?(&1.state))

    snapshot = %{
      stateful_count: length(stateful_rows),
      group_count: length(group_rows),
      session_count: length(session_rows),
      serving_count: length(serving_rows),
      desired_refs:
        for(%{snapshot_ref: ref} <- non_terminal_stateful, is_binary(ref), ref != "", into: MapSet.new(), do: ref),
      live_workloads: MapSet.new(non_terminal_stateful, & &1.workload),
      desired_set_ids:
        for(
          row <- group_rows,
          not GroupState.terminal?(row.state),
          is_binary(row.set_id),
          row.set_id != "",
          into: MapSet.new(),
          do: row.set_id
        ),
      live_group_ids:
        for(row <- group_rows, not GroupState.terminal?(row.state), into: MapSet.new(), do: row.instance_id),
      reported_refs: reported_refs,
      reported_set_ids: reported_set_ids,
      reported_session_refs: reported_session_refs,
      reported_serving_refs: reported_serving_refs,
      reported_lineages: reported_lineages,
      referenced_lineages: referenced_lineages,
      referenced_session_refs: referenced_session_refs,
      referenced_serving_refs: referenced_serving_refs,
      parked_session_expiries:
        for(
          row <- session_rows,
          row.state == :parked,
          is_binary(row.snapshot_ref),
          row.snapshot_ref != "",
          is_integer(Map.get(row, :expires_at)),
          into: %{},
          do: {{:ref, row.snapshot_ref}, Map.get(row, :expires_at)}
        ),
      parked_lineage_expiries:
        for(
          row <- session_rows,
          row.state == :parked,
          is_binary(row.lineage_id),
          row.lineage_id != "",
          is_integer(Map.get(row, :expires_at)),
          into: %{},
          do: {{:lineage, row.lineage_id}, Map.get(row, :expires_at)}
        )
    }

    {:ok, snapshot}
  rescue
    e -> abort(:cp_snapshot_failed, inspect(e))
  catch
    kind, reason -> abort(:cp_snapshot_failed, inspect({kind, reason}))
  end

  # If S3 holds warmth of a kind while the corresponding store tracks NOTHING
  # AT ALL (not merely nothing non-terminal: terminal history counts as "the
  # store has rebuilt"), the CP state is suspect (mid-rebuild, wrong op-log,
  # fresh install against an old bucket). Never treat empty as all-orphaned.
  defp check_empty_cp_state(snapshot, stateful_keys, session_keys, serving_keys, workspace_keys, group_keys) do
    cond do
      stateful_keys != [] and snapshot.stateful_count == 0 ->
        abort(:empty_cp_state, "S3 holds stateful/ objects but StatefulStore tracks no instances at all")

      group_keys != [] and snapshot.group_count == 0 ->
        abort(:empty_cp_state, "S3 holds group_set/ objects but GroupStore tracks no instances at all")

      (session_keys != [] or workspace_keys != []) and snapshot.session_count == 0 ->
        abort(:empty_cp_state, "S3 holds session/ or session-workspace/ objects but SessionStore tracks no rows at all")

      serving_keys != [] and snapshot.serving_count == 0 ->
        abort(:empty_cp_state, "S3 holds serving/ objects but ServingStore tracks no rows at all")

      true ->
        :ok
    end
  end

  defp abort(code, detail) do
    Logger.error("embervm s3 warmth gc: ABORT #{code}: #{detail}")
    {:error, code}
  end

  # -- key parsing -------------------------------------------------------------

  # Group every listed key into its artifact prefix, disambiguating the vendored
  # (5-segment) from the legacy (4-segment) layout by segment count AND
  # segment-2 membership in the known vendor set. Anything else (a workload
  # named like a vendor, an unexpected depth) is AMBIGUOUS: the whole key is
  # held aside and never composed into a deletable prefix (fail-closed).
  # Returns {candidates_by_prefix, ambiguous_keys}.
  defp parse_candidates(state, stateful_keys, session_keys, serving_keys, workspace_keys, group_keys) do
    {s_parsed, s_ambiguous} = split_parsed(state, stateful_keys, "stateful")
    {se_parsed, se_ambiguous} = split_parsed(state, session_keys, "session")
    {sv_parsed, sv_ambiguous} = split_parsed(state, serving_keys, "serving")
    {w_parsed, w_ambiguous} = split_parsed(state, workspace_keys, "session-workspace")
    {g_parsed, g_ambiguous} = split_parsed(state, group_keys, "group_set")

    candidates =
      (s_parsed ++ se_parsed ++ sv_parsed ++ w_parsed ++ g_parsed)
      |> Enum.group_by(fn {meta, _entry} -> meta.prefix end)
      |> Enum.map(fn {prefix, pairs} ->
        {meta, _} = hd(pairs)
        entries = Enum.map(pairs, fn {_m, entry} -> entry end)

        Map.merge(meta, %{
          prefix: prefix,
          files: entries,
          bytes: entries |> Enum.map(& &1.size) |> Enum.sum(),
          newest_modified_ms: entries |> Enum.map(& &1.last_modified_ms) |> Enum.max()
        })
      end)

    {candidates, s_ambiguous ++ se_ambiguous ++ sv_ambiguous ++ w_ambiguous ++ g_ambiguous}
  end

  defp split_parsed(state, entries, kind) do
    Enum.reduce(entries, {[], []}, fn entry, {parsed, ambiguous} ->
      case parse_key(state, entry.key, kind) do
        {:ok, meta} -> {[{meta, entry} | parsed], ambiguous}
        :ambiguous -> {parsed, [entry.key | ambiguous]}
      end
    end)
  end

  defp parse_key(state, key, kind) do
    case String.split(key, "/") do
      ["session-workspace", workload, lineage, file] when file != "" ->
        {:ok, prefix_meta(kind, "", workload, lineage)}

      ["session-workspace", _vendor, _workload, _lineage, file] when file != "" ->
        :ambiguous

      # Vendored: <kind>/<vendor>/<owner>/<ref>/<file>
      [^kind, vendor, owner, ref, file] when file != "" ->
        if vendor in state.vendors and owner not in state.vendors do
          {:ok, prefix_meta(kind, vendor, owner, ref)}
        else
          :ambiguous
        end

      # Legacy pre-R7: <kind>/<owner>/<ref>/<file>. An owner named like a vendor
      # would collide with a vendored key missing its file segment: ambiguous.
      [^kind, owner, ref, file] when file != "" ->
        if owner in state.vendors, do: :ambiguous, else: {:ok, prefix_meta(kind, "", owner, ref)}

      _ ->
        :ambiguous
    end
  end

  defp prefix_meta("stateful", vendor, workload, ref) do
    %{kind: :stateful, vendor: vendor, workload: workload, ref: ref, prefix: join_prefix("stateful", vendor, workload, ref)}
  end

  defp prefix_meta("group_set", vendor, group_instance_id, set_id) do
    %{
      kind: :group,
      vendor: vendor,
      group_instance_id: group_instance_id,
      set_id: set_id,
      prefix: join_prefix("group_set", vendor, group_instance_id, set_id)
    }
  end

  defp prefix_meta("session", vendor, workload, ref), do: %{kind: :session, vendor: vendor, workload: workload, ref: ref, prefix: join_prefix("session", vendor, workload, ref)}
  defp prefix_meta("serving", vendor, workload, ref), do: %{kind: :serving, vendor: vendor, workload: workload, ref: ref, prefix: join_prefix("serving", vendor, workload, ref)}
  defp prefix_meta("session-workspace", "", workload, lineage), do: %{kind: :session_workspace, vendor: "", workload: workload, ref: lineage, lineage: lineage, prefix: join_prefix("session-workspace", "", workload, lineage)}
  defp prefix_meta("session-workspace", _vendor, _workload, _lineage), do: nil

  defp join_prefix(kind, "", owner, ref), do: "#{kind}/#{owner}/#{ref}"
  defp join_prefix(kind, vendor, owner, ref), do: "#{kind}/#{vendor}/#{owner}/#{ref}"

  # -- plan --------------------------------------------------------------------

  # Evaluate the fail-closed predicate per candidate prefix. Returns
  # {eligible, held}: held carries the blocking reason for the manifest so an
  # operator can audit WHY each prefix survived.
  defp build_plan(state, snapshot, candidates) do
    created = created_at_map(state, candidates)
    protected = tier2_protected(state, snapshot, candidates, created)
    now = state.wall_clock.()

    Enum.reduce(candidates, {[], []}, fn cand, {eligible, held} ->
      case classify(state, snapshot, cand, created, protected, now) do
        {:eligible, tier, created_at} ->
          {[Map.merge(cand, %{tier: tier, created_at_ms: created_at}) | eligible], held}

        {:held, reason} ->
          {eligible, [%{prefix: cand.prefix, kind: cand.kind, bytes: cand.bytes, reason: reason} | held]}
      end
    end)
  end

  defp classify(state, snapshot, %{kind: :stateful} = cand, created, protected, now) do
    created_at = Map.get(created, cand.prefix)

    cond do
      # meta.json unreadable for a non-404 reason: age/tier facts are unknown.
      created_at == :error ->
        {:held, "meta_unreadable"}

      MapSet.member?(snapshot.desired_refs, cand.ref) ->
        {:held, "desired_ref"}

      MapSet.member?(snapshot.reported_refs, cand.ref) ->
        {:held, "node_reported"}

      now - created_at < ttl(state, cand.kind) ->
        {:held, "younger_than_age_floor"}

      workload_live?(state, snapshot, cand.workload) ->
        # Tier 2 (live-workload predecessor trim): the newest ref per
        # (vendor, workload) survives; only a
        # strictly older predecessor is eligible. A DEAD workload (Tier 1
        # below) is evicted whole, so the protection applies only here.
        if MapSet.member?(protected, cand.prefix) do
          {:held, "tier2_protected_newest"}
        else
          {:eligible, 2, created_at}
        end

      true ->
        {:eligible, 1, created_at}
    end
  end

  defp classify(state, snapshot, %{kind: kind} = cand, created, _protected, now)
       when kind in [:session, :serving] do
    created_at = Map.get(created, cand.prefix)
    refs = if kind == :session, do: snapshot.referenced_session_refs, else: snapshot.referenced_serving_refs
    reported = if kind == :session, do: snapshot.reported_session_refs, else: snapshot.reported_serving_refs

    cond do
      created_at == :error -> {:held, "meta_unreadable"}
      MapSet.member?(refs, cand.ref) -> {:held, "referenced_ref"}
      MapSet.member?(reported, cand.ref) -> {:held, "node_reported"}
      parked_session_not_expired?(snapshot, cand.ref, now) -> {:held, "session_not_expired"}
      now - created_at < ttl(state, kind) -> {:held, "younger_than_age_floor"}
      true -> {:eligible, 1, created_at}
    end
  end

  defp classify(state, snapshot, %{kind: :session_workspace} = cand, created, _protected, now) do
    created_at = Map.get(created, cand.prefix)

    cond do
      created_at == :error -> {:held, "meta_unreadable"}
      MapSet.member?(snapshot.referenced_lineages, cand.lineage) -> {:held, "lineage_referenced"}
      MapSet.member?(snapshot.reported_lineages, cand.lineage) -> {:held, "node_reported"}
      parked_session_not_expired?(snapshot, cand.lineage, now, :lineage) -> {:held, "session_not_expired"}
      now - created_at < ttl(state, :session_workspace) -> {:held, "younger_than_age_floor"}
      true -> {:eligible, 1, created_at}
    end
  end

  defp classify(state, snapshot, %{kind: :group} = cand, created, _protected, now) do
    created_at = Map.get(created, cand.prefix)

    cond do
      created_at == :error -> {:held, "meta_unreadable"}
      MapSet.member?(snapshot.desired_set_ids, cand.set_id) -> {:held, "desired_set"}
      MapSet.member?(snapshot.reported_set_ids, cand.set_id) -> {:held, "node_reported"}
      # Tier 1 only for groups: a LIVE owning group instance holds its set.
      MapSet.member?(snapshot.live_group_ids, cand.group_instance_id) -> {:held, "group_instance_live"}
      now - created_at < ttl(state, :group) -> {:held, "younger_than_age_floor"}
      true -> {:eligible, 1, created_at}
    end
  end

  defp ttl(state, kind), do: Map.fetch!(state.ttls, kind)

  # A workload is LIVE when the CP tracks a non-terminal instance for it OR the
  # volume ledger holds a row (a cold-but-real workload whose data volume
  # persists). Tier 2 (predecessor trim) applies; Tier 1 (whole-namespace
  # reclaim) requires neither.
  defp workload_live?(state, snapshot, workload) do
    MapSet.member?(snapshot.live_workloads, workload) or state.volume_fun.(workload) != nil
  end

  # created-at per prefix: meta.json createdAtUnixMs when present; the newest
  # file Last-Modified when meta is ABSENT (404); :error (fail-closed hold) when
  # meta exists but cannot be fetched or parsed.
  defp created_at_map(state, candidates) do
    Map.new(candidates, fn cand ->
      value =
        case state.s3.get.(cand.prefix <> "/meta.json") do
          {:ok, body} ->
            case safe_json_decode(body) do
              %{"createdAtUnixMs" => ms} when is_integer(ms) and ms > 0 -> ms
              _ -> :error
            end

          {:error, :not_found} ->
            cand.newest_modified_ms

          {:error, _reason} ->
            :error
        end

      {cand.prefix, value}
    end)
  end

  defp safe_json_decode(body) do
    :json.decode(body)
  rescue
    _ -> nil
  catch
    _, _ -> nil
  end

  # Tier-2 predecessor retention: within each (vendor, workload) stateful
  # namespace, the newest ref by created-at is protected regardless of age.
  # Computed over ALL parsed refs, desired or not, so the protection is at least
  # as wide as the retention contract. Session and serving have no such guard.
  defp tier2_protected(_state, _snapshot, candidates, created) do
    candidates
    |> Enum.filter(&(&1.kind == :stateful))
    |> Enum.group_by(&{&1.vendor, &1.workload})
    |> Enum.flat_map(fn {_vw, cands} ->
      cands
      |> Enum.sort_by(fn c -> sort_created(Map.get(created, c.prefix), c.newest_modified_ms) end, :desc)
      |> Enum.take(1)
      |> Enum.map(& &1.prefix)
    end)
    |> MapSet.new()
  end

  # An unreadable meta sorts as NEWEST so it also lands inside the protected-1
  # window rather than aging a sibling out of it incorrectly.
  defp sort_created(:error, _fallback), do: :infinity
  defp sort_created(nil, fallback), do: fallback
  defp sort_created(ms, _fallback), do: ms

  defp active_session_state?(state), do: not SessionState.terminal?(state) and state not in [:banked, :parked]
  defp active_serving_state?(state), do: not ServingState.terminal?(state) and state != :banked
  defp session_actively_live?(row), do: active_session_state?(row.state)
  defp serving_actively_live?(row), do: active_serving_state?(row.state)

  defp parked_session_not_expired?(snapshot, key, now, kind \\ :ref) do
    expiries = if kind == :lineage, do: snapshot.parked_lineage_expiries, else: snapshot.parked_session_expiries
    expiry = Map.get(expiries, {kind, key})
    is_integer(expiry) and expiry > now
  end

  # Caps: Tier 1 (dead workload, the backlog) before Tier 2, oldest first within
  # a tier; then bound by max prefixes AND cumulative bytes.
  defp apply_caps(state, eligible) do
    eligible
    |> Enum.sort_by(fn e -> {e.tier, e.created_at_ms} end)
    |> Enum.reduce({[], 0}, fn e, {taken, bytes} ->
      if length(taken) < state.max_prefixes and bytes + e.bytes <= state.max_bytes do
        {[e | taken], bytes + e.bytes}
      else
        {taken, bytes}
      end
    end)
    |> elem(0)
    |> Enum.reverse()
  end

  # -- logging + manifest ------------------------------------------------------

  defp log_plan(state, plan, eligible, held, ambiguous) do
    mode = if state.enabled, do: "ARMED", else: "DRY RUN"
    total_bytes = plan |> Enum.map(& &1.bytes) |> Enum.sum()

    Logger.info(
      "embervm s3 warmth gc: sweep (#{mode}): #{length(plan)}/#{length(eligible)} eligible prefixes in plan " <>
        "(~#{total_bytes} bytes, caps #{state.max_prefixes} prefixes / #{state.max_bytes} bytes), " <>
        "#{length(held)} held, #{length(ambiguous)} ambiguous keys skipped"
    )

    Enum.each(plan, fn e ->
      Logger.info(
        "embervm s3 warmth gc: plan tier=#{e.tier} prefix=#{e.prefix} bytes=#{e.bytes} created_at_ms=#{e.created_at_ms}"
      )
    end)
  end

  # The audit manifest, persisted to gc-manifests/<ts>.json BEFORE any delete
  # (and on every dry-run sweep, so the ops step "read the dry-run manifest"
  # has a durable object to read). gc-manifests/ is outside the delete
  # allowlist, so the GC can never consume its own audit trail.
  defp persist_manifest(state, plan, eligible, held, ambiguous) do
    ts = state.wall_clock.()

    manifest = %{
      "ts_unix_ms" => ts,
      "mode" => if(state.enabled, do: "armed", else: "dry_run"),
      "ttls_ms" => Map.new(state.ttls, fn {kind, ttl_ms} -> {Atom.to_string(kind), ttl_ms} end),
      "caps" => %{"max_prefixes" => state.max_prefixes, "max_bytes" => state.max_bytes},
      "plan" => Enum.map(plan, &manifest_entry/1),
      "eligible_beyond_caps" => Enum.map(eligible -- plan, &manifest_entry/1),
      "held" => Enum.map(held, fn h -> %{"prefix" => h.prefix, "reason" => h.reason, "bytes" => h.bytes} end),
      "ambiguous_keys" => ambiguous
    }

    body = manifest |> :json.encode() |> IO.iodata_to_binary()
    state.s3.put.("gc-manifests/#{ts}.json", body)
  rescue
    e -> {:error, {:manifest_encode_raised, e}}
  end

  defp manifest_entry(e) do
    %{
      "prefix" => e.prefix,
      "kind" => Atom.to_string(e.kind),
      "tier" => e.tier,
      "bytes" => e.bytes,
      "created_at_ms" => e.created_at_ms,
      "files" => Enum.map(e.files, & &1.key)
    }
  end

  # -- gated deletion ----------------------------------------------------------

  # Delete each planned prefix: recheck against LIVE ETS immediately before its
  # deletes (the plan may be seconds-to-minutes old), meta.json FIRST (a crashed
  # half-delete then reads as an incomplete artifact, never a stale-valid one),
  # then every LISTED key. Every key must pass the hard allowlist AND belong to
  # the prefix's own listing; a violation means a composition bug, so the whole
  # sweep aborts rather than trusting any further composed key. Any delete
  # failure also aborts the remainder (the next sweep re-plans from a fresh
  # listing).
  defp apply_deletes(state, _snapshot, plan) do
    Enum.reduce_while(plan, [], fn entry, deleted ->
      case recheck_live(state, entry) do
        :ok ->
          case delete_prefix(state, entry) do
            :ok ->
              Logger.info("embervm s3 warmth gc: DELETED prefix=#{entry.prefix} bytes=#{entry.bytes}")
              {:cont, [entry.prefix | deleted]}

            {:error, reason} ->
              abort(:delete_failed, "#{entry.prefix}: #{inspect(reason)}; aborting remaining deletes")
              {:halt, deleted}
          end

        {:blocked, reason} ->
          Logger.warning("embervm s3 warmth gc: recheck blocked #{entry.prefix} (#{reason}); skipping")
          {:cont, deleted}
      end
    end)
    |> Enum.reverse()
  end

  # The per-prefix recheck: re-read the live ETS truth sources for exactly the
  # conditions that can CHANGE between plan and delete (a relight re-desiring a
  # ref, a node re-reporting a bundle, a workload waking). Age and parse cannot
  # regress, so they are not re-evaluated.
  defp recheck_live(state, %{kind: :stateful} = entry) do
    facts = NodeCapacity.all(state.capacity_table)

    reported? =
      Enum.any?(facts, fn fact ->
        Enum.any?(Map.get(fact, :stateful_bundles, []) || [], &(&1.snapshot_ref == entry.ref))
      end)

    desired? =
      Enum.any?(StatefulStore.all(state.stateful_store), fn row ->
        not StatefulState.terminal?(row.state) and row.snapshot_ref == entry.ref
      end)

    tier1_still? = entry.tier != 1 or not workload_live_now?(state, entry.workload)

    cond do
      desired? -> {:blocked, "ref became desired"}
      reported? -> {:blocked, "ref became node-reported"}
      not tier1_still? -> {:blocked, "workload came alive since plan"}
      true -> :ok
    end
  end

  defp recheck_live(state, %{kind: :group} = entry) do
    facts = NodeCapacity.all(state.capacity_table)

    reported? =
      Enum.any?(facts, fn fact ->
        Enum.any?(Map.get(fact, :group_bundle_sets, []) || [], &(&1.set_id == entry.set_id))
      end)

    live? =
      Enum.any?(GroupStore.all(state.group_store), fn row ->
        not GroupState.terminal?(row.state) and (row.set_id == entry.set_id or row.instance_id == entry.group_instance_id)
      end)

    cond do
      live? -> {:blocked, "group became live/desired"}
      reported? -> {:blocked, "set became node-reported"}
      true -> :ok
    end
  end

  defp recheck_live(state, %{kind: kind} = entry) when kind in [:session, :serving, :session_workspace] do
    try do
      facts = NodeCapacity.all(state.capacity_table)
      session_rows = SessionStore.all(state.session_store)
      serving_rows = ServingStore.all(state.serving_store)

      cond do
        kind == :session and
            (Enum.any?(facts, fn f -> Enum.any?(Map.get(f, :session_snapshots, []) || [], &(&1.snapshot_ref == entry.ref)) end) or
               Enum.any?(session_rows, fn r -> session_actively_live?(r) and r.snapshot_ref == entry.ref end)) ->
          {:blocked, "session ref became referenced"}

        kind == :serving and
            (Enum.any?(facts, fn f -> Enum.any?(Map.get(f, :serving_snapshots, []) || [], &(&1.snapshot_ref == entry.ref)) end) or
               Enum.any?(serving_rows, fn r -> serving_actively_live?(r) and r.snapshot_ref == entry.ref end)) ->
          {:blocked, "serving ref became referenced"}

        kind == :session_workspace and
            (Enum.any?(facts, fn f -> Enum.any?(Map.get(f, :session_volumes, []) || [], &(&1.lineage_id == entry.lineage)) end) or
               Enum.any?(session_rows, fn r -> session_actively_live?(r) and r.lineage_id == entry.lineage end)) ->
          {:blocked, "lineage became referenced"}

        true -> :ok
      end
    rescue
      _ -> {:blocked, "registry_unreadable"}
    catch
      _, _ -> {:blocked, "registry_unreadable"}
    end
  end

  defp workload_live_now?(state, workload) do
    live_instance? =
      Enum.any?(StatefulStore.all(state.stateful_store), fn row ->
        not StatefulState.terminal?(row.state) and row.workload == workload
      end)

    live_instance? or state.volume_fun.(workload) != nil
  end

  defp delete_prefix(state, entry) do
    keys = Enum.map(entry.files, & &1.key)
    meta_key = entry.prefix <> "/meta.json"
    ordered = Enum.filter(keys, &(&1 == meta_key)) ++ Enum.reject(keys, &(&1 == meta_key))

    Enum.reduce_while(ordered, :ok, fn key, :ok ->
      cond do
        not allowlisted?(key) ->
          {:halt, {:error, {:allowlist_violation, key}}}

        not String.starts_with?(key, entry.prefix <> "/") ->
          {:halt, {:error, {:key_outside_prefix, key}}}

        true ->
          case state.s3.delete.(key) do
            :ok -> {:cont, :ok}
            {:error, reason} -> {:halt, {:error, {key, reason}}}
          end
      end
    end)
  end

  defp allowlisted?(key), do: Enum.any?(@allowlist, &String.starts_with?(key, &1))
end
