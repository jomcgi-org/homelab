defmodule Embervm.S3WarmthGcTest do
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, S3Client, S3WarmthGc}

  # Fixed clocks: the monotonic clock (uptime + NodeCapacity freshness) and the
  # wall clock (age gate against meta createdAtUnixMs) are injected so every
  # predicate boundary is asserted deterministically.
  @mono 1_000_000_000
  @wall 1_750_000_000_000
  @day 86_400_000

  # -- fakes -------------------------------------------------------------------

  # A minimal store stub answering :all with a fixed row list (drop-in for
  # StatefulStore/GroupStore, which the GC only reads via `Store.all/1`),
  # exactly as WarmthReaperTest's StoreStub.
  defmodule StoreStub do
    use GenServer
    def start_link(rows), do: GenServer.start_link(__MODULE__, rows)
    @impl true
    def init(rows), do: {:ok, rows}
    @impl true
    def handle_call(:all, _from, rows), do: {:reply, rows, rows}
  end

  # A store stub answering :all with a SEQUENCE of responses (last one repeats),
  # so a test can present one CP state to the sweep's planning snapshot and a
  # DIFFERENT one to the pre-delete recheck.
  defmodule SeqStoreStub do
    use GenServer
    def start_link(responses), do: GenServer.start_link(__MODULE__, responses)
    @impl true
    def init(responses), do: {:ok, responses}
    @impl true
    def handle_call(:all, _from, [last]), do: {:reply, last, [last]}
    def handle_call(:all, _from, [h | t]), do: {:reply, h, t}
  end

  defp start_store(rows) do
    {:ok, pid} = StoreStub.start_link(rows)
    pid
  end

  # The fake S3 seam: an Agent-held object map behind the four funs the GC
  # injects. Records every list/delete/put so tests assert exactly what was
  # touched, and in what order.
  defp new_s3(objects) do
    {:ok, agent} = Agent.start_link(fn -> %{objects: objects, deleted: [], puts: [], listed: []} end)

    funs = %{
      list: fn prefix ->
        Agent.update(agent, fn st -> %{st | listed: st.listed ++ [prefix]} end)

        entries =
          Agent.get(agent, & &1.objects)
          |> Enum.filter(fn {key, _} -> String.starts_with?(key, prefix) end)
          |> Enum.map(fn {key, {size, lm, _body}} -> %{key: key, size: size, last_modified_ms: lm} end)
          |> Enum.sort_by(& &1.key)

        {:ok, entries}
      end,
      get: fn key ->
        case Agent.get(agent, &Map.get(&1.objects, key)) do
          nil -> {:error, :not_found}
          {_size, _lm, body} -> {:ok, body}
        end
      end,
      delete: fn key ->
        Agent.update(agent, fn st ->
          %{st | objects: Map.delete(st.objects, key), deleted: st.deleted ++ [key]}
        end)

        :ok
      end,
      put: fn key, body ->
        Agent.update(agent, fn st -> %{st | puts: st.puts ++ [{key, body}]} end)
        :ok
      end
    }

    {agent, funs}
  end

  defp deleted(agent), do: Agent.get(agent, & &1.deleted)
  defp puts(agent), do: Agent.get(agent, & &1.puts)

  # One complete artifact under `prefix`: meta.json (carrying createdAtUnixMs)
  # plus a payload file, as {key => {size, last_modified_ms, body}}.
  defp artifact(prefix, created_at_ms, payload_bytes \\ 1_000) do
    meta = :json.encode(%{"createdAtUnixMs" => created_at_ms}) |> IO.iodata_to_binary()

    %{
      (prefix <> "/meta.json") => {byte_size(meta), created_at_ms, meta},
      (prefix <> "/memfile") => {payload_bytes, created_at_ms, "payload"}
    }
  end

  defp new_cap_table do
    table = :"s3gc_cap_#{System.unique_integer([:positive])}"
    NodeCapacity.create(table)
    table
  end

  defp put_node_fact(table, node_id, stateful_bundles, group_bundle_sets, updated_at \\ @mono, opts \\ []) do
    NodeCapacity.put(table, {node_id, "pod"}, %{
      node_id: node_id,
      instance_id: "#{node_id}/pod",
      stateful_bundles: stateful_bundles,
      group_bundle_sets: group_bundle_sets,
      session_snapshots: Keyword.get(opts, :session_snapshots, []),
      serving_snapshots: Keyword.get(opts, :serving_snapshots, []),
      session_volumes: Keyword.get(opts, :session_volumes, []),
      updated_at: updated_at
    })
  end

  defp stateful_row(state, workload, ref) do
    %{instance_id: "st-#{ref}", workload: workload, state: state, snapshot_ref: ref, node_id: "node-4"}
  end

  defp group_row(state, instance_id, set_id) do
    %{instance_id: instance_id, workload: "web", state: state, set_id: set_id, node_id: "node-4"}
  end

  defp session_row(state, workload, lineage_id, snapshot_ref, opts \\ []) do
    %{session_id: "session-#{snapshot_ref}", workload: workload, lineage_id: lineage_id, snapshot_ref: snapshot_ref, state: state}
    |> Map.put(:expires_at, Keyword.get(opts, :expires_at))
  end

  defp serving_row(state, workload, snapshot_ref) do
    %{instance_id: "serving-#{snapshot_ref}", workload: workload, snapshot_ref: snapshot_ref, state: state}
  end

  defp start_gc(s3_funs, opts) do
    {:ok, pid} =
      S3WarmthGc.start_link(
        Keyword.merge(
          [
            name: nil,
            s3: s3_funs,
            sweep_interval_ms: 0,
            min_uptime_ms: 0,
            expected_nodes: ["node-4"],
            session_store: start_store([]),
            serving_store: start_store([]),
            clock: fn -> @mono end,
            wall_clock: fn -> @wall end
          ],
          opts
        )
      )

    pid
  end

  # A one-orphan happy-path fixture: a dead workload's 30-day-old bundle in S3,
  # a CP that tracks only terminal history for it, and a fresh node-4 fact
  # reporting nothing. Every predicate test perturbs exactly one condition.
  defp orphan_fixture(objects_extra \\ %{}) do
    prefix = "stateful/amd/dead-wl/state-orphan1"
    objects = Map.merge(artifact(prefix, @wall - 30 * @day), objects_extra)
    {agent, s3} = new_s3(objects)
    table = new_cap_table()
    put_node_fact(table, "node-4", [], [])
    stateful = start_store([stateful_row(:destroyed, "dead-wl", "state-orphan1")])
    group = start_store([])

    base_opts = [
      capacity_table: table,
      stateful_store: stateful,
      group_store: group,
      volume_fun: fn _wl -> nil end
    ]

    %{prefix: prefix, agent: agent, s3: s3, table: table, base_opts: base_opts}
  end

  # -- destructive happy path --------------------------------------------------

  describe "gated deletion" do
    test "deletes a proven Tier-1 orphan with the gate ON, meta.json first, only listed keys" do
      %{prefix: prefix, agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()

      gc = start_gc(s3, base_opts ++ [enabled: true])
      assert {:ok, %{deleted: [^prefix], plan: [entry]}} = S3WarmthGc.sweep_now(gc)
      assert entry.tier == 1

      # meta.json is deleted FIRST (a crashed half-delete reads as incomplete,
      # never stale-valid), then the listed payload key, and nothing else.
      assert deleted(agent) == [prefix <> "/meta.json", prefix <> "/memfile"]
      # The audit manifest was persisted BEFORE the deletes, outside the
      # delete allowlist.
      assert [{"gc-manifests/" <> _, manifest_body}] = puts(agent)
      assert %{"mode" => "armed", "plan" => [%{"prefix" => ^prefix}]} = :json.decode(manifest_body)
    end

    test "only lists the five allowlisted prefixes, never base/ or volume/" do
      %{agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()
      gc = start_gc(s3, base_opts ++ [enabled: true])
      assert {:ok, _} = S3WarmthGc.sweep_now(gc)
      assert Agent.get(agent, & &1.listed) == ["stateful/", "session/", "serving/", "session-workspace/", "group_set/"]
    end
  end

  # -- each predicate condition independently blocks ---------------------------

  describe "fail-closed predicate" do
    test "uses the eight-hour stateful TTL while session and serving use seven days" do
      old_stateful = "stateful/amd/wl/state-9h"
      old_session = "session/amd/wl/session-8d"
      old_serving = "serving/amd/wl/serving-8d"
      young_session = "session/amd/young/session-2d"
      young_serving = "serving/amd/young/serving-2d"
      old_workspace = "session-workspace/wl/lineage-old"
      old_group = "group_set/wl/group-8d"

      objects =
        artifact(old_stateful, @wall - 9 * 60 * 60 * 1000)
        |> Map.merge(artifact(old_session, @wall - 8 * @day))
        |> Map.merge(artifact(old_serving, @wall - 8 * @day))
        |> Map.merge(artifact(young_session, @wall - 2 * @day))
        |> Map.merge(artifact(young_serving, @wall - 2 * @day))
        |> Map.merge(artifact(old_workspace, @wall - 8 * @day))
        |> Map.merge(artifact(old_group, @wall - 8 * @day))

      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc =
        start_gc(s3,
          enabled: false,
          capacity_table: table,
          stateful_store: start_store([stateful_row(:destroyed, "wl", "old")]),
          group_store: start_store([group_row(:destroyed, "group", "old")]),
          session_store: start_store([session_row(:destroyed, "other", "other", nil)]),
          serving_store: start_store([serving_row(:destroyed, "other", "other")]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, result} = S3WarmthGc.sweep_now(gc)
      assert Enum.any?(result.plan, &(&1.prefix == old_stateful))
      assert Enum.any?(result.plan, &(&1.prefix == old_workspace))
      assert Enum.any?(result.plan, &(&1.prefix == old_session))
      assert Enum.any?(result.plan, &(&1.prefix == old_serving))
      assert Enum.any?(result.held, &(&1.prefix == young_session and &1.reason == "younger_than_age_floor"))
      assert Enum.any?(result.held, &(&1.prefix == young_serving and &1.reason == "younger_than_age_floor"))
      assert deleted(agent) == []
    end

    test "a parked session workspace lineage is eligible after the TTL" do
      prefix = "session-workspace/wl/lineage-parked"
      {agent, s3} = new_s3(artifact(prefix, @wall - 8 * @day))
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
          group_store: start_store([]),
          session_store: start_store([session_row(:parked, "wl", "lineage-parked", nil)]),
          serving_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, %{plan: [%{prefix: ^prefix}], held: []}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == [prefix <> "/meta.json", prefix <> "/memfile"]
    end

    test "banked session and banked serving artifacts are eligible after the TTL" do
      session_prefix = "session/amd/wl/session-live"
      serving_prefix = "serving/amd/wl/serving-live"

      {agent, s3} =
        new_s3(
          artifact(session_prefix, @wall - 8 * @day)
          |> Map.merge(artifact(serving_prefix, @wall - 8 * @day))
        )

      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
          group_store: start_store([]),
          session_store: start_store([session_row(:banked, "wl", "lineage", "session-live")]),
          serving_store: start_store([serving_row(:banked, "wl", "serving-live")]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, result} = S3WarmthGc.sweep_now(gc)
      assert Enum.any?(result.plan, &(&1.prefix == session_prefix))
      assert Enum.any?(result.plan, &(&1.prefix == serving_prefix))
      assert length(deleted(agent)) == 4
    end

    test "parked session with a future expiry is held, and a past expiry is deleted" do
      prefix = "session/amd/wl/session-expiry"
      objects = artifact(prefix, @wall - 8 * @day)
      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      opts = [
        enabled: true,
        capacity_table: table,
        stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
        group_store: start_store([]),
        serving_store: start_store([]),
        volume_fun: fn _ -> nil end
      ]

      gc = start_gc(s3, opts ++ [session_store: start_store([session_row(:parked, "wl", "lineage", "session-expiry", expires_at: @wall + @day)])])
      assert {:ok, %{plan: [], held: [%{prefix: ^prefix, reason: "session_not_expired"}], deleted: []}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []

      {agent, s3} = new_s3(objects)
      gc = start_gc(s3, opts ++ [session_store: start_store([session_row(:parked, "wl", "lineage", "session-expiry", expires_at: @wall - 1)])])
      assert {:ok, %{plan: [%{prefix: ^prefix}], deleted: [^prefix]}} = S3WarmthGc.sweep_now(gc)
      assert length(deleted(agent)) == 2
    end

    test "parked workspace lineage is held until expiry, then deleted" do
      prefix = "session-workspace/wl/lineage-expiry"
      objects = artifact(prefix, @wall - 8 * @day)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])
      common = [
        enabled: true,
        capacity_table: table,
        stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
        group_store: start_store([]),
        serving_store: start_store([]),
        volume_fun: fn _ -> nil end
      ]

      {agent, s3} = new_s3(objects)
      gc = start_gc(s3, common ++ [session_store: start_store([session_row(:parked, "wl", "lineage-expiry", nil, expires_at: @wall + @day)])])
      assert {:ok, %{plan: [], held: [%{prefix: ^prefix, reason: "session_not_expired"}], deleted: []}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []

      {agent, s3} = new_s3(objects)
      gc = start_gc(s3, common ++ [session_store: start_store([session_row(:parked, "wl", "lineage-expiry", nil, expires_at: @wall - 1)])])
      assert {:ok, %{plan: [%{prefix: ^prefix}], deleted: [^prefix]}} = S3WarmthGc.sweep_now(gc)
      assert length(deleted(agent)) == 2
    end

    test "actively live session and serving refs are held" do
      session_prefix = "session/amd/wl/session-live-ref"
      serving_prefix = "serving/amd/wl/serving-live-ref"
      {agent, s3} = new_s3(artifact(session_prefix, @wall - 8 * @day) |> Map.merge(artifact(serving_prefix, @wall - 8 * @day)))
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc = start_gc(s3,
        enabled: true,
        capacity_table: table,
        stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
        group_store: start_store([]),
        session_store: start_store([session_row(:running, "wl", "lineage", "session-live-ref")]),
        serving_store: start_store([serving_row(:published, "wl", "serving-live-ref")]),
        volume_fun: fn _ -> nil end
      )

      assert {:ok, result} = S3WarmthGc.sweep_now(gc)
      assert Enum.all?([session_prefix, serving_prefix], fn prefix -> Enum.any?(result.held, &(&1.prefix == prefix and &1.reason == "referenced_ref")) end)
      assert result.plan == []
      assert deleted(agent) == []
    end

    test "actively live session lineage is held" do
      prefix = "session-workspace/wl/lineage-live"
      {agent, s3} = new_s3(artifact(prefix, @wall - 8 * @day))
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc = start_gc(s3,
        enabled: true,
        capacity_table: table,
        stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
        group_store: start_store([]),
        session_store: start_store([session_row(:running, "wl", "lineage-live", nil)]),
        serving_store: start_store([]),
        volume_fun: fn _ -> nil end
      )

      assert {:ok, %{plan: [], held: [%{prefix: ^prefix, reason: "lineage_referenced"}], deleted: []}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "node-reported session, serving, and workspace refs are held" do
      refs = %{session: "session-reported", serving: "serving-reported", lineage: "lineage-reported"}
      objects =
        artifact("session/amd/wl/" <> refs.session, @wall - 8 * @day)
        |> Map.merge(artifact("serving/amd/wl/" <> refs.serving, @wall - 8 * @day))
        |> Map.merge(artifact("session-workspace/wl/" <> refs.lineage, @wall - 8 * @day))
      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [], @mono, session_snapshots: [%{snapshot_ref: refs.session}], serving_snapshots: [%{snapshot_ref: refs.serving}], session_volumes: [%{lineage_id: refs.lineage}])

      gc = start_gc(s3,
        enabled: true,
        capacity_table: table,
        stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
        group_store: start_store([]),
        session_store: start_store([session_row(:destroyed, "wl", "other", nil)]),
        serving_store: start_store([serving_row(:destroyed, "wl", "other")]),
        volume_fun: fn _ -> nil end
      )

      assert {:ok, result} = S3WarmthGc.sweep_now(gc)
      assert Enum.all?(result.held, &(&1.reason == "node_reported"))
      assert length(result.held) == 3
      assert result.plan == []
      assert deleted(agent) == []
    end

    test "a desired (non-terminal) ref is held, not deleted" do
      %{prefix: prefix, agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()
      # Same ref, but the CP still tracks a non-terminal (banked) instance on it.
      stateful = start_store([stateful_row(:banked, "dead-wl", "state-orphan1")])
      opts = Keyword.put(base_opts, :stateful_store, stateful)

      gc = start_gc(s3, opts ++ [enabled: true])
      assert {:ok, %{deleted: [], plan: [], held: held}} = S3WarmthGc.sweep_now(gc)
      assert [%{prefix: ^prefix, reason: "desired_ref"}] = held
      assert deleted(agent) == []
    end

    test "a node-reported ref is held, not deleted" do
      %{prefix: prefix, agent: agent, s3: s3, table: table, base_opts: base_opts} = orphan_fixture()
      # node-4 reports the bundle on disk (so the WarmthReaper owns it, not us).
      put_node_fact(table, "node-4", [%{snapshot_ref: "state-orphan1", workload: "", size_bytes: 1}], [])

      gc = start_gc(s3, base_opts ++ [enabled: true])
      assert {:ok, %{deleted: [], held: [%{prefix: ^prefix, reason: "node_reported"}]}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "a prefix younger than the age floor is held" do
      prefix = "stateful/amd/dead-wl/state-young"
      # 2 hours old: inside the stateful 8h TTL (the old fixture's 2 days was
      # "young" only against the retired 7-day floor).
      {agent, s3} = new_s3(artifact(prefix, @wall - 2 * 60 * 60 * 1000))
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])
      stateful = start_store([stateful_row(:destroyed, "dead-wl", "state-young")])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: stateful,
          group_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, %{deleted: [], held: [%{reason: "younger_than_age_floor"}]}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "Tier 2: a live workload keeps its newest ref, older predecessors trim" do
      objects =
        Map.merge(
          artifact("stateful/amd/live-wl/state-old", @wall - 40 * @day),
          Map.merge(
            artifact("stateful/amd/live-wl/state-mid", @wall - 30 * @day),
            artifact("stateful/amd/live-wl/state-new", @wall - 20 * @day)
          )
        )

      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])
      # The workload is LIVE (a running instance on a fourth, current ref).
      stateful = start_store([stateful_row(:serving, "live-wl", "state-current")])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: stateful,
          group_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, result} = S3WarmthGc.sweep_now(gc)
      # Only the newest ref is held; both older predecessors are eligible.
      assert result.deleted == ["stateful/amd/live-wl/state-old", "stateful/amd/live-wl/state-mid"]
      assert Enum.map(result.plan, & &1.tier) == [2, 2]

      held_reasons = Enum.map(result.held, & &1.reason) |> Enum.sort()
      assert held_reasons == ["tier2_protected_newest"]
      refute Enum.any?(deleted(agent), &String.contains?(&1, "state-new"))
    end

    test "a volume row alone makes the workload live (Tier 2 protection, not Tier 1 reclaim)" do
      %{agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()
      # No non-terminal instance, but the volume ledger holds a row: the sole
      # prefix in the namespace lands inside the newest-1 protection.
      opts = Keyword.put(base_opts, :volume_fun, fn "dead-wl" -> %{workload: "dead-wl"} end)

      gc = start_gc(s3, opts ++ [enabled: true])
      assert {:ok, %{deleted: [], held: [%{reason: "tier2_protected_newest"}]}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "an ambiguous key parse (workload named like a vendor) is skipped whole" do
      # Legacy-depth key whose segment 2 IS a vendor token: indistinguishable
      # from a vendored key, so it must never compose into a deletable prefix.
      objects = %{
        "stateful/amd/state-ambig/memfile" => {100, @wall - 30 * @day, "x"}
      }

      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])
      stateful = start_store([stateful_row(:destroyed, "whatever", "state-x")])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: stateful,
          group_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, %{plan: [], deleted: [], ambiguous: ["stateful/amd/state-ambig/memfile"]}} =
               S3WarmthGc.sweep_now(gc)

      assert deleted(agent) == []
    end

    test "a five-segment workspace key is ambiguous and does not crash the sweep" do
      key = "session-workspace/amd/wl/lineage-ambig/meta.json"
      {agent, s3} = new_s3(%{key => {100, @wall - 30 * @day, "x"}})
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
          group_store: start_store([]),
          session_store: start_store([session_row(:destroyed, "wl", "none", nil)]),
          serving_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, %{plan: [], deleted: [], ambiguous: [^key], held: []}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end
  end

  # -- sweep-level aborts ------------------------------------------------------

  describe "aborts" do
    test "a missing expected node aborts the whole sweep before any list or delete" do
      %{agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()

      gc = start_gc(s3, base_opts ++ [enabled: true, expected_nodes: ["node-4", "node-9"]])
      assert {:error, :fleet_stale} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
      assert puts(agent) == []
    end

    test "a STALE expected node (present but not fresh) aborts the sweep" do
      %{agent: agent, s3: s3, table: table, base_opts: base_opts} = orphan_fixture()
      # node-4 present but last updated 10 minutes of monotonic time ago.
      put_node_fact(table, "node-4", [], [], @mono - 600_000)

      gc = start_gc(s3, base_opts ++ [enabled: true, freshness_window_ms: 120_000])
      assert {:error, :fleet_stale} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "an empty expected-node list always aborts (no fleet contract, no sweep)" do
      %{agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()
      gc = start_gc(s3, base_opts ++ [enabled: true, expected_nodes: []])
      assert {:error, :no_expected_nodes} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "empty CP state aborts: S3 objects with a store tracking nothing is never all-orphaned" do
      %{agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()
      opts = Keyword.put(base_opts, :stateful_store, start_store([]))

      gc = start_gc(s3, opts ++ [enabled: true])
      assert {:error, :empty_cp_state} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "empty session, serving, and workspace CP stores abort when S3 has those kinds" do
      objects =
        artifact("session/amd/wl/session-empty", @wall - 8 * @day)
        |> Map.merge(artifact("serving/amd/wl/serving-empty", @wall - 8 * @day))
        |> Map.merge(artifact("session-workspace/wl/lineage-empty", @wall - 8 * @day))
      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
          group_store: start_store([]),
          session_store: start_store([]),
          serving_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:error, :empty_cp_state} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "an empty serving store aborts independently of the session branch" do
      objects = artifact("serving/amd/wl/serving-only", @wall - 8 * @day)
      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: start_store([stateful_row(:destroyed, "wl", "none")]),
          group_store: start_store([]),
          session_store: start_store([session_row(:destroyed, "wl", "lineage", "some-ref")]),
          serving_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:error, :empty_cp_state} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "insufficient CP uptime aborts (stores may still be rebuilding)" do
      %{agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()
      gc = start_gc(s3, base_opts ++ [enabled: true, min_uptime_ms: 60_000])
      assert {:error, :cp_too_young} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "any list failure aborts the whole sweep (a partial listing deletes on absence)" do
      %{s3: s3, agent: agent, base_opts: base_opts} = orphan_fixture()
      failing = %{s3 | list: fn _prefix -> {:error, :seaweed_500} end}

      gc = start_gc(failing, base_opts ++ [enabled: true])
      assert {:error, :list_failed} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end

    test "a manifest persist failure aborts BEFORE any delete" do
      %{s3: s3, agent: agent, base_opts: base_opts} = orphan_fixture()
      no_put = %{s3 | put: fn _key, _body -> {:error, :seaweed_500} end}

      gc = start_gc(no_put, base_opts ++ [enabled: true])
      assert {:error, :manifest_persist_failed} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end
  end

  # -- pre-delete recheck ------------------------------------------------------

  test "the per-prefix recheck blocks a delete when the ref becomes desired after planning" do
    %{prefix: _prefix, agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()

    # First :all (the planning snapshot): only terminal history, so the prefix
    # plans eligible. Second :all (the pre-delete recheck): the instance has
    # been relit non-terminal on the same ref, so the delete must be blocked.
    {:ok, seq} =
      SeqStoreStub.start_link([
        [stateful_row(:destroyed, "dead-wl", "state-orphan1")],
        [stateful_row(:serving, "dead-wl", "state-orphan1")]
      ])

    opts = Keyword.put(base_opts, :stateful_store, seq)
    gc = start_gc(s3, opts ++ [enabled: true])

    assert {:ok, %{plan: [_], deleted: []}} = S3WarmthGc.sweep_now(gc)
    assert deleted(agent) == []
  end

  # -- dry run -----------------------------------------------------------------

  describe "dry run (gate off, the shipped default)" do
    test "emits the plan and persists a dry_run manifest without deleting anything" do
      %{prefix: prefix, agent: agent, s3: s3, base_opts: base_opts} = orphan_fixture()

      gc = start_gc(s3, base_opts)
      assert {:ok, %{plan: [entry], deleted: []}} = S3WarmthGc.sweep_now(gc)
      assert entry.prefix == prefix
      assert deleted(agent) == []

      assert [{"gc-manifests/" <> _, body}] = puts(agent)
      assert %{"mode" => "dry_run", "plan" => [%{"prefix" => ^prefix, "tier" => 1}]} = :json.decode(body)
    end
  end

  # -- caps --------------------------------------------------------------------

  describe "caps" do
    test "max_prefixes bounds one sweep, oldest Tier-1 first" do
      objects =
        Map.merge(
          artifact("stateful/amd/dead-a/state-a", @wall - 40 * @day),
          artifact("stateful/amd/dead-b/state-b", @wall - 20 * @day)
        )

      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      stateful =
        start_store([
          stateful_row(:destroyed, "dead-a", "state-a"),
          stateful_row(:destroyed, "dead-b", "state-b")
        ])

      gc =
        start_gc(s3,
          enabled: true,
          max_prefixes: 1,
          capacity_table: table,
          stateful_store: stateful,
          group_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, %{deleted: ["stateful/amd/dead-a/state-a"], eligible: eligible}} = S3WarmthGc.sweep_now(gc)
      assert length(eligible) == 2
      refute Enum.any?(deleted(agent), &String.contains?(&1, "dead-b"))
    end

    test "max_bytes bounds one sweep" do
      objects =
        Map.merge(
          artifact("stateful/amd/dead-a/state-a", @wall - 40 * @day, 5_000),
          artifact("stateful/amd/dead-b/state-b", @wall - 20 * @day, 5_000)
        )

      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      stateful =
        start_store([
          stateful_row(:destroyed, "dead-a", "state-a"),
          stateful_row(:destroyed, "dead-b", "state-b")
        ])

      gc =
        start_gc(s3,
          enabled: true,
          max_bytes: 6_000,
          capacity_table: table,
          stateful_store: stateful,
          group_store: start_store([]),
          volume_fun: fn _ -> nil end
        )

      assert {:ok, %{deleted: ["stateful/amd/dead-a/state-a"]}} = S3WarmthGc.sweep_now(gc)
      refute Enum.any?(deleted(agent), &String.contains?(&1, "dead-b"))
    end
  end

  # -- groups ------------------------------------------------------------------

  describe "group sets" do
    test "a terminal group's old legacy-layout set deletes; a live group's set is held" do
      objects =
        Map.merge(
          artifact("group_set/grp-dead/set-1", @wall - 30 * @day),
          artifact("group_set/grp-live/set-2", @wall - 30 * @day)
        )

      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])

      group =
        start_store([
          group_row(:destroyed, "grp-dead", nil),
          group_row(:running, "grp-live", nil)
        ])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: start_store([]),
          group_store: group,
          volume_fun: fn _ -> nil end
        )

      assert {:ok, result} = S3WarmthGc.sweep_now(gc)
      assert result.deleted == ["group_set/grp-dead/set-1"]
      assert [%{prefix: "group_set/grp-live/set-2", reason: "group_instance_live"}] = result.held
      refute Enum.any?(deleted(agent), &String.contains?(&1, "grp-live"))
    end

    test "a desired set_id is held even for a vendored-layout key" do
      objects = artifact("group_set/amd/grp-1/set-9", @wall - 30 * @day)
      {agent, s3} = new_s3(objects)
      table = new_cap_table()
      put_node_fact(table, "node-4", [], [])
      group = start_store([group_row(:banked, "grp-1", "set-9")])

      gc =
        start_gc(s3,
          enabled: true,
          capacity_table: table,
          stateful_store: start_store([]),
          group_store: group,
          volume_fun: fn _ -> nil end
        )

      assert {:ok, %{deleted: [], held: [%{reason: "desired_set"}]}} = S3WarmthGc.sweep_now(gc)
      assert deleted(agent) == []
    end
  end

  # -- S3 client parsing -------------------------------------------------------

  describe "S3Client.parse_list_response/1" do
    # Captured live from the deployed SeaweedFS gateway (2026-07-22): truncated
    # page with NextContinuationToken = the page's last key.
    @seaweed_page """
    <?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>embervm</Name><Prefix>stateful/</Prefix><MaxKeys>2</MaxKeys><IsTruncated>true</IsTruncated><Contents><Key>stateful/amd/demo-postgres/state-1995f0f7dede0cdc/gen</Key><ETag>&#34;00000000000000000000000000000001&#34;</ETag><Size>3</Size><Owner><ID></ID></Owner><StorageClass>STANDARD</StorageClass><LastModified>2026-07-22T02:24:53Z</LastModified></Contents><Contents><Key>stateful/amd/demo-postgres/state-1995f0f7dede0cdc/memfile</Key><ETag>&#34;00000000000000000000000000000002&#34;</ETag><Size>536870912</Size><Owner><ID></ID></Owner><StorageClass>STANDARD</StorageClass><LastModified>2026-07-22T02:25:20Z</LastModified></Contents><NextContinuationToken>stateful/amd/demo-postgres/state-1995f0f7dede0cdc/memfile</NextContinuationToken><KeyCount>2</KeyCount></ListBucketResult>
    """

    test "parses keys, sizes, Last-Modified, truncation, and the continuation token" do
      assert {:ok, [gen, memfile], true, token} = S3Client.parse_list_response(@seaweed_page)
      assert gen.key == "stateful/amd/demo-postgres/state-1995f0f7dede0cdc/gen"
      assert gen.size == 3
      assert gen.last_modified_ms > 0
      assert memfile.size == 536_870_912
      assert token == "stateful/amd/demo-postgres/state-1995f0f7dede0cdc/memfile"
    end

    test "a terminal page reports not-truncated with no token" do
      body =
        ~s(<?xml version="1.0"?><ListBucketResult><Prefix>group_set/</Prefix><IsTruncated>false</IsTruncated><KeyCount>0</KeyCount></ListBucketResult>)

      assert {:ok, [], false, nil} = S3Client.parse_list_response(body)
    end

    test "a non-list body is an error, never an empty success" do
      assert {:error, {:not_a_list_response, _}} = S3Client.parse_list_response("<html>gateway error</html>")
    end
  end
end
