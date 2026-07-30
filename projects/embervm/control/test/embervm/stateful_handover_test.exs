defmodule Embervm.StatefulHandoverTest do
  @moduledoc """
  Exercises Embervm.StatefulHandover (#4119 slice 4) against a real
  StatefulStore + op-log and a real NodeCapacity table, with the three daemon
  verbs (export/restore/evict) injected as recording seams.

  The properties that matter are about what survives a refusal, not the happy
  path:

    * a refused export ABORTS with the anchor untouched (the ADR 011 gate is the
      only thing standing between a self-bumped divergent copy and it becoming
      authoritative on a peer);
    * a live instance refuses the move outright (moving a volume out from under
      a writable attach is the split brain the fence exists to prevent);
    * a refused source eviction still SUCCEEDS, because the anchor has already
      moved and the leftover is a stale copy rather than a competing writer.

  No test sleeps; the clock is injected.
  """
  use ExUnit.Case, async: false

  alias Embervm.{NodeCapacity, StatefulHandover, StatefulStore}
  alias Embervm.OpLog.SQLite

  defp start_stack(_opts \\ []) do
    suffix = System.unique_integer([:positive])
    cap_table = :"sfhocap_#{suffix}"
    NodeCapacity.create(cap_table)

    path = Path.join(System.tmp_dir!(), "embervm_statefulhandover_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = StatefulStore.start_link(name: nil, op_log: op_log, op_log_mod: SQLite)

    # Two reporting nodes. configured_id is what a dial resolves against.
    for node <- ["node-a", "node-b"] do
      NodeCapacity.put(cap_table, node, %{
        configured_id: node,
        node_id: node,
        max_live_vms: 8,
        live_vms: 0,
        workloads: %{},
        stateful_vms: [],
        stateful_bundles: []
      })
    end

    {:ok, calls} = Agent.start_link(fn -> [] end)
    %{store: store, op_log: op_log, cap_table: cap_table, calls: calls}
  end

  defp record(calls, tag, dial, result) do
    Agent.update(calls, &(&1 ++ [{tag, dial}]))
    result
  end

  defp calls(calls), do: Agent.get(calls, & &1)

  # Base options wiring the seams. Each verb records (tag, dial_id) so the test
  # can assert WHICH node each RPC went to, which is the part a move gets wrong.
  defp opts(ctx, overrides \\ []) do
    export = Keyword.get(overrides, :export, {:ok, %{skipped: false, generation: 7}})
    restore = Keyword.get(overrides, :restore, {:ok, %{}})
    evict = Keyword.get(overrides, :evict, {:ok, %{}})

    [
      store: ctx.store,
      capacity_table: ctx.cap_table,
      op_log: ctx.op_log,
      op_log_mod: SQLite,
      clock: fn -> 1_000 end,
      # The channel seam hands the dial id straight through so each verb sees it.
      channel_fun: fn dial -> {:ok, dial} end,
      export_fun: fn dial, _req -> record(ctx.calls, :export, dial, export) end,
      restore_fun: fn dial, _req -> record(ctx.calls, :restore, dial, restore) end,
      evict_fun: fn dial, _req -> record(ctx.calls, :evict, dial, evict) end
    ]
  end

  defp anchor(ctx, workload) do
    case StatefulStore.get_volume(ctx.store, workload) do
      nil -> nil
      volume -> Map.get(volume, :node_id)
    end
  end

  defp seed_volume(ctx, workload, node) do
    {:ok, _} =
      StatefulStore.create_volume(ctx.store, workload, %{
        node_id: node,
        generation: 1,
        size_bytes: 1024,
        allocated_bytes: 1024
      })

    :ok
  end

  test "moves a banked volume, re-anchors it, and evicts the source copy" do
    ctx = start_stack()
    seed_volume(ctx, "demo-postgres", "node-a")

    assert {:ok, moved} = StatefulHandover.move("demo-postgres", "node-b", opts(ctx))

    assert moved.from == "node-a"
    assert moved.to == "node-b"
    assert moved.generation == 7
    assert moved.source_evicted

    # The anchor is the whole point: the next wake must plan against node-b.
    assert anchor(ctx, "demo-postgres") == "node-b"

    # Export off the SOURCE, restore onto the TARGET, evict the SOURCE. Getting
    # either direction backwards would still "succeed" without this assertion.
    assert calls(ctx.calls) == [{:export, "node-a"}, {:restore, "node-b"}, {:evict, "node-a"}]
  end

  test "a refused export aborts the move and leaves the anchor untouched" do
    ctx = start_stack()
    seed_volume(ctx, "demo-postgres", "node-a")

    # skipped: true is what noded returns for an unblessed generation (ADR 011)
    # or a store copy that is already newer (#4111).
    result =
      StatefulHandover.move(
        "demo-postgres",
        "node-b",
        opts(ctx, export: {:ok, %{skipped: true, generation: 0}})
      )

    assert result == {:error, :source_export_refused}
    assert anchor(ctx, "demo-postgres") == "node-a"
    # Nothing was restored onto the target: a peer must never receive bytes the
    # control plane never blessed.
    assert calls(ctx.calls) == [{:export, "node-a"}]
  end

  test "refuses to move while a live instance exists" do
    ctx = start_stack()
    seed_volume(ctx, "demo-postgres", "node-a")

    {:ok, _instance} =
      StatefulStore.start(ctx.store, %{
        instance_id: "sf-live-1",
        tenant: "homelab",
        principal: "p1",
        workload: "demo-postgres",
        node_id: "node-a",
        vm_id: "vm-1",
        generation: 1
      })

    assert {:error, {:not_banked, live}} =
             StatefulHandover.move("demo-postgres", "node-b", opts(ctx))

    assert live > 0
    assert anchor(ctx, "demo-postgres") == "node-a"
    assert calls(ctx.calls) == []
  end

  test "refuses a move onto the node that already holds the volume" do
    ctx = start_stack()
    seed_volume(ctx, "demo-postgres", "node-a")

    assert StatefulHandover.move("demo-postgres", "node-a", opts(ctx)) ==
             {:error, :already_anchored}

    assert calls(ctx.calls) == []
  end

  test "refuses a target that is not reporting" do
    ctx = start_stack()
    seed_volume(ctx, "demo-postgres", "node-a")

    assert StatefulHandover.move("demo-postgres", "node-gone", opts(ctx)) ==
             {:error, {:node_not_reporting, "node-gone"}}

    assert anchor(ctx, "demo-postgres") == "node-a"
    assert calls(ctx.calls) == []
  end

  test "a workload with no volume is a clean refusal, not a crash" do
    ctx = start_stack()

    assert StatefulHandover.move("nonexistent", "node-b", opts(ctx)) == {:error, :no_volume}
  end

  # The shape production actually uses, and the one the first live drill got
  # wrong: NodeCapacity keys facts by INSTANCE id ("node-4/<uuid>") and a volume
  # row anchors on one, while an operator naming a target says "node-4". A bare
  # name cannot be an instance id because the "/" would not survive a URL path
  # segment, so both forms must resolve.
  defp put_brick(ctx, node, uuid, updated_at) do
    NodeCapacity.put(ctx.cap_table, {node, uuid}, %{
      node_id: "#{node}/#{uuid}",
      updated_at: updated_at,
      max_live_vms: 8,
      live_vms: 0,
      workloads: %{},
      stateful_vms: [],
      stateful_bundles: []
    })
  end

  test "resolves a bare node name to a reporting instance and anchors on the instance id" do
    ctx = start_stack()
    put_brick(ctx, "node-c", "aaaa", 10)
    put_brick(ctx, "node-d", "bbbb", 20)
    seed_volume(ctx, "demo-postgres", "node-c/aaaa")

    assert {:ok, moved} = StatefulHandover.move("demo-postgres", "node-d", opts(ctx))

    assert moved.from == "node-c/aaaa"
    # Resolved, not the shorthand that was typed.
    assert moved.to == "node-d/bbbb"

    # The anchor MUST be the instance id: StatefulManager resolves it through an
    # exact NodeCapacity match, so a bare name here would make every subsequent
    # wake fail :volume_node_gone.
    assert anchor(ctx, "demo-postgres") == "node-d/bbbb"

    assert calls(ctx.calls) == [
             {:export, "node-c/aaaa"},
             {:restore, "node-d/bbbb"},
             {:evict, "node-c/aaaa"}
           ]
  end

  test "refuses a bare-name move onto the node that already holds the volume" do
    ctx = start_stack()
    put_brick(ctx, "node-c", "aaaa", 10)
    seed_volume(ctx, "demo-postgres", "node-c/aaaa")

    # Comparing the raw strings would MISS this ("node-c/aaaa" != "node-c") and
    # export and restore the volume onto the node that already holds it.
    assert StatefulHandover.move("demo-postgres", "node-c", opts(ctx)) ==
             {:error, :already_anchored}

    assert calls(ctx.calls) == []
  end

  test "an op-log failure aborts before anything is dispatched" do
    ctx = start_stack()
    seed_volume(ctx, "demo-postgres", "node-a")

    # The started op is the record that adjudicates a crash mid-move, so a move
    # that cannot record it must not begin. Note the EXIT rather than a raise:
    # a GenServer.call to a name that is not running exits, which is exactly how
    # the first live drill escaped as an unhandled 500 with an empty body.
    boom = fn _op_log, _op -> exit(:noproc) end

    assert {:error, {:op_log_unavailable, _}} =
             StatefulHandover.move(
               "demo-postgres",
               "node-b",
               Keyword.put(opts(ctx), :append_fun, boom)
             )

    assert anchor(ctx, "demo-postgres") == "node-a"
    assert calls(ctx.calls) == []
  end

  test "a refused source eviction still completes the move" do
    ctx = start_stack()
    seed_volume(ctx, "demo-postgres", "node-a")

    # EvictArtifact refuses a volume still paired with a local bundle (standing
    # decision 8), which is the COMMON case right after a bank. Failing the move
    # on it would reject most otherwise-correct handovers.
    assert {:ok, moved} =
             StatefulHandover.move(
               "demo-postgres",
               "node-b",
               opts(ctx, evict: {:error, :still_paired})
             )

    refute moved.source_evicted
    assert anchor(ctx, "demo-postgres") == "node-b"
  end
end
