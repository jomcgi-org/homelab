defmodule Embervm.WakeInstanceTest do
  @moduledoc """
  Exercises Embervm.WakeInstance (brick co-location foundation, Step 4): selecting
  the SPECIFIC instance on a resolved anchor node a wake must dial, so a co-located
  brick set no longer collapses to the last node-name registrant.

  Covers the spec's four cases: a relight prefers the bundle-OWNING instance; a cold
  wake picks a mem-eligible instance and skips a too-small classed brick; the DS
  wildcard (empty class / zero budget) is always eligible; no eligible instance ->
  a clean :no_eligible_instance failure (never a bad dial). Plus the inert
  single-instance case that keeps the current fleet output-equivalent.
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, WakeInstance}

  setup do
    table = :"wi_cap_#{System.unique_integer([:positive])}"
    NodeCapacity.create(table)
    %{table: table}
  end

  # A per-instance capacity fact. Defaults model a real classed brick with headroom
  # and a free slot; override per case (size_class "" / mem_budget_mib 0 => wildcard).
  defp put_instance(table, node_id, pod_uid, opts) do
    instance_id = "#{node_id}/#{pod_uid}"

    facts =
      %{
        node_id: node_id,
        configured_id: node_id,
        pod_uid: pod_uid,
        instance_id: instance_id,
        size_class: Keyword.get(opts, :size_class, "8gi"),
        mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8_000),
        mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 8_192),
        live_vms: Keyword.get(opts, :live_vms, 0),
        max_live_vms: Keyword.get(opts, :max_live_vms, 4),
        stateful_bundles: Keyword.get(opts, :stateful_bundles, []),
        serving_snapshots: Keyword.get(opts, :serving_snapshots, []),
        group_bundle_sets: Keyword.get(opts, :group_bundle_sets, []),
        updated_at: Keyword.get(opts, :updated_at, 0)
      }

    NodeCapacity.put(table, {node_id, pod_uid}, facts)
    instance_id
  end

  describe "warmth ownership (a relight lands on the bundle-owning instance)" do
    test "prefers the instance whose stateful_bundles report the snapshot_ref", %{table: table} do
      # Two co-located bricks on node-4; only pod-b banked the bundle on disk.
      _a = put_instance(table, "node-4", "pod-a", stateful_bundles: [])

      owner =
        put_instance(table, "node-4", "pod-b",
          stateful_bundles: [%{snapshot_ref: "stateful/wl-a", workload: "wl-a", generation: 3}]
        )

      assert {:ok, ^owner} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "wl-a",
                 need_mib: 2_000,
                 warmth_key: :stateful_bundles,
                 warmth_ref: "stateful/wl-a"
               )
    end

    test "a group set owner is matched by group_instance_id", %{table: table} do
      _a = put_instance(table, "node-4", "pod-a", group_bundle_sets: [])

      owner =
        put_instance(table, "node-4", "pod-b",
          group_bundle_sets: [%{group_instance_id: "grp-1", set_id: "set-xyz", members: [%{member_name: "db"}]}]
        )

      assert {:ok, ^owner} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "gwl",
                 need_mib: 1_000,
                 warmth_key: :group_bundle_sets,
                 warmth_match_field: :group_instance_id,
                 warmth_ref: "grp-1"
               )
    end

    test "the owner is chosen even if it is mem-too-small (a relight must land on its disk)", %{table: table} do
      # The owner is a 2Gi brick with only 100 MiB headroom; the need is 4000. Warmth
      # ownership wins regardless: the bundle is only on this instance's disk.
      owner =
        put_instance(table, "node-4", "pod-small",
          size_class: "2gi",
          mem_headroom_mib: 100,
          mem_budget_mib: 2_048,
          stateful_bundles: [%{snapshot_ref: "stateful/wl-a"}]
        )

      _big = put_instance(table, "node-4", "pod-big", size_class: "16gi", mem_headroom_mib: 16_000, mem_budget_mib: 16_384)

      assert {:ok, ^owner} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "wl-a",
                 need_mib: 4_000,
                 warmth_key: :stateful_bundles,
                 warmth_ref: "stateful/wl-a"
               )
    end
  end

  describe "cold pick (mem-eligible, skips too-small classed bricks)" do
    test "picks a mem-eligible instance and never a too-small classed brick", %{table: table} do
      # A 2Gi brick (100 MiB headroom) and an 8Gi brick (8000 MiB). The 2Gi is too
      # small for a 4000 MiB need; selection must land on the 8Gi one, for every
      # workload key (so it is never the 2Gi brick by hash chance).
      small = put_instance(table, "node-4", "pod-small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)
      big = put_instance(table, "node-4", "pod-big", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      for wl <- ~w(wl-a wl-b wl-c wl-d wl-e) do
        assert {:ok, picked} =
                 WakeInstance.select("node-4", table: table, workload: wl, need_mib: 4_000, warmth_key: :stateful_bundles)

        assert picked == big
        refute picked == small
      end
    end

    test "no warmth_ref given falls straight through to the cold pick", %{table: table} do
      big = put_instance(table, "node-4", "pod-big", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      # warmth_key present but no warmth_ref => nothing to prefer => cold pick.
      assert {:ok, ^big} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 1_000, warmth_key: :stateful_bundles)
    end
  end

  describe "DS wildcard is always mem-eligible" do
    test "an empty-size-class instance is eligible even with zero headroom", %{table: table} do
      wildcard =
        put_instance(table, "node-4", "ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0, max_live_vms: 10)

      assert {:ok, ^wildcard} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 4_000)
    end

    test "a zero-budget (no cgroup limit) instance is eligible even with a non-empty class", %{table: table} do
      # Non-empty class but mem_budget_mib 0 (no cgroup limit): treated as the big
      # burst envelope, always mem-eligible.
      wildcard = put_instance(table, "node-4", "burst", size_class: "8gi", mem_headroom_mib: 0, mem_budget_mib: 0)

      assert {:ok, ^wildcard} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 4_000)
    end
  end

  describe "no eligible instance -> clean failure (never a bad dial)" do
    test "a node whose only instance is a too-small classed brick fails cleanly", %{table: table} do
      _small = put_instance(table, "node-4", "pod-small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)

      assert {:error, :no_eligible_instance} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 4_000)
    end

    test "a node with no free slots fails cleanly", %{table: table} do
      _full = put_instance(table, "node-4", "pod-full", mem_headroom_mib: 8_000, live_vms: 4, max_live_vms: 4)

      assert {:error, :no_eligible_instance} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 1_000)
    end

    test "a node not in the table fails cleanly", %{table: table} do
      assert {:error, :no_eligible_instance} =
               WakeInstance.select("ghost-node", table: table, workload: "wl-a", need_mib: 1_000)
    end
  end

  describe "inert on the single-instance-per-node fleet" do
    test "the node's sole instance is returned and dialed by its instance_id", %{table: table} do
      only = put_instance(table, "node-4", "pod-only", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0, max_live_vms: 4)

      # Cold pick and warmth pick both resolve to the same sole instance.
      assert {:ok, ^only} = WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 512)

      only_with_bundle =
        put_instance(table, "node-4", "pod-only",
          size_class: "",
          mem_headroom_mib: 0,
          mem_budget_mib: 0,
          max_live_vms: 4,
          stateful_bundles: [%{snapshot_ref: "stateful/wl-a"}]
        )

      assert {:ok, ^only_with_bundle} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "wl-a",
                 need_mib: 512,
                 warmth_key: :stateful_bundles,
                 warmth_ref: "stateful/wl-a"
               )
    end

    test "a fact without an instance_id falls back to the node name (legacy/dual-key)", %{table: table} do
      # Mirror a statically-seeded / pre-field fact: no :instance_id, keyed by node.
      NodeCapacity.put(table, "node-4", %{
        node_id: "node-4",
        configured_id: "node-4",
        size_class: "",
        mem_headroom_mib: 0,
        mem_budget_mib: 0,
        live_vms: 0,
        max_live_vms: 4,
        stateful_bundles: [%{snapshot_ref: "stateful/wl-a"}]
      })

      assert {:ok, "node-4"} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 512)

      assert {:ok, "node-4"} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "wl-a",
                 need_mib: 512,
                 warmth_key: :stateful_bundles,
                 warmth_ref: "stateful/wl-a"
               )
    end
  end
end
