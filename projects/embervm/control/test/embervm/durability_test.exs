defmodule Embervm.DurabilityTest do
  use ExUnit.Case, async: true

  alias Embervm.{Durability, NodeCapacity}

  # Fixed clocks injected into every evaluation so each boundary is asserted
  # deterministically (same idiom as S3WarmthGcTest).
  @mono 1_000_000_000
  @wall 1_756_000_000_000
  @hour 3_600_000
  @day 86_400_000

  defp opts(extra \\ []) do
    Keyword.merge(
      [
        now_mono: @mono,
        now_wall: @wall,
        freshness_window_ms: 120_000,
        streak_threshold: 10,
        sweep_interval_ms: @hour,
        # A fresh manifest by default, so overall `ok` tracks tier 1 in the
        # tier-1 tests; tier-2 tests override explicitly.
        s3: manifest_s3(@hour)
      ],
      extra
    )
  end

  defp new_cap_table do
    table = :"dur_cap_#{System.unique_integer([:positive])}"
    NodeCapacity.create(table)
    table
  end

  # A node fact reporting one artifact per tracked kind. Every id is unique so
  # counts are unambiguous; `exported?` stamps every artifact at once.
  defp node_fact(node_id, exported?, updated_at \\ @mono) do
    %{
      node_id: node_id,
      updated_at: updated_at,
      session_snapshots: [%{snapshot_ref: "sref-#{node_id}", session_id: "s", workload: "wl", exported: exported?}],
      serving_snapshots: [%{snapshot_ref: "vref-#{node_id}", workload: "wl", exported: exported?}],
      stateful_bundles: [%{snapshot_ref: "bref-#{node_id}", workload: "wl", exported: exported?}],
      group_bundle_sets: [%{set_id: "set-#{node_id}", group_instance_id: "g", exported: exported?}],
      workloads: %{
        "wl" => %{snapshot_ref: "base-#{node_id}", base_state: :BASE_BUILD_STATE_READY, exported: exported?}
      }
    }
  end

  # The tier-2 seam: `%{list: fun}` answering with a listing whose newest
  # object sits `age_ms` before the fixed wall clock.
  defp manifest_s3(age_ms) do
    listing = [%{key: "gc-manifests/x.json", size: 10, last_modified_ms: @wall - age_ms}]
    %{list: fn _prefix -> {:ok, listing} end}
  end

  defp listing_s3(result), do: %{list: fn _prefix -> result end}

  # -- Tier 1 ------------------------------------------------------------------

  describe "tier 1: export failure streaks" do
    test "fresh signals stay healthy: everything exported and fleet fresh reads ok" do
      report = Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts())

      assert report.ok
      assert report.tier1.ok
      assert report.tier1.verdict == :ok
      # Every kind's streak is at zero on a clean read.
      assert report.tier1.streaks == %{base: 0, group_set: 0, serving: 0, session: 0, stateful: 0}
      assert report.tier1.failing_kinds == []
      assert report.tier1.fresh_nodes == ["node-1"]
      assert report.tier1.missing_nodes == []
    end

    test "a pending kind crossing the threshold flips unhealthy immediately" do
      fact = node_fact("node-1", false)

      # Below the threshold: still ok (a fresh bank exports within minutes).
      {report, streaks} = run_rounds([fact], ["node-1"], %{}, 9)
      assert report.ok
      assert report.tier1.verdict == :ok

      # The threshold round latches tier 1 NOW (minutes, not hours). The
      # fixture has every kind pending, so every kind fires together.
      {report, _} = run_rounds([fact], ["node-1"], streaks, 1)
      refute report.ok
      refute report.tier1.ok
      assert report.tier1.verdict == :export_failure_streak
      assert report.tier1.failing_kinds == [:base, :group_set, :serving, :session, :stateful]
      assert get_in(report, [:tier1, :streaks, :session]) == 10
    end

    test "the failing kinds recover once their store copies confirm" do
      fact = node_fact("node-1", false)

      {_, streaks} = run_rounds([fact], ["node-1"], %{}, 12)
      assert streaks[:session] >= 10

      # Exports succeed again: the very next round clears the streak.
      recovered = Durability.evaluate([node_fact("node-1", true)], ["node-1"], streaks, opts())
      assert recovered.ok
      assert recovered.tier1.verdict == :ok
      assert recovered.tier1.failing_kinds == []
      assert get_in(recovered, [:tier1, :streaks, :session]) == 0
    end

    test "kinds are independent: a recovered kind does not clear a still-failing one" do
      fact = node_fact("node-1", false)

      {_, streaks} = run_rounds([fact], ["node-1"], %{}, 11)

      # Everything confirms EXCEPT the stateful bundle.
      partial =
        node_fact("node-1", true)
        |> Map.put(:stateful_bundles, [%{snapshot_ref: "bref-node-1", workload: "wl", exported: false}])

      report = Durability.evaluate([partial], ["node-1"], streaks, opts())
      refute report.ok
      assert report.tier1.verdict == :export_failure_streak
      assert report.tier1.failing_kinds == [:stateful]
      assert get_in(report, [:tier1, :streaks, :session]) == 0
    end

    test "an artifact leaving the facts resets its kind instead of firing forever" do
      fact = node_fact("node-1", false)
      {_, streaks} = run_rounds([fact], ["node-1"], %{}, 5)

      # The banked snapshot was evicted/retired: nothing pending anymore.
      empty =
        node_fact("node-1", true)
        |> Map.put(:session_snapshots, [])

      report = Durability.evaluate([empty], ["node-1"], streaks, opts())
      assert get_in(report, [:tier1, :streaks, :session]) == 0
    end
  end

  describe "tier 1 vacuous-green guards" do
    test "no expected nodes configured reads unknown, never green" do
      report = Durability.evaluate([node_fact("node-1", true)], [], %{}, opts())

      refute report.ok
      refute report.tier1.ok
      assert report.tier1.verdict == :unknown
    end

    test "no expected node reporting fresh reads unknown, never green" do
      report = Durability.evaluate([], ["node-1"], %{}, opts())

      refute report.ok
      assert report.tier1.verdict == :unknown
      assert report.tier1.missing_nodes == ["node-1"]
    end

    test "a stale expected node with nothing pending reads unknown, not green" do
      stale = node_fact("node-1", true, @mono - 120_001)
      report = Durability.evaluate([stale], ["node-1"], %{}, opts())

      refute report.ok
      assert report.tier1.verdict == :unknown
      assert report.tier1.missing_nodes == ["node-1"]
    end

    test "a firing streak stands even while another expected node is missing" do
      fact = node_fact("node-1", false)
      {_, streaks} = run_rounds([fact], ["node-1"], %{}, 10)

      report = Durability.evaluate([fact], ["node-1", "node-2"], streaks, opts())
      refute report.ok
      # The failure evidence is real regardless of the silent node.
      assert report.tier1.verdict == :export_failure_streak
      assert report.tier1.missing_nodes == ["node-2"]
    end

    test "a streak survives a transient unknown round without resetting" do
      fact = node_fact("node-1", false)
      {_, streaks} = run_rounds([fact], ["node-1"], %{}, 6)

      # A fleet-stale round (all nodes missing) keeps the counters.
      report = Durability.evaluate([], ["node-1"], streaks, opts())
      assert report.tier1.verdict == :unknown
      assert get_in(report, [:tier1, :streaks, :session]) == streaks[:session]
    end

    test "a LATCHED streak stays latched when the whole fleet goes silent" do
      # Only the session bundle is pending; everything else confirms.
      fact =
        node_fact("node-1", true)
        |> Map.put(:session_snapshots, [%{snapshot_ref: "sref-node-1", session_id: "s", workload: "wl", exported: false}])

      {_, streaks} = run_rounds([fact], ["node-1"], %{}, 10)

      # All witnesses disappear: the latch must not heal on missing data.
      report = Durability.evaluate([], ["node-1"], streaks, opts())
      refute report.ok
      assert report.tier1.verdict == :export_failure_streak
      assert report.tier1.failing_kinds == [:session]
    end
  end

  # -- Tier 2 ------------------------------------------------------------------

  describe "tier 2: gc sweep stall" do
    test "a fresh manifest stays healthy" do
      report = Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: manifest_s3(@hour)))

      assert report.tier2.ok
      assert report.tier2.verdict == :ok
      assert report.tier2.newest_manifest_age_ms == @hour
      # Bound is 24h + sweep interval.
      assert report.tier2.stall_bound_ms == @day + @hour
    end

    test "a stalled sweep older than the bound degrades; exactly at the bound does not" do
      bound = @day + @hour

      at_bound = Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: manifest_s3(bound)))
      assert at_bound.tier2.verdict == :ok
      assert at_bound.ok

      past_bound = Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: manifest_s3(bound + 1)))
      refute past_bound.tier2.ok
      assert past_bound.tier2.verdict == :gc_sweep_stalled
      refute past_bound.ok
    end

    test "the newest object decides: an old stall heals when a fresh manifest lands" do
      entries = [
        %{key: "gc-manifests/old.json", size: 10, last_modified_ms: @wall - 3 * @day},
        %{key: "gc-manifests/new.json", size: 10, last_modified_ms: @wall - 2 * @hour}
      ]

      report =
        Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: listing_s3({:ok, entries})))

      assert report.tier2.ok
      assert report.tier2.newest_manifest_age_ms == 2 * @hour
    end

    test "an EMPTY gc-manifests listing never reads healthy" do
      report =
        Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: listing_s3({:ok, []})))

      refute report.ok
      refute report.tier2.ok
      assert report.tier2.verdict == :unknown
      assert report.tier2.newest_manifest_age_ms == nil
    end

    test "a failed listing never reads healthy" do
      for result <- [{:error, :boom}, {:error, %RuntimeError{}}] do
        report =
          Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: listing_s3(result)))

        refute report.ok
        assert report.tier2.verdict == :unknown
      end
    end

    test "a raising list fun never reads healthy" do
      raising = fn _prefix -> raise "s3 down" end

      report =
        Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: %{list: raising}))

      refute report.ok
      assert report.tier2.verdict == :unknown
    end

    test "no store configured never reads healthy" do
      report = Durability.evaluate([node_fact("node-1", true)], ["node-1"], %{}, opts(s3: nil))

      refute report.ok
      assert report.tier2.verdict == :unknown
    end
  end

  # -- supervised process --------------------------------------------------------

  describe "supervised process" do
    test "accumulates streaks across rounds through its own capacity read" do
      table = new_cap_table()
      put_fact(table, node_fact("node-1", false))

      {:ok, pid} =
        Durability.start_link(
          name: nil,
          enabled: true,
          capacity_table: table,
          expected_nodes: ["node-1"],
          round_interval_ms: 0,
          streak_threshold: 3,
          s3: manifest_s3(@hour),
          clock: fn -> @mono end,
          wall_clock: fn -> @wall end
        )

      # The first snapshot computes synchronously (round one).
      first = Durability.snapshot(pid)
      assert get_in(first, [:tier1, :streaks, :session]) == 1

      for n <- 2..4 do
        report = GenServer.call(pid, :round_now)
        assert get_in(report, [:tier1, :streaks, :session]) == n
      end

      latched = GenServer.call(pid, :round_now)
      refute latched.ok
      assert latched.tier1.verdict == :export_failure_streak

      # Snapshot now returns the CACHED verdict without recomputing.
      assert Durability.snapshot(pid).tier1.verdict == :export_failure_streak

      # Recovery: the node reports confirmed store copies.
      put_fact(table, node_fact("node-1", true))
      recovered = GenServer.call(pid, :round_now)
      assert recovered.ok
      assert recovered.tier1.verdict == :ok
    end

    test "dark process answers :suspended and evaluates nothing" do
      {:ok, pid} = Durability.start_link(name: nil, enabled: false, capacity_table: new_cap_table())
      assert Durability.snapshot(pid) == :suspended
    end

    test "missing expected nodes surface as unknown through the process too" do
      {:ok, pid} =
        Durability.start_link(
          name: nil,
          enabled: true,
          capacity_table: new_cap_table(),
          expected_nodes: ["node-none"],
          round_interval_ms: 0,
          s3: manifest_s3(@hour),
          clock: fn -> @mono end,
          wall_clock: fn -> @wall end
        )

      report = Durability.snapshot(pid)
      refute report.ok
      assert report.tier1.verdict == :unknown
    end
  end

  # -- helpers -----------------------------------------------------------------

  # Run `n` rounds of the pure evaluation, threading streaks through, returning
  # the last report + final streak map.
  defp run_rounds(facts, expected, initial_streaks, n) do
    Enum.reduce(1..n//1, {%{}, initial_streaks}, fn _i, {_rep, streaks} ->
      report = Durability.evaluate(facts, expected, streaks, opts())
      {report, get_in(report, [:tier1, :streaks])}
    end)
  end

  defp put_fact(table, fact) do
    NodeCapacity.put(table, {fact.node_id, "pod"}, fact)
  end
end
