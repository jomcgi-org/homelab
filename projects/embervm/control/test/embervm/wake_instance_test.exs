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

  # The workloads every cold-pick test queries; the fixture advertises each as
  # base READY by default so the base-readiness gate is satisfied and these tests
  # exercise the mem/slot behaviour. A test that wants a NOT-yet-advertised
  # instance (the fresh/rolled window) passes `advertise: []`.
  @default_advertised ~w(wl-a wl-b wl-c wl-d wl-e gwl)

  # A per-instance capacity fact. Defaults model a real classed brick with headroom
  # and a free slot that has ADVERTISED the default workloads' bases as READY;
  # override per case (size_class "" / mem_budget_mib 0 => wildcard; advertise: []
  # => a fresh instance that has not advertised any base yet).
  defp put_instance(table, node_id, pod_uid, opts) do
    instance_id = "#{node_id}/#{pod_uid}"

    workloads =
      for wl <- Keyword.get(opts, :advertise, @default_advertised), into: %{} do
        {wl, %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap/#{wl}"}}
      end

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
        workloads: workloads,
        stateful_bundles: Keyword.get(opts, :stateful_bundles, []),
        serving_snapshots: Keyword.get(opts, :serving_snapshots, []),
        group_bundle_sets: Keyword.get(opts, :group_bundle_sets, []),
        group_member_vms: Keyword.get(opts, :group_member_vms, []),
        updated_at: Keyword.get(opts, :updated_at, 0)
      }

    NodeCapacity.put(table, {node_id, pod_uid}, facts)
    instance_id
  end

  describe "warmth_also_keys (a bank of a RUNNING group resolves to its node, #4006)" do
    test "matches a LIVE group member even when the node has no free capacity", %{table: table} do
      # The running group occupies node-4, so it has NO room for a second copy. A bank
      # must still resolve to this node via its live group_member_vms, not fail the
      # free-capacity cold-pick (which would wedge the bank in :banking).
      owner =
        put_instance(table, "node-4", "pod-a",
          mem_headroom_mib: 100,
          group_member_vms: [%{group_instance_id: "grp-1", member_name: "server"}]
        )

      assert {:ok, ^owner} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "gwl",
                 need_mib: 7_168,
                 warmth_key: :group_bundle_sets,
                 warmth_also_keys: [:group_member_vms],
                 warmth_match_field: :group_instance_id,
                 warmth_ref: "grp-1"
               )
    end

    test "without warmth_also_keys a running-only instance still fails the capacity pick", %{table: table} do
      # Same node, same lack of capacity, but no live-member fallback key: the bank
      # would fail (the pre-#4006 behaviour), confirming the fix is what rescues it.
      _owner =
        put_instance(table, "node-4", "pod-a",
          mem_headroom_mib: 100,
          group_member_vms: [%{group_instance_id: "grp-1", member_name: "server"}]
        )

      assert {:error, :no_eligible_instance} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "gwl",
                 need_mib: 7_168,
                 warmth_key: :group_bundle_sets,
                 warmth_match_field: :group_instance_id,
                 warmth_ref: "grp-1"
               )
    end
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

    test "the owner is chosen even if it is mem-too-small AND has not advertised the base (a relight must land on its disk)", %{table: table} do
      # The owner is a 2Gi brick with only 100 MiB headroom (need 4000) that has
      # advertised NO base (advertise: []). Warmth ownership wins regardless of BOTH
      # the mem gate and the base-readiness gate: the bundle is only on this instance's
      # disk, so the readiness gate (which is a COLD-pick-only concern) must not touch it.
      owner =
        put_instance(table, "node-4", "pod-small",
          size_class: "2gi",
          mem_headroom_mib: 100,
          mem_budget_mib: 2_048,
          advertise: [],
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

  describe "cold pick gates on base readiness (co-location gap)" do
    test "skips a mem-eligible instance that has NOT advertised the base, picks the advertised one", %{table: table} do
      # fresh: mem-eligible + free slot, but advertises no base yet (the post-roll /
      # post-scale-up window: adopted bases from disk internally but noded has not
      # re-provisioned the runtime image, so base_state is absent and it cannot resolve
      # boot_image_ref). ready: same size, advertises the base READY.
      _fresh =
        put_instance(table, "node-4", "pod-fresh",
          size_class: "16gi",
          mem_headroom_mib: 16_000,
          mem_budget_mib: 16_384,
          advertise: []
        )

      ready =
        put_instance(table, "node-4", "pod-ready",
          size_class: "16gi",
          mem_headroom_mib: 16_000,
          mem_budget_mib: 16_384
        )

      for wl <- ~w(wl-a wl-b wl-c wl-d wl-e) do
        assert {:ok, picked} =
                 WakeInstance.select("node-4", table: table, workload: wl, need_mib: 4_000, warmth_key: :stateful_bundles)

        assert picked == ready
      end
    end

    test "the only base-ready instance is too small AND the mem-eligible one is not ready -> clean no-eligible (retryable, NOT a bad pick)", %{table: table} do
      # ready_small: advertises the base READY but is a 2Gi brick, too small for 4000.
      # big_fresh: mem-eligible (16Gi) but has NOT advertised the base. Neither is BOTH
      # eligible and ready, so selection returns :no_eligible_instance (the wake retries
      # once big_fresh finishes provisioning) rather than dialling either bad choice.
      _ready_small =
        put_instance(table, "node-4", "pod-small",
          size_class: "2gi",
          mem_headroom_mib: 100,
          mem_budget_mib: 2_048
        )

      _big_fresh =
        put_instance(table, "node-4", "pod-big",
          size_class: "16gi",
          mem_headroom_mib: 16_000,
          mem_budget_mib: 16_384,
          advertise: []
        )

      assert {:error, :no_eligible_instance} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 4_000, warmth_key: :stateful_bundles)
    end

    test "a node whose sole instance has not advertised the base fails cleanly (retryable wait)", %{table: table} do
      _fresh = put_instance(table, "node-4", "pod-fresh", advertise: [])

      assert {:error, :no_eligible_instance} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 512)
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

    test "a fact without an instance_id falls back to the node name (legacy/node-scoped)", %{table: table} do
      # Mirror a statically-seeded / pre-field fact: no :instance_id, keyed by node.
      # Post-B0c this bare node name is still a valid key for a node-scoped instance
      # (its instance_id IS the node name), so the fallback resolves; only a co-located
      # dial-home fact (which always carries :instance_id) would bypass it.
      NodeCapacity.put(table, "node-4", %{
        node_id: "node-4",
        configured_id: "node-4",
        size_class: "",
        mem_headroom_mib: 0,
        mem_budget_mib: 0,
        live_vms: 0,
        max_live_vms: 4,
        workloads: %{"wl-a" => %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: "snap/wl-a"}},
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

  describe "cold-rejection diagnostics (#4077)" do
    # :no_eligible_instance is returned for two unrelated reasons -- no slot/memory,
    # or eligible bricks that are not base-READY -- and from outside the process they
    # look identical. #4077 was hours spent guessing exactly that. These assert the
    # diagnostic says WHICH.
    #
    # Asserted as DATA rather than through the emitted log on purpose: CaptureLog in
    # an async module also captures concurrently-running tests, so counting log lines
    # here would reintroduce the flake class #4078 just removed.

    defp brick(opts) do
      %{
        instance_id: "node-4/pod-a",
        size_class: Keyword.get(opts, :size_class, "8gi"),
        free_slots: Keyword.get(opts, :free_slots, 4),
        mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8_000),
        mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 8_192),
        workloads: Keyword.get(opts, :workloads, %{})
      }
    end

    test "a brick that advertised ANOTHER workload is a genuine provisioning wait" do
      view =
        WakeInstance.brick_rejection_view(
          brick(workloads: %{"wl-other" => %{base_state: :BASE_BUILD_STATE_READY}}),
          "wl-a",
          512
        )

      # Room to spare, so a scale-up would not help and the autoscaler must not be
      # told this was a capacity problem.
      assert view.eligible
      refute view.base_ready
      # The submap EXISTS, so base_ready is false because the daemon really has not
      # advertised wl-a -- not because the fact lost its map.
      assert view.workload_facts == 1
      assert view.base_state == nil
    end

    test "a fact with NO workloads submap is distinguishable from one that advertised" do
      # The #4077 hypothesis shape: slots and memory fine, workloads absent, so
      # BrickLedger.to_brick/1's `|| %{}` default makes base_ready false even though
      # nothing ever said this workload was unready.
      view = WakeInstance.brick_rejection_view(brick(workloads: %{}), "wl-a", 512)

      assert view.eligible
      refute view.base_ready
      assert view.workload_facts == 0
      assert view.base_state == nil
    end

    test "an advertised-but-not-READY base reports its actual state" do
      view =
        WakeInstance.brick_rejection_view(
          brick(workloads: %{"wl-a" => %{base_state: :BASE_BUILD_STATE_BUILDING}}),
          "wl-a",
          512
        )

      refute view.base_ready
      assert view.workload_facts == 1
      # Present and named, so this is a build in flight, not a lost submap.
      assert view.base_state == :BASE_BUILD_STATE_BUILDING
    end

    test "a real capacity wall reports ineligible with the numbers that caused it" do
      view =
        WakeInstance.brick_rejection_view(
          brick(free_slots: 0, workloads: %{"wl-a" => %{base_state: :BASE_BUILD_STATE_READY}}),
          "wl-a",
          512
        )

      refute view.eligible
      assert view.free_slots == 0
      # Base was fine; the slot was not. Without both fields this is the ambiguity.
      assert view.base_ready
    end

    test "a too-small classed brick reports the headroom that failed the ask" do
      view = WakeInstance.brick_rejection_view(brick(mem_headroom_mib: 128), "wl-a", 512)

      refute view.eligible
      assert view.mem_headroom_mib == 128
      assert view.mem_budget_mib == 8_192
    end

    test "the throttle allows one diagnostic per {workload, node} per window" do
      # #4077 retried every ~10s for 2.5h (~900 rejections); unthrottled, the
      # diagnostic would bury the event it exists to surface.
      assert WakeInstance.throttle_rejection_log?("wl-a", "node-4")
      refute WakeInstance.throttle_rejection_log?("wl-a", "node-4")
      refute WakeInstance.throttle_rejection_log?("wl-a", "node-4")
    end

    test "the throttle is per workload and per node, so a second wedge is not silenced" do
      assert WakeInstance.throttle_rejection_log?("wl-a", "node-4")
      assert WakeInstance.throttle_rejection_log?("wl-b", "node-4")
      assert WakeInstance.throttle_rejection_log?("wl-a", "node-3")
      refute WakeInstance.throttle_rejection_log?("wl-a", "node-4")
    end
  end
end
