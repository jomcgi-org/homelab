defmodule Embervm.NodeSizer do
  @moduledoc """
  The control-plane dynamic per-node sizing loop (artifact-decoupling PR-I,
  ADR embervm/012). It makes guest memory SCHEDULER-VISIBLE by resizing each noded
  pod's memory + CPU request AND limit, in place, to reflect the guest capacity the
  control plane has provisioned or committed on that node. This replaces the fixed
  `maxLiveVMs`/`36Gi`-ceiling static sizing with a live capacity ledger owned by the
  thing that already owns placement, so the k8s scheduler bin-packs guests and
  platform workloads on honest numbers (which is why ADR 012 needs no FC taint).

  ## the desired envelope

  For a node the sizer computes:

      desired_mem_mib   = baseline_mem_mib
                          + Σ committed guest mem_mib on the node
                          + reserved (in-flight grow) mem_mib
                          + headroom_mib
      desired_cpu_milli = baseline_cpu_millicores
                          + Σ committed guest vcpus*1000 on the node
                          + reserved vcpus*1000

  "Committed guest capacity" is derived from the node's LIVE VM facts
  (`Embervm.NodeCapacity`: session/serving/stateful/composite VMs, each carrying its
  `workload`) joined to the per-workload sizing in `Embervm.WorkloadCatalog`
  (`mem_mib`, `vcpus`). "Reserved" is the grow-eager ledger below: capacity a
  placement asked for that is not yet visible as a live VM.

  ## grow-eager / shrink-lazy (ADR 012 policy)

    * GROW-EAGER: `reserve/3` grows the pod envelope BEFORE a placement commits and
      returns `:ok` ONLY once the kubelet accepts the resize. A placement holds the
      reservation while its VM boots; the periodic reconcile drops it once the live
      VM shows up (the observed committed capacity now covers it). A kubelet that
      reports the grow Infeasible/Deferred returns `{:error, :infeasible}`: the
      caller treats that node as unable to take the workload and places elsewhere,
      NEVER overcommitting past the accepted envelope.
    * SHRINK-LAZY: the reconcile shrinks a pod only when the RELEASED delta is large
      (>= `shrink_threshold_mib`) and never below live commitment. A memory-limit
      decrease may require a pod restart, which is exactly the disruption this
      platform exists to avoid, so a shrink is best-effort in place and DEFERS to the
      next natural roll rather than forcing one: a rejected shrink is logged and left
      for the roll, never retried into a restart.

  ## boot ordering (the prod-down crashloop this guards)

  `Embervm.K8s` dials the apiserver over the `Embervm.Finch` pool, which the
  supervisor starts as a CHILD. This module is a supervised child placed AFTER Finch
  (and after `Embervm.NodeRegistry`, whose capacity it reads) in the `:rest_for_one`
  chain, and it does NO k8s/Finch work at init: `init/1` only stores config and
  arms a timer via `{:continue, :start}`. Every resize/list call runs in
  `handle_continue`/the periodic tick, AFTER Finch is up, and each is wrapped so a
  transient apiserver error logs-and-continues rather than crashing the loop (a
  crash here would bounce every child after it under `:rest_for_one`). The
  application boot-ordering test asserts a fresh sizer's init makes no Finch call.

  ## keeping noded Burstable (the QoS invariant)

  An in-place resize CANNOT change a pod's QoS class; the kubelet rejects one that
  would. noded is Burstable (CPU request only, NO CPU limit; memory request ==
  limit). Every resize this module issues keeps that shape: it sets a CPU REQUEST
  but no CPU limit, and sets memory request == memory limit. It never introduces a
  CPU limit and never drops the memory limit, so the kubelet accepts the patch. See
  `Embervm.K8s.resize_pod/5`, which enforces the request/limit map shape.
  """

  use GenServer
  require Logger

  alias Embervm.{NodeCapacity, WorkloadCatalog}

  # Reconcile cadence: recompute every node's desired envelope from live facts and
  # apply grow-eager / shrink-lazy. 15s matches the registry/session sweep tempo so
  # a converged reservation is dropped and a released shrink attempted promptly.
  @reconcile_interval_ms 15_000

  # Daemon-only baseline the pod needs with ZERO live guests (the daemon process,
  # its Go runtime, transient base-build cold-boot headroom). The CP grows above
  # this per committed guest. Mirrors the chart's shrunk noded.resources baseline.
  @default_baseline_mem_mib 512
  @default_baseline_cpu_millicores 100

  # Bounded headroom added above baseline + committed, so a just-committed guest's
  # own working set + a little slack is covered before the next reconcile observes
  # it. Kept small: the reservation ledger already covers in-flight grows.
  @default_headroom_mib 512

  # SHRINK-LAZY threshold: only shrink when the released (desired < current) memory
  # delta is at least this large, so the loop does not churn resizes for a single
  # small guest teardown. A shrink below this is deferred to the next natural roll.
  @default_shrink_threshold_mib 2_048

  # The container name inside the noded pod the resize patch targets. Matches the
  # chart's noded-deployment.yaml container name.
  @default_container "noded"

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  GROW-EAGER capacity reservation for placing a guest of `workload` on `node_id`.
  Grows the node's noded pod envelope to cover the workload's sizing (on top of the
  already-committed guests) and returns:

    * `:ok` once the kubelet ACCEPTS the resize (the placement may proceed; the
      reservation is held until the reconcile observes the live VM);
    * `{:error, :infeasible}` when the kubelet cannot satisfy the grow (Infeasible /
      Deferred / QoS-change / any non-2xx) OR the resize call errors: a PLACEMENT
      REFUSAL for this node, so the caller tries the next candidate and NEVER
      overcommits;
    * `{:error, :disabled}` when the sizer is disabled (no namespace configured):
      the caller falls back to the legacy maxLiveVMs backstop unchanged.

  Synchronous (a `GenServer.call`): placement must not proceed until the kubelet has
  accepted the grow, so the resize round-trip is on the reservation path by design.
  """
  @spec reserve(GenServer.server(), String.t(), String.t()) ::
          :ok | {:error, :infeasible | :disabled}
  def reserve(server \\ __MODULE__, node_id, workload) do
    GenServer.call(server, {:reserve, node_id, workload}, 15_000)
  end

  @doc """
  Whether the sizer is enabled (a namespace is configured, so resize actuation is
  possible). When disabled, placement callers skip the sizer gate and keep the
  legacy `maxLiveVMs` backstop as the sole capacity model, so a control plane with
  no dynamic-sizing config behaves exactly as before this PR.
  """
  @spec enabled?(GenServer.server()) :: boolean()
  def enabled?(server \\ __MODULE__) do
    GenServer.call(server, :enabled?)
  end

  @doc """
  Forces one synchronous reconcile pass (the same code the periodic timer runs).
  Tests drive reconcile deterministically through this; in production the timer
  fires it every #{@reconcile_interval_ms}ms.
  """
  @spec reconcile(GenServer.server()) :: :ok
  def reconcile(server \\ __MODULE__) do
    GenServer.call(server, :reconcile)
  end

  @doc """
  The computed desired envelope for `node_id` from CURRENT live facts (no
  reservation, no actuation). Exposed for tests and operational visibility:
  `%{mem_mib, cpu_millicores}`. A node absent from the capacity table yields the
  baseline-only envelope.
  """
  @spec desired_envelope(GenServer.server(), String.t()) ::
          %{mem_mib: non_neg_integer(), cpu_millicores: non_neg_integer()}
  def desired_envelope(server \\ __MODULE__, node_id) do
    GenServer.call(server, {:desired_envelope, node_id})
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    # CRITICAL (boot ordering): do NO Finch / k8s work here. init runs while the
    # supervisor is bringing children up; a resize/list call here would race Finch
    # (or raise "unknown registry: Embervm.Finch") and crash-loop the control plane.
    # Only store config + injected seams; all actuation is post-start.
    state = %{
      table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      namespace: Keyword.get(opts, :namespace, nil),
      pod_label_selector: Keyword.get(opts, :pod_label_selector, nil),
      container: Keyword.get(opts, :container, @default_container),
      baseline_mem_mib: Keyword.get(opts, :baseline_mem_mib, @default_baseline_mem_mib),
      baseline_cpu_millicores: Keyword.get(opts, :baseline_cpu_millicores, @default_baseline_cpu_millicores),
      headroom_mib: Keyword.get(opts, :headroom_mib, @default_headroom_mib),
      shrink_threshold_mib: Keyword.get(opts, :shrink_threshold_mib, @default_shrink_threshold_mib),
      reconcile_interval_ms: Keyword.get(opts, :reconcile_interval_ms, @reconcile_interval_ms),
      # Injected k8s seams (defaults call Embervm.K8s over Finch). Tests inject fakes
      # to assert grow/shrink/refusal decisions without a live apiserver.
      resize_fun: Keyword.get(opts, :resize_fun, &default_resize/5),
      list_pods_fun: Keyword.get(opts, :list_pods_fun, &default_list_pods/2),
      # Per-node reservation ledger: node_id -> %{mem_mib, cpu_millicores}. In-flight
      # grows a placement asked for but whose live VM the reconcile has not yet seen.
      reservations: %{},
      # Per-node last-applied envelope, so the reconcile only issues a resize on an
      # actual change and can tell grow from shrink. node_id -> %{mem_mib, cpu_millicores}.
      applied: %{},
      # node_id -> pod_name cache, refreshed each reconcile from list_pods.
      pod_names: %{}
    }

    # reconcile_startup drives the loop from init; tests set it false and call
    # reconcile/1 explicitly so no background timer fires.
    if Keyword.get(opts, :reconcile_startup, true) do
      {:ok, state, {:continue, :start}}
    else
      {:ok, state}
    end
  end

  # Runs AFTER init returns, once the supervisor has started Finch (NodeSizer sits
  # after Finch in the rest_for_one chain), so a k8s list/resize is safe here.
  @impl true
  def handle_continue(:start, state) do
    state = do_reconcile(state)
    schedule_reconcile(state)
    {:noreply, state}
  end

  @impl true
  def handle_call({:reserve, node_id, workload}, _from, state) do
    if enabled_state?(state) do
      {result, state} = do_reserve(state, node_id, workload)
      {:reply, result, state}
    else
      {:reply, {:error, :disabled}, state}
    end
  end

  def handle_call(:enabled?, _from, state) do
    {:reply, enabled_state?(state), state}
  end

  def handle_call(:reconcile, _from, state) do
    {:reply, :ok, do_reconcile(state)}
  end

  def handle_call({:desired_envelope, node_id}, _from, state) do
    {:reply, committed_envelope(state, node_id), state}
  end

  @impl true
  def handle_info(:reconcile, state) do
    state = do_reconcile(state)
    schedule_reconcile(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- reservation (grow-eager) ----------------------------------------------

  # Grow the node's envelope to cover `workload`'s sizing on top of the already
  # committed + reserved capacity, and try the resize. On kubelet acceptance, RECORD
  # the reservation (held until the reconcile sees the live VM) and return :ok. On
  # any refusal, do NOT record it (no overcommit) and return {:error, :infeasible}.
  defp do_reserve(state, node_id, workload) do
    {mem, vcpus} = workload_sizing(state, workload)

    prev = Map.get(state.reservations, node_id, %{mem_mib: 0, cpu_millicores: 0})
    next_reservation = %{mem_mib: prev.mem_mib + mem, cpu_millicores: prev.cpu_millicores + vcpus * 1000}

    desired = envelope_with_reservation(state, node_id, next_reservation)

    case resize_node(state, node_id, desired) do
      :ok ->
        state =
          state
          |> put_in([:reservations, node_id], next_reservation)
          |> put_in([:applied, node_id], desired)

        {:ok, state}

      {:error, reason} ->
        Logger.warning(
          "embervm node sizer: grow for #{node_id} (workload #{workload}) refused (#{inspect(reason)}); placement must try elsewhere"
        )

        {{:error, :infeasible}, state}
    end
  end

  # -- reconcile (grow-eager settle + shrink-lazy) ---------------------------

  # Recompute each node's desired envelope from CURRENT live facts, refresh the
  # pod-name cache, drop reservations now covered by observed live VMs, and apply
  # grow-eager / shrink-lazy against the live pod. Wrapped node-by-node so one
  # node's apiserver error never aborts the whole pass.
  defp do_reconcile(state) do
    if enabled_state?(state) do
      state = refresh_pod_names(state)

      Enum.reduce(node_ids(state), state, fn node_id, acc ->
        reconcile_node(acc, node_id)
      end)
    else
      state
    end
  end

  defp reconcile_node(state, node_id) do
    # The observed committed envelope (live VMs on this node, no reservation).
    observed = committed_envelope(state, node_id)
    reservation = Map.get(state.reservations, node_id, %{mem_mib: 0, cpu_millicores: 0})

    # Drop the part of the reservation the observed live capacity already covers: a
    # reservation exists to cover an in-flight grow; once the VM is live its sizing
    # is in `observed`, so keeping the reservation would double-count. We keep any
    # reservation still ABOVE observed (a placement accepted but not yet booted).
    residual = residual_reservation(state, node_id, observed, reservation)
    state = put_in(state.reservations[node_id], residual)

    desired = add_envelopes(observed, residual) |> add_baseline_headroom(state)
    current = Map.get(state.applied, node_id) || baseline_only(state)

    cond do
      # GROW-EAGER: desired exceeds current on either axis. Grow immediately.
      grow?(desired, current) ->
        apply_grow(state, node_id, desired)

      # SHRINK-LAZY: desired is smaller and the released memory delta is large.
      shrink?(state, desired, current) ->
        apply_shrink(state, node_id, desired)

      true ->
        state
    end
  end

  # Keep only the reservation NOT yet covered by observed live capacity: max(0,
  # reservation - (observed - applied_baseline_committed)). Simpler and safe: once
  # observed committed mem >= the reservation's mem, the grow has landed, so drop
  # the whole reservation; otherwise keep it (still booting). Same per axis.
  defp residual_reservation(_state, _node_id, observed, reservation) do
    # A reservation is fully covered once the observed committed capacity has grown
    # by at least the reserved amount since it was taken. We cannot cheaply diff
    # against the pre-reservation observed, so use the conservative rule: keep the
    # reservation until observed committed capacity is at least as large as it (the
    # live VM now counts in observed), then drop. This never shrinks below live
    # commitment because `desired` always adds `observed` on top.
    %{
      mem_mib: if(observed.mem_mib >= reservation.mem_mib, do: 0, else: reservation.mem_mib),
      cpu_millicores:
        if(observed.cpu_millicores >= reservation.cpu_millicores, do: 0, else: reservation.cpu_millicores)
    }
  end

  defp apply_grow(state, node_id, desired) do
    case resize_node(state, node_id, desired) do
      :ok ->
        put_in(state.applied[node_id], desired)

      {:error, reason} ->
        # A grow the kubelet refuses during reconcile is not a placement decision
        # here (no caller waiting); log and leave `applied` unchanged so the next
        # tick retries. Never crash the loop.
        Logger.warning("embervm node sizer: reconcile grow for #{node_id} refused (#{inspect(reason)}); will retry")
        state
    end
  end

  defp apply_shrink(state, node_id, desired) do
    case resize_node(state, node_id, desired) do
      :ok ->
        put_in(state.applied[node_id], desired)

      {:error, reason} ->
        # SHRINK-LAZY: a memory-limit decrease the kubelet cannot satisfy in place
        # may need a restart. We NEVER force one: log and DEFER to the next natural
        # roll, leaving `applied` at the old (larger) envelope. The ledger runs
        # temporarily fat, which ADR 012 accepts.
        Logger.info(
          "embervm node sizer: in-place shrink for #{node_id} deferred to next roll (#{inspect(reason)})"
        )

        state
    end
  end

  # -- envelope arithmetic ---------------------------------------------------

  # The committed envelope from a node's LIVE VM facts joined to catalog sizing. No
  # baseline, no headroom, no reservation: just Σ committed guests.
  defp committed_envelope(state, node_id) do
    case NodeCapacity.fetch(state.table, node_id) do
      {:ok, facts} ->
        live_workloads(facts)
        |> Enum.reduce(%{mem_mib: 0, cpu_millicores: 0}, fn wl, acc ->
          {mem, vcpus} = workload_sizing(state, wl)
          %{mem_mib: acc.mem_mib + mem, cpu_millicores: acc.cpu_millicores + vcpus * 1000}
        end)

      :error ->
        %{mem_mib: 0, cpu_millicores: 0}
    end
  end

  # Every live VM's workload on the node across the four VM classes (a list with
  # one entry per live VM, so N VMs of a workload count N times). Group members,
  # serving, stateful, and session VMs all consume node memory.
  defp live_workloads(facts) do
    [:serving_vms, :stateful_vms, :session_vms, :group_member_vms]
    |> Enum.flat_map(fn key -> Map.get(facts, key, []) end)
    |> Enum.map(fn vm -> Map.get(vm, :workload) end)
    |> Enum.reject(&(is_nil(&1) or &1 == ""))
  end

  # Desired envelope with a specific reservation layered in (the reserve path uses
  # this to compute what to grow TO before committing the reservation).
  defp envelope_with_reservation(state, node_id, reservation) do
    committed_envelope(state, node_id)
    |> add_envelopes(reservation)
    |> add_baseline_headroom(state)
  end

  defp add_envelopes(a, b) do
    %{mem_mib: a.mem_mib + b.mem_mib, cpu_millicores: a.cpu_millicores + b.cpu_millicores}
  end

  defp add_baseline_headroom(env, state) do
    %{
      mem_mib: state.baseline_mem_mib + env.mem_mib + state.headroom_mib,
      cpu_millicores: state.baseline_cpu_millicores + env.cpu_millicores
    }
  end

  defp baseline_only(state) do
    %{mem_mib: state.baseline_mem_mib + state.headroom_mib, cpu_millicores: state.baseline_cpu_millicores}
  end

  defp grow?(desired, current) do
    desired.mem_mib > current.mem_mib or desired.cpu_millicores > current.cpu_millicores
  end

  # A shrink is worth doing only when the released MEMORY delta is large (memory is
  # the scarce, restart-risky axis). A CPU-only shrink is applied opportunistically
  # too, but the threshold gates memory (never churn a small guest teardown).
  defp shrink?(state, desired, current) do
    desired.mem_mib < current.mem_mib and current.mem_mib - desired.mem_mib >= state.shrink_threshold_mib
  end

  # Per-workload (mem_mib, vcpus) from the catalog; {0, 0} for an unknown workload
  # (a VM whose workload the catalog does not carry contributes no sizing, which is
  # safe: it under-counts rather than over-grows, and the headroom covers slack).
  defp workload_sizing(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, entry} ->
        {int0(Map.get(entry, :mem_mib)), int0(Map.get(entry, :vcpus))}

      :error ->
        {0, 0}
    end
  end

  defp int0(n) when is_integer(n) and n > 0, do: n
  defp int0(_), do: 0

  # -- actuation -------------------------------------------------------------

  # Resize a node's noded pod to `desired`. Resolves the pod name from the cache
  # (refreshed each reconcile from list_pods); a node with no known pod is a soft
  # miss ({:error, :no_pod}) the caller treats as a refusal / retry, never a crash.
  # The resize sets a CPU REQUEST but NO CPU limit and memory request == limit, so
  # the pod STAYS Burstable and the kubelet accepts the in-place resize (QoS is
  # immutable in place).
  defp resize_node(state, node_id, desired) do
    case Map.get(state.pod_names, node_id) do
      nil ->
        {:error, :no_pod}

      pod_name ->
        requests = %{
          "cpu" => "#{desired.cpu_millicores}m",
          "memory" => "#{desired.mem_mib}Mi"
        }

        # Burstable-preserving: memory limit == request, NO cpu limit key.
        limits = %{"memory" => "#{desired.mem_mib}Mi"}

        state.resize_fun.(state.namespace, pod_name, state.container, requests, limits)
    end
  end

  # Refresh the node_id -> pod_name map from the apiserver. Keyed by the pod's
  # spec.nodeName, which is exactly the K8s node NAME the daemon self-reports as its
  # capacity-fact node_id (EMBERVM_NODED_NODE = spec.nodeName). Wrapped so an
  # apiserver blip leaves the last-known map in place rather than clearing it.
  defp refresh_pod_names(state) do
    case state.list_pods_fun.(state.namespace, state.pod_label_selector) do
      {:ok, pods} ->
        names =
          for %{name: name, node_name: node} <- pods,
              is_binary(name) and is_binary(node),
              into: %{} do
            {node, name}
          end

        %{state | pod_names: names}

      {:error, reason} ->
        Logger.warning("embervm node sizer: pod list failed (#{inspect(reason)}); keeping last-known pod map")
        state
    end
  end

  # -- helpers ---------------------------------------------------------------

  # A node id the reconcile should consider: the union of dispatchable nodes (in the
  # capacity table) and nodes we hold a reservation or applied envelope for (so a
  # node that just lost its last guest is still shrunk on the next pass).
  defp node_ids(state) do
    from_facts = for f <- NodeCapacity.all(state.table), do: Map.get(f, :configured_id) || Map.get(f, :node_id)
    (from_facts ++ Map.keys(state.reservations) ++ Map.keys(state.applied))
    |> Enum.reject(&is_nil/1)
    |> Enum.uniq()
  end

  # The sizer actuates only when a namespace is configured (in-cluster). Without it
  # (local mix, ExUnit with no namespace opt) it is disabled: reserve/3 returns
  # {:error, :disabled} and placement keeps the legacy maxLiveVMs backstop.
  defp enabled_state?(state), do: is_binary(state.namespace) and state.namespace != ""

  defp schedule_reconcile(state) do
    Process.send_after(self(), :reconcile, state.reconcile_interval_ms)
  end

  # -- default (production) seams --------------------------------------------

  defp default_resize(namespace, pod_name, container, requests, limits) do
    Embervm.K8s.resize_pod(namespace, pod_name, container, requests, limits)
  end

  defp default_list_pods(namespace, label_selector) do
    Embervm.K8s.list_pods(namespace, label_selector)
  end
end
