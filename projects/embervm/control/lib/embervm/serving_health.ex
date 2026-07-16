defmodule Embervm.ServingHealth do
  @moduledoc """
  Health ejection for serving instances: reconcile each live serving instance's
  fan-out membership against the daemon's health-probe fact
  (`NodeStatus.serving_vms[].healthy`, projected into `Embervm.NodeCapacity`).

  The daemon probes `GET {health_path}` on each live serving VM and reports the
  result; it never acts on it. The control plane is the actor: when the node
  reports an instance UNHEALTHY, this pulls its endpoint from the fan-out
  (`serving_unpublished` reason `unhealthy`, ETS state `published -> draining`),
  so Envoy stops routing to a VM that is failing its probe; when the node reports
  it HEALTHY again, this republishes it (`serving_published` reason `healthy`, ETS
  `draining -> published`), so a recovered VM re-enters the fan-out. Every flip
  triggers one `Embervm.EndpointPublisher.publish/1`, which re-derives the EDS
  assignment from the healthy-published set on its next (debounced) flush.

  ## correlation is by vm_id

  A `ServingVm` fact carries no instance id (a serving VM is not a session), so an
  instance is matched to its node fact by `vm_id`: the ServingStore row's `vm_id`
  (written at `serving_started`/adopted from node truth) keyed against the node's
  reported serving VMs. An instance whose vm_id the node does not report at all is
  left untouched here (a vanished VM is the adoption reconcile's business, Task 8,
  not health ejection's: absence is not the same signal as an unhealthy probe).

  ## idempotent + level-triggered

  This is a pure reconcile over current facts: running it twice is a no-op the
  second time (an already-ejected instance is `draining`, so the FSM's
  `published -> draining` edge is not re-taken; an already-published healthy one
  stays published via the `published -> published` self-edge, which the store
  treats as a benign republish). It is safe to run on every registry sweep. It
  performs NO node RPCs and reads only ETS facts, so a stale/empty capacity read
  simply ejects nothing (fail-safe: a missing node fact never spuriously ejects a
  live endpoint; only an explicit `healthy: false` does).
  """

  require Logger

  alias Embervm.{EndpointPublisher, NodeCapacity, ServingStore}

  @doc """
  Reconciles every live serving instance's fan-out membership against the node
  health facts, then requests a republish if anything changed. Returns the count
  of instances whose published/unpublished state this flipped (0 when nothing
  changed), so a sweep caller can log/emit it. `opts` carries the seams:
  `:store`, `:capacity_table`, `:publisher` (all default to the supervised
  singletons).
  """
  @spec reconcile(keyword()) :: non_neg_integer()
  def reconcile(opts \\ []) do
    store = Keyword.get(opts, :store, ServingStore)
    capacity_table = Keyword.get(opts, :capacity_table, NodeCapacity.table())
    publisher = Keyword.get(opts, :publisher, EndpointPublisher)

    health_by_vm = index_serving_health(capacity_table)

    flipped =
      ServingStore.all(store)
      |> Enum.reduce(0, fn instance, acc ->
        case reconcile_one(store, instance, health_by_vm) do
          :flipped -> acc + 1
          :noop -> acc
        end
      end)

    if flipped > 0 do
      # A fan-out membership change: ask the publisher to re-derive + re-push. The
      # publisher debounces, so N flips in one sweep coalesce into one PUT.
      EndpointPublisher.publish(publisher)
    end

    flipped
  end

  # vm_id -> healthy?, over every node's reported serving VMs. A vm_id reported by
  # more than one node (should not happen: a VM lives on one node) takes the last
  # writer; harmless, since the health signal is per-VM regardless of node.
  defp index_serving_health(capacity_table) do
    for fact <- NodeCapacity.all(capacity_table),
        vm <- Map.get(fact, :serving_vms, []) || [],
        is_binary(Map.get(vm, :vm_id)),
        into: %{} do
      {vm.vm_id, Map.get(vm, :healthy, false)}
    end
  end

  # Reconcile one instance against the node health index. Only two edges are
  # health-driven:
  #   * published + node-reports-unhealthy -> unpublish (reason unhealthy);
  #   * draining  + node-reports-healthy   -> republish (reason healthy), reusing
  #     the instance's known {ip, port} (a draining-but-healthy instance still
  #     holds its live VM endpoint; it was ejected only for a prior unhealthy blip).
  # Every other state (starting, banking, banked, relighting, terminal) is not a
  # health-ejection concern and is left to the lifecycle/adoption paths. An
  # instance whose vm_id the node does not report is skipped (absence != unhealthy).
  defp reconcile_one(store, instance, health_by_vm) do
    case Map.fetch(health_by_vm, instance.vm_id) do
      {:ok, healthy?} ->
        cond do
          instance.state == :published and not healthy? ->
            eject(store, instance)

          # !!! TASK 9 IMPLEMENTER, READ THIS BEFORE LANDING DRAIN-BEFORE-BANK !!!
          #
          # This republish (draining + node-reports-healthy -> published) is safe
          # TODAY only because NOTHING moves an instance to `draining` while its VM
          # is alive and healthy: in R3 PR-4 the sole path into `draining` is health
          # ejection itself (an UNHEALTHY probe), so a draining instance the node now
          # reports HEALTHY can only be one that recovered, and republishing it is
          # correct.
          #
          # Task 9's drain-before-bank BREAKS that assumption. The idle-bank sequence
          # is: unpublish (published -> draining, reason `drain`), wait `drainSeconds`,
          # then StopServing(BANK). During that drain window the instance is `draining`
          # with its {ip, port} STILL SET and its VM STILL UP and probing HEALTHY. This
          # health sweep would then REPUBLISH the very instance the bank is
          # deliberately draining, RACING the bank (re-adding an endpoint about to be
          # snapshotted-and-destroyed -> requests routed to a VM that vanishes).
          #
          # Task 9 MUST distinguish drain-for-HEALTH from drain-for-BANK before landing
          # drain-before-bank, e.g. a `drain_reason` on the ETS row that this branch
          # checks (republish only when drain_reason == :unhealthy), or a distinct
          # transient `bank_draining` state this branch excludes. Do NOT land the
          # idle-bank drain without gating this republish, or banks will flap.
          instance.state == :draining and healthy? and republishable?(instance) ->
            republish(store, instance)

          true ->
            :noop
        end

      :error ->
        :noop
    end
  end

  defp eject(store, instance) do
    case ServingStore.unpublish(store, instance.instance_id, :unhealthy) do
      {:ok, _} ->
        Logger.warning("embervm serving: ejected unhealthy instance",
          instance_id: instance.instance_id,
          workload: instance.workload,
          vm_id: instance.vm_id
        )

        :flipped

      {:error, reason} ->
        Logger.error("embervm serving: eject failed",
          instance_id: instance.instance_id,
          reason: inspect(reason)
        )

        :noop
    end
  end

  defp republish(store, instance) do
    case ServingStore.publish(store, instance.instance_id, instance.ip, instance.port, :healthy) do
      {:ok, _} ->
        Logger.info("embervm serving: republished recovered instance",
          instance_id: instance.instance_id,
          workload: instance.workload,
          vm_id: instance.vm_id
        )

        :flipped

      {:error, reason} ->
        Logger.error("embervm serving: republish failed",
          instance_id: instance.instance_id,
          reason: inspect(reason)
        )

        :noop
    end
  end

  # A draining instance can only be republished from health recovery if it still
  # holds a routable endpoint (it was ejected for health, not drained for a bank:
  # a bank-drain clears ip/port). Guards against republishing an endpoint-less row.
  defp republishable?(instance) do
    is_binary(instance.ip) and instance.ip != "" and is_integer(instance.port)
  end
end
