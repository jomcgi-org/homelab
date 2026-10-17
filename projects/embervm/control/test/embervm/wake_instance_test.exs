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
  alias Embervm.Scheduler.Inflight

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
        session_snapshots: Keyword.get(opts, :session_snapshots, []),
        serving_snapshots: Keyword.get(opts, :serving_snapshots, []),
        group_bundle_sets: Keyword.get(opts, :group_bundle_sets, []),
        group_member_vms: Keyword.get(opts, :group_member_vms, []),
        serving_vms: Keyword.get(opts, :serving_vms, []),
        stateful_vms: Keyword.get(opts, :stateful_vms, []),
        vm_overhead_mib: Keyword.get(opts, :vm_overhead_mib, 0),
        admits_on_reservation: Keyword.get(opts, :admits_on_reservation, false),
        updated_at: Keyword.get(opts, :updated_at, 0)
      }

    NodeCapacity.put(table, {node_id, pod_uid}, facts)
    instance_id
  end

  test "session relight resolves the instance reporting the snapshot", %{table: table} do
    put_instance(table, "node-4", "pod-a", session_snapshots: [%{snapshot_ref: "sess-1"}])
    assert {:ok, "node-4/pod-a"} = WakeInstance.node_for_relight(%{node_id: "node-4", snapshot_ref: "sess-1"}, table)
  end

  test "session relight reports snapshot_lost when the snapshot is absent", %{table: table} do
    put_instance(table, "node-4", "pod-a", [])
    assert {:error, :snapshot_lost} = WakeInstance.node_for_relight(%{node_id: "node-4", snapshot_ref: "gone"}, table)
  end

  test "session relight reports an unreported owner while another node reports", %{table: table} do
    put_instance(table, "node-5", "pod-a", [])

    assert {:error, {:node_unreported, "node-4"}} =
             WakeInstance.node_for_relight(%{node_id: "node-4", snapshot_ref: "sess-1"}, table)
  end

  test "session relight reports no_bricks when capacity is empty", %{table: table} do
    assert {:error, :no_bricks} =
             WakeInstance.node_for_relight(%{node_id: "node-4", snapshot_ref: "sess-1"}, table)
  end

  test "session relight reports snapshot_lost for nil snapshot ref when capacity is empty", %{table: table} do
    assert {:error, :snapshot_lost} =
             WakeInstance.node_for_relight(%{node_id: "node-4", snapshot_ref: nil}, table)
  end

  test "session relight reports snapshot_lost for empty snapshot ref when capacity is empty", %{table: table} do
    assert {:error, :snapshot_lost} =
             WakeInstance.node_for_relight(%{node_id: "node-4", snapshot_ref: ""}, table)
  end

  test "serving relight resolves a serving-capable node", %{table: table} do
    NodeCapacity.put(table, "node-4", %{
      node_id: "node-4",
      configured_id: "node-4",
      serving_subnet_cidr: "10.99.0.0/24",
      serving_snapshots: [%{snapshot_ref: "serving-1"}]
    })

    assert {:ok, "node-4"} = WakeInstance.serving_node_for_relight(%{node_id: "node-4", snapshot_ref: "serving-1"}, table)
  end

  test "current serving image ref reads the node workload entry", %{table: table} do
    NodeCapacity.put(table, "node-4", %{node_id: "node-4", workloads: %{"wl-a" => %{serving_image_ref: "img-1"}}})
    assert WakeInstance.current_serving_image_ref(table, "node-4", "wl-a") == "img-1"
    assert WakeInstance.current_serving_image_ref(table, "node-dead", "wl-a") == nil
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

  describe "DS wildcard is always mem-eligible, a blind classed brick never is" do
    test "an empty-size-class instance is eligible even with zero headroom", %{table: table} do
      wildcard =
        put_instance(table, "node-4", "ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0, max_live_vms: 10)

      assert {:ok, ^wildcard} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 4_000)
    end

    test "a zero-budget CLASSED instance is blind and never eligible (fail closed)", %{table: table} do
      # Deliberately inverted from the old fail-open reading. A classed brick
      # reporting mem_budget_mib 0 used to count as the unlimited burst envelope,
      # but that is also exactly what a jailer-armed brick whose cgroup readout
      # broke reports, and placing on it would admit every guest. Only an EMPTY
      # class is the wildcard now; a classed zero budget is blind.
      _blind = put_instance(table, "node-4", "burst", size_class: "8gi", mem_headroom_mib: 0, mem_budget_mib: 0)

      assert {:error, :no_eligible_instance} =
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
      # Brick.to_brick/1's `|| %{}` default makes base_ready false even though
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

  describe "the rejection diagnostic survives the real log formatter (#4077)" do
    # The regression this pins: the payload originally rode Logger METADATA, and
    # Embervm.LogFormatter encodes a fixed @meta_keys whitelist and drops the rest
    # (deliberately -- an un-encodable term must never crash the formatter). In prod
    # the line emitted with only workload/node_id and every diagnostic field was
    # silently discarded. Testing brick_rejection_view/3 alone did NOT catch that:
    # the data was right and never reached a log line.

    defp diag_brick(opts) do
      %{
        instance_id: "node-2/pod-a",
        size_class: Keyword.get(opts, :size_class, "8gi"),
        free_slots: Keyword.get(opts, :free_slots, 4),
        mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8_000),
        mem_budget_mib: 8_192,
        workloads: Keyword.get(opts, :workloads, %{})
      }
    end

    test "the summary carries every field needed to tell the two failures apart" do
      summary =
        WakeInstance.rejection_summary(512, false, [diag_brick(free_slots: 4)], "wl-a")

      for field <- ~w(need_mib capacity_denial brick_count eligible base_ready workload_facts) do
        assert summary =~ field, "summary must carry #{field}"
      end
    end

    test "the payload renders through Embervm.LogFormatter, not just into a string" do
      # End-to-end through the ACTUAL formatter, without ExUnit.CaptureLog (which in
      # an async module also captures other tests' output).
      summary = WakeInstance.rejection_summary(512, false, [diag_brick([])], "wl-a")

      line =
        Embervm.LogFormatter.format(
          %{
            level: :warning,
            msg: {:string, "embervm wake: no cold-placement candidate on node " <> summary},
            meta: %{workload: "wl-a", node_id: "node-2"}
          },
          %{}
        )
        |> IO.iodata_to_binary()

      assert line =~ "no cold-placement candidate"
      # The fields that were dropped when this rode metadata:
      assert line =~ "capacity_denial=false"
      assert line =~ "workload_facts=0"
      assert line =~ "brick_count=1"
      # And the whitelisted metadata still lands as structured JSON fields.
      assert line =~ ~s("workload":"wl-a")
      assert line =~ ~s("node_id":"node-2")
    end

    test "a capacity wall and a provisioning wait are distinguishable in the line" do
      wall = WakeInstance.rejection_summary(512, true, [diag_brick(free_slots: 0)], "wl-a")
      wait = WakeInstance.rejection_summary(512, false, [diag_brick(free_slots: 4)], "wl-a")

      assert wall =~ "capacity_denial=true"
      assert wall =~ "free=0"
      assert wait =~ "capacity_denial=false"
      assert wait =~ "eligible=true"
    end

    test "an empty brick list renders without crashing the formatter" do
      # node scoping matching nothing is a real outcome; the diagnostic must still
      # emit rather than raise inside a log call.
      summary = WakeInstance.rejection_summary(512, true, [], "wl-a")
      assert summary =~ "brick_count=0"
      assert summary =~ "bricks=[none]"
    end
  end

  # Runs `fun` in a traced child process and returns `{result, casts}`, where
  # `casts` is the autoscaler demand it recorded, as `{tag, workload, need}`: the
  # BrickController casts it sent. Async-safe (only the child's sends are seen),
  # and trace_delivered makes the read deterministic.
  defp demand_casts(fun) do
    parent = self()

    {pid, mon} =
      spawn_monitor(fn ->
        receive do
          :go -> send(parent, {:demand_result, fun.()})
        end
      end)

    :erlang.trace(pid, true, [:send])
    send(pid, :go)
    assert_receive {:demand_result, result}
    assert_receive {:DOWN, ^mon, :process, ^pid, _}
    ref = :erlang.trace_delivered(:all)
    assert_receive {:trace_delivered, :all, ^ref}
    {result, collect_demand_casts([])}
  end

  defp collect_demand_casts(acc) do
    receive do
      {:trace, _pid, kind, {:"$gen_cast", {tag, workload, need}}, _to}
      when kind in [:send, :send_to_non_existing_process] and tag in [:denial, :empty_demand] ->
        collect_demand_casts([{tag, workload, need} | acc])

      {:trace, _pid, _kind, _msg, _to} ->
        collect_demand_casts(acc)
    after
      0 -> Enum.reverse(acc)
    end
  end

  describe "in-flight claims" do
    setup do
      %{ledger: Inflight.create(:"wi_inflight_#{System.unique_integer([:positive])}")}
    end

    test "a cold pick with a claim_ref charges the picked brick its need plus overhead", %{table: table, ledger: ledger} do
      dial = put_instance(table, "node-4", "pod-a", vm_overhead_mib: 64, admits_on_reservation: true)

      assert {:ok, ^dial} =
               WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 1_000, claim_ref: :wake, inflight: ledger)

      assert Inflight.pending_mib(dial, table: ledger) == 1_064
    end

    test "without a claim_ref the pick charges nothing", %{table: table, ledger: ledger} do
      dial = put_instance(table, "node-4", "pod-a", [])
      assert {:ok, ^dial} = WakeInstance.select("node-4", table: table, workload: "wl-a", need_mib: 1_000, inflight: ledger)
      assert Inflight.entries(table: ledger) == []
    end

    test "serial picks spread across bricks by their claims, then run out", %{table: table, ledger: ledger} do
      a = put_instance(table, "node-4", "pod-a", size_class: "4gi", mem_headroom_mib: 4_000, mem_budget_mib: 4_096)
      b = put_instance(table, "node-4", "pod-b", size_class: "4gi", mem_headroom_mib: 4_000, mem_budget_mib: 4_096)
      opts = [table: table, workload: "wl-a", need_mib: 3_000, inflight: ledger]

      # Both bricks read the same stale headroom; only the first wake's claim tells
      # the second that its head is already spoken for.
      assert {:ok, first} = WakeInstance.select("node-4", [claim_ref: :one] ++ opts)
      assert {:ok, second} = WakeInstance.select("node-4", [claim_ref: :two] ++ opts)
      assert Enum.sort([first, second]) == Enum.sort([a, b])
      assert Inflight.pending_mib(a, table: ledger) == 3_000
      assert Inflight.pending_mib(b, table: ledger) == 3_000

      assert {:error, :no_eligible_instance} =
               WakeInstance.select("node-4", [claim_ref: :three, record_demand: false] ++ opts)

      # A release (the brick refused, or the boot failed) makes it placeable again.
      Inflight.release(first, :one, table: ledger)
      assert {:ok, ^first} = WakeInstance.select("node-4", [claim_ref: :four] ++ opts)
    end

    test "a relight claims the owning instance without re-gating it on capacity", %{table: table, ledger: ledger} do
      owner =
        put_instance(table, "node-4", "pod-b",
          mem_headroom_mib: 100,
          stateful_bundles: [%{snapshot_ref: "stateful/wl-a", workload: "wl-a", generation: 3}]
        )

      assert {:ok, ^owner} =
               WakeInstance.select("node-4",
                 table: table,
                 workload: "wl-a",
                 need_mib: 2_000,
                 warmth_key: :stateful_bundles,
                 warmth_ref: "stateful/wl-a",
                 claim_ref: :relight,
                 inflight: ledger
               )

      assert Inflight.pending_mib(owner, table: ledger) == 2_000
    end

    test "cold_candidates with a claim_ref claims only the head of the frontier", %{table: table, ledger: ledger} do
      put_instance(table, "node-4", "pod-a", [])
      put_instance(table, "node-4", "pod-b", [])

      assert {:ok, [head, tail]} =
               WakeInstance.cold_candidates("node-4", table: table, workload: "wl-a", need_mib: 1_000, claim_ref: :c, inflight: ledger)

      assert Inflight.pending_mib(head, table: ledger) == 1_000
      assert Inflight.pending_mib(tail, table: ledger) == 0
    end

    test "claim/4 charges the brick's overhead, and the bare need for a dial with no fact", %{table: table, ledger: ledger} do
      dial = put_instance(table, "node-4", "pod-a", vm_overhead_mib: 64, admits_on_reservation: true)

      assert :ok = WakeInstance.claim(dial, :m, 512, table: table, inflight: ledger)
      assert :ok = WakeInstance.claim("node-9/gone", :m, 512, table: table, inflight: ledger)
      assert :ok = WakeInstance.claim(dial, :off, 512, table: table, inflight: false)

      assert Inflight.pending_mib(dial, table: ledger) == 576
      assert Inflight.pending_mib("node-9/gone", table: ledger) == 512
    end
  end

  describe "pinned demand" do
    test "a pinned miss records no demand with record_demand: false, and one denial by default", %{table: table} do
      # The only brick on the node is out of slots: a capacity wall, not a base wait.
      put_instance(table, "node-4", "pod-a", live_vms: 4, max_live_vms: 4)
      opts = [table: table, workload: "wl-a", need_mib: 512]

      assert {{:error, :no_eligible_instance}, []} =
               demand_casts(fn -> WakeInstance.select("node-4", [record_demand: false] ++ opts) end)

      assert {{:error, :no_eligible_instance}, []} =
               demand_casts(fn -> WakeInstance.cold_candidates("node-4", [record_demand: false] ++ opts) end)

      assert {{:error, :no_eligible_instance}, [{:denial, "wl-a", 512}]} =
               demand_casts(fn -> WakeInstance.select("node-4", opts) end)

      assert {{:error, :no_eligible_instance}, [{:denial, "wl-a", 512}]} =
               demand_casts(fn -> WakeInstance.cold_candidates("node-4", opts) end)
    end
  end

  describe "drains_with_instance?" do
    test "spares only a guest a co-located sibling positively reports", %{table: table} do
      # pod-2gi is draining, so the registry already dropped its fact.
      put_instance(table, "node-4", "pod-8gi", serving_vms: [%{vm_id: "vm-a"}])

      refute WakeInstance.drains_with_instance?(table, "node-4", "pod-2gi", :serving_vms, :vm_id, "vm-a")
      assert WakeInstance.drains_with_instance?(table, "node-4", "pod-2gi", :serving_vms, :vm_id, "vm-b")
    end

    test "the draining instance's own report and a node-scoped drain still drain", %{table: table} do
      put_instance(table, "node-4", "pod-2gi", stateful_vms: [%{vm_id: "vm-b"}])
      put_instance(table, "node-4", "pod-8gi", stateful_vms: [%{vm_id: "vm-a"}])

      assert WakeInstance.drains_with_instance?(table, "node-4", "pod-2gi", :stateful_vms, :vm_id, "vm-b")
      assert WakeInstance.drains_with_instance?(table, "node-4", "", :stateful_vms, :vm_id, "vm-a")
      assert WakeInstance.drains_with_instance?(table, "node-4", nil, :stateful_vms, :vm_id, "vm-a")
    end

    test "a reporting fact without an instance_id, or on another node, is no proof", %{table: table} do
      NodeCapacity.put(table, "node-4", %{node_id: "node-4", configured_id: "node-4", serving_vms: [%{vm_id: "vm-a"}]})
      put_instance(table, "node-5", "pod-8gi", serving_vms: [%{vm_id: "vm-c"}])

      assert WakeInstance.drains_with_instance?(table, "node-4", "pod-2gi", :serving_vms, :vm_id, "vm-a")
      assert WakeInstance.drains_with_instance?(table, "node-4", "pod-2gi", :serving_vms, :vm_id, "vm-c")
    end

    test "a group is spared by a sibling's live members, never by a banked set", %{table: table} do
      put_instance(table, "node-4", "pod-8gi", group_bundle_sets: [%{group_instance_id: "g-1", set_id: "s-1"}])
      assert WakeInstance.drains_with_instance?(table, "node-4", "pod-2gi", :group_member_vms, :group_instance_id, "g-1")

      put_instance(table, "node-4", "pod-4gi", group_member_vms: [%{group_instance_id: "g-1", member_name: "db"}])
      refute WakeInstance.drains_with_instance?(table, "node-4", "pod-2gi", :group_member_vms, :group_instance_id, "g-1")
    end

    test "draining_dial names the draining instance, or nothing for a node-scoped drain" do
      assert WakeInstance.draining_dial("node-4", "pod-2gi") == "node-4/pod-2gi"
      assert WakeInstance.draining_dial("node-4", "") == nil
      assert WakeInstance.draining_dial("node-4", nil) == nil
    end
  end
end
