defmodule Embervm.ServingHealthTest do
  @moduledoc """
  Exercises Embervm.ServingHealth: the health-ejection reconcile that flips a
  serving instance's fan-out membership from the node's health-probe fact. Proves
  eject-on-unhealthy (serving_unpublished reason unhealthy), republish-on-recovery,
  idempotence over a repeated sweep, that an instance whose vm_id the node does
  NOT report is left untouched (absence != unhealthy), and that a change triggers
  the publisher.
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, ServingHealth, ServingStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite

  defp start_stack do
    suffix = System.unique_integer([:positive])
    cap_table = :"hcap_#{suffix}"
    cat_table = :"hcat_#{suffix}"

    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    path = Path.join(System.tmp_dir!(), "embervm_servinghealth_test_#{suffix}.db")
    on_exit(fn -> File.rm_rf!(path) end)

    {:ok, op_log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = ServingStore.start_link(name: nil, op_log: op_log, clock: fn -> 1_000 end)

    %{store: store, cap_table: cap_table, op_log: op_log}
  end

  # ServingHealth.reconcile calls EndpointPublisher.publish(publisher), a
  # GenServer.cast; a tiny GenServer that counts those casts lets a test assert the
  # reconcile asks for a republish exactly when something flipped.
  defp start_counting_publisher do
    {:ok, pid} = Embervm.ServingHealthTest.CountingPublisher.start_link()
    pid
  end

  defp put_serving_vm(ctx, node_id, vm_id, healthy?) do
    existing =
      case NodeCapacity.fetch(ctx.cap_table, node_id) do
        {:ok, f} -> Map.get(f, :serving_vms, [])
        :error -> []
      end

    vm = %{vm_id: vm_id, workload: "wl-a", ip: "10.99.0.5", port: 8080, healthy: healthy?, last_probe_unix_ms: 1}
    others = Enum.reject(existing, &(&1.vm_id == vm_id))

    NodeCapacity.put(ctx.cap_table, node_id, %{
      configured_id: node_id,
      node_id: node_id,
      serving_subnet_cidr: "10.99.0.0/24",
      serving_vms: [vm | others],
      serving_snapshots: []
    })
  end

  defp start_published(ctx, instance_id, vm_id) do
    {:ok, _} =
      ServingStore.start(ctx.store, %{
        instance_id: instance_id,
        tenant: "homelab",
        principal: "p1",
        workload: "wl-a",
        node_id: "node-4",
        vm_id: vm_id,
        ip: "10.99.0.5",
        port: 8080
      })

    {:ok, _} = ServingStore.publish(ctx.store, instance_id, "10.99.0.5", 8080, :started)
  end

  test "an unhealthy node fact ejects a published instance (published -> draining)" do
    ctx = start_stack()
    pub = start_counting_publisher()
    start_published(ctx, "srv-1", "vm-1")
    put_serving_vm(ctx, "node-4", "vm-1", false)

    flipped = ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub)
    assert flipped == 1

    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :draining
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []

    # The durable audit record: serving_unpublished landed.
    {:ok, [row]} = SQLite.load_serving_instances(ctx.op_log)
    assert row.state == "draining"

    # A change was flipped, so the publisher was asked to republish.
    assert Embervm.ServingHealthTest.CountingPublisher.count(pub) == 1
  end

  test "recovery republishes a drained-for-health instance (draining -> published)" do
    ctx = start_stack()
    pub = start_counting_publisher()
    start_published(ctx, "srv-1", "vm-1")

    # Eject on unhealthy.
    put_serving_vm(ctx, "node-4", "vm-1", false)
    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 1

    # Recover: healthy again.
    put_serving_vm(ctx, "node-4", "vm-1", true)
    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 1

    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :published
    assert instance.healthy
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == [%{ip: "10.99.0.5", port: 8080}]
  end

  test "reconcile is idempotent: a second sweep over stable facts flips nothing" do
    ctx = start_stack()
    pub = start_counting_publisher()
    start_published(ctx, "srv-1", "vm-1")
    put_serving_vm(ctx, "node-4", "vm-1", false)

    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 1
    # Second sweep: already draining, nothing to flip.
    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 0
    # The publisher was asked to republish only for the real change.
    assert Embervm.ServingHealthTest.CountingPublisher.count(pub) == 1
  end

  test "an instance whose vm_id the node does not report is left untouched (absence != unhealthy)" do
    ctx = start_stack()
    pub = start_counting_publisher()
    start_published(ctx, "srv-1", "vm-1")

    # Node reports a DIFFERENT vm, not srv-1's. A vanished VM is adoption's job, not
    # health ejection's: srv-1 stays published.
    put_serving_vm(ctx, "node-4", "vm-other", true)

    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 0
    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :published
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == [%{ip: "10.99.0.5", port: 8080}]
  end

  test "a bank-drain is NOT republished by a health sweep even when the VM probes healthy" do
    # The drain-for-bank health race (the Task 9 carry-forward): the idle-bank sweep
    # unpublishes a still-ALIVE, still-HEALTHY VM (reason :bank) and waits drainSeconds
    # before StopServing(BANK). A health sweep in that window sees draining + healthy
    # and must NOT republish it (that would race the bank). The drain_reason :bank on
    # the ETS row gates the republish.
    ctx = start_stack()
    pub = start_counting_publisher()
    start_published(ctx, "srv-1", "vm-1")

    # The idle-bank sweep's first step: unpublish reason :bank (draining, drain_reason
    # :bank, but ip/port STILL SET because the VM is alive during the drain window).
    {:ok, drained} = ServingStore.unpublish(ctx.store, "srv-1", :bank)
    assert drained.state == :draining
    assert drained.drain_reason == :bank
    assert drained.ip == "10.99.0.5"

    # The VM is still up and probing HEALTHY (the bank has not run yet).
    put_serving_vm(ctx, "node-4", "vm-1", true)

    # A health sweep must flip NOTHING: the bank owns this instance.
    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 0

    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :draining, "the bank-drain must not be republished"
    assert ServingStore.published_endpoints(ctx.store, "wl-a") == []
    assert Embervm.ServingHealthTest.CountingPublisher.count(pub) == 0
  end

  test "a health-drain (reason :unhealthy) IS still republished on recovery (regression guard)" do
    # The complement of the bank-drain test: an instance drained for HEALTH must still
    # republish on recovery, so the drain_reason gate did not break health ejection.
    ctx = start_stack()
    pub = start_counting_publisher()
    start_published(ctx, "srv-1", "vm-1")

    put_serving_vm(ctx, "node-4", "vm-1", false)
    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 1
    {:ok, ejected} = ServingStore.get(ctx.store, "srv-1")
    assert ejected.drain_reason == :unhealthy

    put_serving_vm(ctx, "node-4", "vm-1", true)
    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 1
    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :published
  end

  test "an empty capacity table ejects nothing (fail-safe: no facts, no eject)" do
    ctx = start_stack()
    pub = start_counting_publisher()
    start_published(ctx, "srv-1", "vm-1")

    # No serving_vms reported at all.
    assert ServingHealth.reconcile(store: ctx.store, capacity_table: ctx.cap_table, publisher: pub) == 0
    {:ok, instance} = ServingStore.get(ctx.store, "srv-1")
    assert instance.state == :published
  end

  defmodule CountingPublisher do
    @moduledoc "A tiny GenServer that counts publish/1 casts, for the reconcile assertions."
    use GenServer

    def start_link, do: GenServer.start_link(__MODULE__, 0)
    def count(pid), do: GenServer.call(pid, :count)

    @impl true
    def init(n), do: {:ok, n}

    # ServingHealth calls EndpointPublisher.publish(pub), which is
    # GenServer.cast(pub, :publish). Count it.
    @impl true
    def handle_cast(:publish, n), do: {:noreply, n + 1}

    @impl true
    def handle_call(:count, _from, n), do: {:reply, n, n}
  end
end
