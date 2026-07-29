defmodule Embervm.BrickLedgerTest do
  # Brick-capacity PR-1: the ledger is a pure query surface over the registry's
  # per-instance capacity ETS. These cases populate that table directly (the
  # ledger has no process of its own) and assert the brick derivation, class
  # bucketing, wildcard/headroom/slot filtering, and the deterministic sticky
  # pick. Nothing here drives placement: PR-1's ledger is populated but unread,
  # so this is a unit test of the primitive PR-2 will wire consumers to.
  use ExUnit.Case, async: true

  alias Embervm.{BrickLedger, NodeCapacity}

  defp unique_table, do: :"bl_test_#{System.unique_integer([:positive])}"

  # Insert one brick's capacity facts under the {node, pod_uid} instance key the
  # registry uses, carrying only the fields the ledger reads.
  defp put_brick(table, opts) do
    node = Keyword.fetch!(opts, :node)
    pod_uid = Keyword.fetch!(opts, :pod_uid)

    facts = %{
      node_id: node,
      pod_uid: pod_uid,
      instance_id: "#{node}/#{pod_uid}",
      size_class: Keyword.get(opts, :size_class, ""),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 100_000),
      mem_reject_floor_mib: Keyword.get(opts, :mem_reject_floor_mib, 0),
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 0),
      live_vms: Keyword.get(opts, :live_vms, 0),
      max_live_vms: Keyword.get(opts, :max_live_vms, 8),
      updated_at: Keyword.get(opts, :updated_at, 0)
    }

    NodeCapacity.put(table, {node, pod_uid}, facts)
  end

  defp new_table do
    table = unique_table()
    NodeCapacity.create(table)
    table
  end

  describe "bricks/1 and by_class/1" do
    test "derives bricks with free_slots and buckets by class" do
      table = new_table()
      put_brick(table, node: "node-4", pod_uid: "a", size_class: "2gi", live_vms: 3, max_live_vms: 8)
      put_brick(table, node: "node-4", pod_uid: "b", size_class: "16gi", live_vms: 8, max_live_vms: 8)

      bricks = BrickLedger.bricks(table)
      assert length(bricks) == 2

      small = Enum.find(bricks, &(&1.pod_uid == "a"))
      assert small.size_class == "2gi"
      assert small.free_slots == 5

      large = Enum.find(bricks, &(&1.pod_uid == "b"))
      assert large.free_slots == 0

      by_class = BrickLedger.by_class(table)
      assert Map.keys(by_class) |> Enum.sort() == ["16gi", "2gi"]
    end

    test "empty table yields no bricks (fail-closed default)" do
      assert BrickLedger.bricks(unique_table()) == []
    end
  end

  describe "candidates/3" do
    test "matches exact class and folds in the wildcard (empty-class) brick" do
      table = new_table()
      put_brick(table, node: "node-4", pod_uid: "exact", size_class: "8gi")
      put_brick(table, node: "node-5", pod_uid: "wild", size_class: "")
      put_brick(table, node: "node-6", pod_uid: "other", size_class: "2gi")

      pods = BrickLedger.candidates("8gi", 1_000, table) |> Enum.map(& &1.pod_uid)
      assert Enum.sort(pods) == ["exact", "wild"]
    end

    test "filters out bricks without memory headroom or a free slot" do
      table = new_table()
      put_brick(table, node: "n", pod_uid: "no-mem", size_class: "2gi", mem_headroom_mib: 500)
      put_brick(table, node: "n", pod_uid: "no-slot", size_class: "2gi", live_vms: 8, max_live_vms: 8)
      put_brick(table, node: "n", pod_uid: "ok", size_class: "2gi", mem_headroom_mib: 4_000, max_live_vms: 8)

      pods = BrickLedger.candidates("2gi", 2_000, table) |> Enum.map(& &1.pod_uid)
      assert pods == ["ok"]
    end
  end

  describe "pick/4" do
    test "does not pick a brick that clears need but not need plus the admission floor" do
      table = new_table()
      put_brick(table, node: "n", pod_uid: "small", size_class: "2gi", mem_headroom_mib: 1_933, mem_reject_floor_mib: 512, mem_budget_mib: 2_048)
      put_brick(table, node: "n", pod_uid: "large", size_class: "2gi", mem_headroom_mib: 16_384, mem_reject_floor_mib: 512, mem_budget_mib: 16_384)

      {:ok, brick} = BrickLedger.pick("2gi", 1_536, "key", table)
      assert brick.pod_uid == "large"
    end

    test "fleet_full when no brick can serve the request" do
      table = new_table()
      put_brick(table, node: "n", pod_uid: "wrong", size_class: "2gi", mem_headroom_mib: 100)
      assert BrickLedger.pick("2gi", 50_000, "key", table) == {:error, :fleet_full}
    end

    test "is deterministic and sticky for a given key" do
      table = new_table()
      for c <- ["a", "b", "c"], do: put_brick(table, node: "n", pod_uid: c, size_class: "4gi")

      {:ok, first} = BrickLedger.pick("4gi", 1_000, "session-42", table)
      {:ok, again} = BrickLedger.pick("4gi", 1_000, "session-42", table)
      assert first.instance_id == again.instance_id
    end

    test "spreads distinct keys across bricks rather than collapsing to one" do
      table = new_table()
      for c <- ["a", "b", "c"], do: put_brick(table, node: "n", pod_uid: c, size_class: "4gi")

      chosen =
        for k <- 1..60 do
          {:ok, b} = BrickLedger.pick("4gi", 1_000, "key-#{k}", table)
          b.pod_uid
        end

      assert chosen |> Enum.uniq() |> length() > 1
    end
  end

  describe "choose/2 (the selection primitive the dispatcher reuses)" do
    # entry maps mimic what the dispatcher passes: raw capacity facts, keyed by
    # :instance_id, NOT bricks derived from the ledger.
    defp entry(id), do: %{instance_id: id, node_id: "n"}

    test "empty list yields nil" do
      assert BrickLedger.choose([], "k") == nil
    end

    test "single candidate is always chosen (output-equivalence, one-per-node)" do
      only = entry("node-4/a")
      assert BrickLedger.choose([only], "wl-a") == only
      assert BrickLedger.choose([only], "wl-b") == only
    end

    test "is deterministic and sticky for a given key" do
      cands = for id <- ["node-4/a", "node-5/b", "node-6/c"], do: entry(id)
      first = BrickLedger.choose(cands, "wl-a")
      assert first == BrickLedger.choose(cands, "wl-a")
    end

    test "order-independent: choice does not depend on input list order" do
      cands = for id <- ["node-4/a", "node-5/b", "node-6/c"], do: entry(id)
      assert BrickLedger.choose(cands, "wl-a") == BrickLedger.choose(Enum.reverse(cands), "wl-a")
    end

    test "spreads distinct keys across candidates" do
      cands = for id <- ["node-4/a", "node-4/b", "node-4/c"], do: entry(id)
      chosen = for k <- 1..60, do: BrickLedger.choose(cands, "wl-#{k}").instance_id
      assert chosen |> Enum.uniq() |> length() > 1
    end

    test "chooses the fuller classed brick by headroom" do
      table = new_table()
      put_brick(table, node: "n", pod_uid: "full", size_class: "8gi", mem_headroom_mib: 2_000, mem_budget_mib: 8_192)
      put_brick(table, node: "n", pod_uid: "empty", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      assert BrickLedger.choose(BrickLedger.candidates("8gi", 1_000, table), "wl").pod_uid == "full"
    end

    test "chooses the fuller classed brick by live VM count" do
      table = new_table()
      put_brick(table, node: "n", pod_uid: "full", size_class: "8gi", live_vms: 6, max_live_vms: 8, mem_headroom_mib: 8_000, mem_budget_mib: 8_192)
      put_brick(table, node: "n", pod_uid: "empty", size_class: "8gi", live_vms: 1, max_live_vms: 8, mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      assert BrickLedger.choose(BrickLedger.candidates("8gi", 1_000, table), "wl").pod_uid == "full"
    end

    test "prefers a classed brick over a wildcard" do
      table = new_table()
      put_brick(table, node: "n", pod_uid: "wild", size_class: "", mem_headroom_mib: 8_000, mem_budget_mib: 0)
      put_brick(table, node: "n", pod_uid: "classed", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      assert BrickLedger.choose(BrickLedger.candidates("8gi", 1_000, table), "wl").pod_uid == "classed"
    end
  end
end
