defmodule Embervm.SessionPlacementTest do
  @moduledoc """
  Task 8 placement seam: node_for_create picks a ready node with budget over the
  capacity table (rendezvous hash, trivially one node in v1), and node_for_relight
  resolves ONLY to a node currently reporting the session's snapshot (a lost
  snapshot => snapshot_lost). This is the reviewer-checked boundary: the router and
  SessionManager call THIS module and never inspect node facts, so its correctness
  is the whole placement contract.
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, SessionPlacement}

  defp table do
    t = :"placement_#{System.unique_integer([:positive])}"
    NodeCapacity.create(t)
    t
  end

  defp put_node(t, id, opts) do
    NodeCapacity.put(t, id, %{
      node_id: id,
      configured_id: id,
      workloads: %{
        "wl" => %{
          free_primed_slots: 1,
          snapshot_ref: Keyword.get(opts, :snapshot_ref, "base-wl"),
          base_state: Keyword.get(opts, :base_state, :BASE_BUILD_STATE_READY),
          primed_vm_ids: []
        }
      },
      live_vms: Keyword.get(opts, :live, 0),
      max_live_vms: Keyword.get(opts, :max, 8),
      session_vms: Keyword.get(opts, :session_vms, []),
      session_snapshots: Keyword.get(opts, :session_snapshots, []),
      snapshot_disk_free_bytes: Keyword.get(opts, :free, 1_000_000),
      snapshot_disk_used_bytes: 0,
      draining: false,
      updated_at: 0
    })
  end

  # -- create ---------------------------------------------------------------

  test "node_for_create picks a ready node with budget and returns its base snapshot_ref" do
    t = table()
    put_node(t, "node-4", snapshot_ref: "base-wl")

    assert {:ok, "node-4", "base-wl"} = SessionPlacement.node_for_create("wl", t)
  end

  test "node_for_create denies when the base is not ready" do
    t = table()
    put_node(t, "node-4", base_state: :BASE_BUILD_STATE_BUILDING)
    assert {:error, :no_capacity} = SessionPlacement.node_for_create("wl", t)
  end

  test "node_for_create denies when the node is at its live-VM cap" do
    t = table()
    put_node(t, "node-4", live: 8, max: 8)
    assert {:error, :no_capacity} = SessionPlacement.node_for_create("wl", t)
  end

  test "node_for_create denies when no node reports the workload" do
    t = table()
    assert {:error, :no_capacity} = SessionPlacement.node_for_create("other-wl", t)
  end

  test "node_for_create is deterministic (rendezvous hash) across eligible nodes" do
    t = table()
    put_node(t, "node-a", snapshot_ref: "base-a")
    put_node(t, "node-b", snapshot_ref: "base-b")

    {:ok, first, _} = SessionPlacement.node_for_create("wl", t)
    # Stable across repeated calls (no randomness): the same workload key always maps
    # to the same node until the node set changes.
    for _ <- 1..5, do: assert({:ok, ^first, _} = SessionPlacement.node_for_create("wl", t))
    assert first in ["node-a", "node-b"]
  end

  # -- grow-eager sizing gate (PR-I) ----------------------------------------

  test "node_for_create refuses a candidate the sizer reports infeasible and falls to the next" do
    t = table()
    put_node(t, "node-a", snapshot_ref: "base-a")
    put_node(t, "node-b", snapshot_ref: "base-b")

    # The rendezvous winner (whichever it is) is refused by the sizer; placement must
    # fall to the OTHER eligible node rather than deny.
    {:ok, winner, _} = SessionPlacement.node_for_create("wl", t)
    other = Enum.find(["node-a", "node-b"], &(&1 != winner))

    refuse_winner = fn node_id, "wl" ->
      if node_id == winner, do: {:error, :infeasible}, else: :ok
    end

    assert {:ok, ^other, _} = SessionPlacement.node_for_create("wl", t, refuse_winner)
  end

  test "node_for_create denies :no_capacity when the sizer refuses EVERY candidate" do
    t = table()
    put_node(t, "node-a", snapshot_ref: "base-a")
    put_node(t, "node-b", snapshot_ref: "base-b")

    refuse_all = fn _node_id, _wl -> {:error, :infeasible} end
    assert {:error, :no_capacity} = SessionPlacement.node_for_create("wl", t, refuse_all)
  end

  test "node_for_create proceeds when the sizer is disabled (legacy backstop only)" do
    t = table()
    put_node(t, "node-4", snapshot_ref: "base-wl")

    disabled = fn _node_id, _wl -> {:error, :disabled} end
    assert {:ok, "node-4", "base-wl"} = SessionPlacement.node_for_create("wl", t, disabled)
  end

  # -- relight --------------------------------------------------------------

  test "node_for_relight resolves to the node reporting the session's snapshot" do
    t = table()

    put_node(t, "node-4",
      session_snapshots: [%{snapshot_ref: "sess-snap-1", session_id: "s-1", workload: "wl", size_bytes: 10, created_at_unix_ms: 0}]
    )

    session = %{node_id: "node-4", snapshot_ref: "sess-snap-1"}
    assert {:ok, "node-4"} = SessionPlacement.node_for_relight(session, t)
  end

  test "node_for_relight is snapshot_lost when no ready node reports the snapshot" do
    t = table()
    # Node up but NOT reporting this session's snapshot (evicted out of band).
    put_node(t, "node-4", session_snapshots: [])

    session = %{node_id: "node-4", snapshot_ref: "sess-snap-gone"}
    assert {:error, :snapshot_lost} = SessionPlacement.node_for_relight(session, t)
  end

  test "node_for_relight is snapshot_lost when the recorded node is down (absent from the table)" do
    t = table()
    session = %{node_id: "node-dead", snapshot_ref: "sess-snap-1"}
    assert {:error, :snapshot_lost} = SessionPlacement.node_for_relight(session, t)
  end

  test "node_for_relight is snapshot_lost with no snapshot_ref on the row" do
    t = table()
    put_node(t, "node-4", [])
    assert {:error, :snapshot_lost} = SessionPlacement.node_for_relight(%{node_id: "node-4", snapshot_ref: nil}, t)
  end
end
