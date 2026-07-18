defmodule Embervm.GroupStoreTest do
  @moduledoc """
  Exercises Embervm.GroupStore against a real op-log: the singleton create gate,
  write-through transitions (durable op BEFORE ETS visibility), the member lifecycle
  (start -> running -> bank), the degraded FLAG (a member unhealthy on a running
  group), illegal-transition raise-through, set-completeness derivation + eager
  eviction of a partial set, the entry-endpoint fact, and a rebuild-from-projection
  reproducing exact instance + member state.
  """
  use ExUnit.Case, async: true

  alias Embervm.GroupStore
  alias Embervm.OpLog.SQLite

  defp start_store(_opts \\ []) do
    suffix = System.unique_integer([:positive])
    path = Path.join(System.tmp_dir!(), "embervm_groupstore_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, counter} = Agent.start_link(fn -> 1_000 end)
    clock = fn -> Agent.get_and_update(counter, fn n -> {n, n + 1} end) end

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = GroupStore.start_link(name: nil, op_log: op_log, clock: clock)
    %{store: store, op_log: op_log, path: path}
  end

  defp create(ctx, instance_id, workload \\ "grp-a", extra \\ %{}) do
    attrs =
      Map.merge(
        %{
          instance_id: instance_id,
          tenant: "homelab",
          principal: "system:group:#{workload}",
          workload: workload,
          node_id: "node-4",
          subnet_cidr: "10.101.0.0/24",
          entry_member: "leader",
          entry_port: 8080,
          listen_port: 5410,
          secret: "s3cr3t"
        },
        extra
      )

    GroupStore.create(ctx.store, attrs)
  end

  defp member(ctx, instance_id, name, index, extra \\ %{}) do
    fields = Map.merge(%{member_name: name, member_index: index, vm_id: "vm-#{name}", ip: "10.101.0.#{10 + index}"}, extra)
    GroupStore.member_started(ctx.store, instance_id, fields)
  end

  test "create records a group in :creating and inserts the instance row" do
    ctx = start_store()
    assert {:ok, instance} = create(ctx, "g-1")
    assert instance.state == :creating
    assert instance.subnet_cidr == "10.101.0.0/24"
    assert instance.entry_member == "leader"
    assert {:ok, ^instance} = GroupStore.get(ctx.store, "g-1")
  end

  test "the singleton gate refuses a second LIVE instance for the workload without appending" do
    ctx = start_store()
    assert {:ok, _} = create(ctx, "g-1", "grp-a")
    assert {:error, :already_live} = create(ctx, "g-2", "grp-a")
    # A different workload is fine.
    assert {:ok, _} = create(ctx, "g-3", "grp-b")
  end

  test "a banked instance does NOT block a fresh create (banked is not live)" do
    ctx = start_store()
    assert {:ok, _} = create(ctx, "g-1", "grp-a")
    # Drive to banked: members up -> running -> bank -> bank_ready.
    {:ok, _} = member(ctx, "g-1", "leader", 0)
    {:ok, _} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)
    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)
    {:ok, _} = GroupStore.bank_ready(ctx.store, "g-1", "set-1", [%{name: "leader", snapshot_ref: "snap-l"}])

    # Now a banked-only workload can create a fresh cold boot.
    assert {:ok, _} = create(ctx, "g-2", "grp-a")
  end

  test "member lifecycle: started -> running marks healthy -> bank stamps snapshot + clears VM" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, m} = member(ctx, "g-1", "leader", 0)
    assert m.state == "starting"
    assert m.healthy == false

    {:ok, inst} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)
    assert inst.state == :running
    assert inst.entry_ip == "10.0.0.9"
    assert inst.entry_port_published == 30012

    [leader] = GroupStore.members(ctx.store, "g-1")
    assert leader.healthy == true

    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)
    {:ok, banked} = GroupStore.bank_ready(ctx.store, "g-1", "set-1", [%{name: "leader", snapshot_ref: "snap-l"}])
    assert banked.state == :banked
    assert banked.set_id == "set-1"
    assert banked.entry_ip == nil

    [leader] = GroupStore.members(ctx.store, "g-1")
    assert leader.snapshot_ref == "snap-l"
    assert leader.vm_id == nil
    assert leader.state == "banked"
  end

  test "member_started records the daemon-reported entry endpoint (ETS-only)" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, m} = member(ctx, "g-1", "leader", 0, %{endpoint_ip: "10.42.1.95", endpoint_port: 36_443})
    assert m.endpoint_ip == "10.42.1.95"
    assert m.endpoint_port == 36_443

    # A member reported without one carries the zero values, not nil.
    {:ok, w} = member(ctx, "g-1", "worker-0", 1)
    assert w.endpoint_ip == ""
    assert w.endpoint_port == 0
  end

  test "member_started against a terminal instance is refused (zombie worker guard)" do
    # A StartGroupMember that returns AFTER the wake bound expired and the instance
    # was force-rolled must not resurrect member rows on the destroyed instance.
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")

    {:ok, _} =
      GroupStore.transition(ctx.store, "g-1", :destroy, :group_destroyed, %{reason: "forced_roll"}, %{})

    assert {:error, {:instance_terminal, :destroyed}} = member(ctx, "g-1", "leader", 0)
    assert GroupStore.members(ctx.store, "g-1") == []
  end

  test "the degraded flag: one member unhealthy on a running group names it; recovery clears it" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, _} = member(ctx, "g-1", "leader", 0)
    {:ok, _} = member(ctx, "g-1", "worker", 1)
    {:ok, _} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)

    assert GroupStore.degraded?(ctx.store, "grp-a") == false

    {:ok, inst} = GroupStore.set_member_health(ctx.store, "g-1", "worker", false)
    assert inst.state == :running
    assert inst.degraded_member == "worker"
    assert GroupStore.degraded?(ctx.store, "grp-a") == {true, "worker"}

    {:ok, inst} = GroupStore.set_member_health(ctx.store, "g-1", "worker", true)
    assert inst.degraded_member == nil
    assert GroupStore.degraded?(ctx.store, "grp-a") == false
  end

  test "an illegal transition raises out of transition/6 as an error (never corrupts ETS)" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    # :bank_ready is illegal from :creating (only from :banking).
    assert {:error, {:illegal_transition, :creating, :bank_ready}} =
             GroupStore.transition(ctx.store, "g-1", :bank_ready, :group_banked, %{}, %{})

    # ETS is untouched: still creating.
    assert {:ok, %{state: :creating}} = GroupStore.get(ctx.store, "g-1")
  end

  test "the entry-endpoint fact is the running entry {ip, port}, nil otherwise" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, _} = member(ctx, "g-1", "leader", 0)
    assert GroupStore.entry_endpoint(ctx.store, "grp-a") == nil

    {:ok, _} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)
    assert GroupStore.entry_endpoint(ctx.store, "grp-a") == %{ip: "10.0.0.9", port: 30012}

    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)
    {:ok, _} = GroupStore.bank_ready(ctx.store, "g-1", "set-1", [%{name: "leader", snapshot_ref: "snap-l"}])
    assert GroupStore.entry_endpoint(ctx.store, "grp-a") == nil
  end

  test "set-completeness: a banked set missing a member's bundle is eagerly evicted (partial_set)" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, _} = member(ctx, "g-1", "leader", 0)
    {:ok, _} = member(ctx, "g-1", "worker", 1)
    {:ok, _} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)
    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)

    {:ok, _} =
      GroupStore.bank_ready(ctx.store, "g-1", "set-1", [
        %{name: "leader", snapshot_ref: "snap-l"},
        %{name: "worker", snapshot_ref: "snap-w"}
      ])

    # A COMPLETE reported set (both members) survives.
    complete = %{"g-1" => MapSet.new(["leader", "worker"])}
    assert GroupStore.evict_partial_sets(ctx.store, complete) == []
    assert {:ok, %{set_id: "set-1"}} = GroupStore.get(ctx.store, "g-1")

    # A PARTIAL reported set (worker's bundle gone) is evicted: set_id cleared.
    partial = %{"g-1" => MapSet.new(["leader"])}
    assert GroupStore.evict_partial_sets(ctx.store, partial) == ["g-1"]
    assert {:ok, %{set_id: nil, state: :banked}} = GroupStore.get(ctx.store, "g-1")
  end

  test "a banked instance ABSENT from the reported sets is treated as partial (evicted)" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, _} = member(ctx, "g-1", "leader", 0)
    {:ok, _} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)
    {:ok, _} = GroupStore.mark(ctx.store, "g-1", :bank)
    {:ok, _} = GroupStore.bank_ready(ctx.store, "g-1", "set-1", [%{name: "leader", snapshot_ref: "snap-l"}])

    assert GroupStore.evict_partial_sets(ctx.store, %{}) == ["g-1"]
    assert {:ok, %{set_id: nil}} = GroupStore.get(ctx.store, "g-1")
  end

  test "held_subnets reports every live-or-banked instance's /24, excluding terminal ones" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1", "grp-a", %{subnet_cidr: "10.101.0.0/24"})
    {:ok, _} = create(ctx, "g-2", "grp-b", %{subnet_cidr: "10.101.1.0/24"})

    held = GroupStore.held_subnets(ctx.store)
    assert MapSet.member?(held, "10.101.0.0/24")
    assert MapSet.member?(held, "10.101.1.0/24")

    # Destroy g-2: its subnet is freed.
    {:ok, _} = GroupStore.transition(ctx.store, "g-2", :destroy, :group_destroyed, %{reason: "test"}, %{})
    held = GroupStore.held_subnets(ctx.store)
    refute MapSet.member?(held, "10.101.1.0/24")
    assert MapSet.member?(held, "10.101.0.0/24")
  end

  test "rebuild from the durable projection reproduces exact instance + member state" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, _} = member(ctx, "g-1", "leader", 0)
    {:ok, _} = member(ctx, "g-1", "worker", 1)
    {:ok, _} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)

    # A fresh store over the SAME op-log rebuilds from the projection alone.
    {:ok, store2} = GroupStore.start_link(name: nil, op_log: ctx.op_log, clock: fn -> 9_000 end)

    assert {:ok, inst} = GroupStore.get(store2, "g-1")
    assert inst.state == :running
    assert inst.entry_member == "leader"
    # A durably-running group is whole (group_running marked every member healthy
    # durably), so no degraded flag on rebuild.
    assert inst.degraded_member == nil
    # The entry endpoint is reconstructed from durable facts alone: the entry
    # member's tap ip + the group's entry.port (Task 7 adoption re-derives the DNAT
    # {pod IP, vmPort} on the next sweep).
    assert GroupStore.entry_endpoint(store2, "grp-a") == %{ip: "10.101.0.10", port: 8080}

    members = GroupStore.members(store2, "g-1")
    assert length(members) == 2
    assert Enum.all?(members, & &1.healthy)
  end

  test "an ETS-only member-health flip is LOSSY: it does not survive a rebuild" do
    ctx = start_store()
    {:ok, _} = create(ctx, "g-1")
    {:ok, _} = member(ctx, "g-1", "leader", 0)
    {:ok, _} = member(ctx, "g-1", "worker", 1)
    {:ok, _} = GroupStore.publish(ctx.store, "g-1", "10.0.0.9", 30012)

    # set_member_health is an ETS-only lossy node fact (no op-log append), exactly
    # like the stateful set_health/2: the degraded flag is live-visible...
    {:ok, inst} = GroupStore.set_member_health(ctx.store, "g-1", "worker", false)
    assert inst.degraded_member == "worker"

    # ...but a rebuild from the durable projection (which was never told the member
    # went unhealthy) sees a whole group. A durable degrade rides a group_degraded op
    # (Task 7's health/adoption path), not this ETS-only flip.
    {:ok, store2} = GroupStore.start_link(name: nil, op_log: ctx.op_log, clock: fn -> 9_000 end)
    assert {:ok, %{degraded_member: nil}} = GroupStore.get(store2, "g-1")
  end
end
