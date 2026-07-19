defmodule Embervm.ServingPlacement do
  @moduledoc """
  The serving placement seam (R3, Task 8): the ONLY module that computes WHERE a
  serving instance lives, shaped exactly like `Embervm.SessionPlacement` (standing
  decision 10). The activator (`Embervm.ServingManager`) calls this and never
  inspects node capacity facts itself, keeping the front-end split from placement
  so multi-node is a DATA change (more capacity rows), not a code change.

  ## the two placement questions (mirroring SessionPlacement)

    * `node_for_create/2`: where does a NEW (cold) serving instance's VM get
      placed? A rendezvous hash of the workload over the SERVING-CAPABLE ready
      nodes with the workload's base built AND live-VM budget. v1 has one serving
      node, so the hash is trivially that node, but the interface takes the
      capacity table so multi-node needs only more rows. Returns
      `{:ok, node_id, base_snapshot_ref}` (the base image ref the daemon cold-boots
      for `StartServing(fresh)`, D-R3.4.2) or `{:error, :no_capacity}`.

    * `node_for_relight/2`: where does a BANKED serving instance relight? NOT a
      fresh choice: a node-local serving snapshot lives on exactly one node
      (standing decision 3), so the instance relights ONLY on the node that reports
      its snapshot. This reads the instance's recorded `node_id` and CONFIRMS the
      node currently reports that instance's serving snapshot in its inventory. If
      no ready node reports the snapshot (node death, evicted out of band), relight
      is impossible and the caller fails the instance `snapshot_lost` (loud, never a
      silent blank VM), exactly the session relight-lost path.

  ## serving-capable = reports a serving subnet

  A node is a serving target only when it reports a non-empty `serving_subnet_cidr`
  (the daemon allocates serving tap IPs from it): the same predicate
  `Embervm.EndpointPublisher` uses to derive which node Envoys to push to, so
  "where a serving VM can be placed" and "where its endpoint is published" are
  the same node set by construction.

  ## why placement is pure functions over the capacity table

  Placement is a read over the `Embervm.NodeCapacity` ETS table (the registry's
  fail-closed projection of node truth) plus the instance row. It holds no state
  and runs no process: a stale/empty capacity read simply yields "no node", which
  the caller turns into a `:no_capacity` create denial or a `:snapshot_lost`
  relight failure. Keeping it a pure module (not a GenServer) means the activator
  can call it inline without a message round-trip, and it is trivially testable.
  """

  alias Embervm.NodeCapacity

  @type node_choice ::
          {:ok, node_id :: String.t(), base_snapshot_ref :: String.t() | nil}
          | {:error, :no_capacity}

  @doc """
  Picks the node for a new (cold) serving instance of `workload`: the
  rendezvous-hash winner among the serving-capable ready nodes that have this
  workload's base built AND live-VM budget. Returns `{:ok, node_id,
  base_snapshot_ref}` (the base the daemon cold-boots for `StartServing(fresh)`)
  or `{:error, :no_capacity}` when no node qualifies.

  The rendezvous key is the WORKLOAD (mirroring SessionPlacement); with one serving
  node this is deterministic, and multi-node spreads a workload's cold starts
  across nodes without central state.

  ## grow-eager sizing gate (PR-I, ADR embervm/012)

  Mirrors `Embervm.SessionPlacement.node_for_create/3`: before returning a node,
  placement asks the CP dynamic sizer to grow that node's noded pod envelope for the
  new serving guest. A resize the kubelet reports Infeasible/Deferred is a placement
  refusal (drop the candidate, try the next rendezvous candidate); `:ok` and
  `{:error, :disabled}` let the candidate stand. The default seam consults
  `Embervm.NodeSizer`; tests inject a fake.
  """
  @spec node_for_create(String.t(), atom(), (String.t(), String.t() -> :ok | {:error, atom()})) ::
          node_choice()
  def node_for_create(workload, capacity_table \\ NodeCapacity.table(), reserve_fun \\ &default_reserve/2) do
    NodeCapacity.all(capacity_table)
    |> Enum.filter(&serving_capable?/1)
    |> Enum.filter(&eligible_for_workload?(&1, workload))
    |> pick_with_sizing(workload, reserve_fun)
    |> case do
      nil ->
        {:error, :no_capacity}

      fact ->
        wc = Map.get(fact.workloads, workload)
        # The cold-boot ref is the node's serving_image_ref (the handler artifact a
        # serving cold boot attaches), NOT snapshot_ref (the base memory snapshot the
        # task lane restores). Passing snapshot_ref here was the "not provisioned"
        # bug: noded looked a base-snapshot key up in the runtime-rootfs image table.
        {:ok, fact.configured_id, Map.get(wc, :serving_image_ref)}
    end
  end

  @doc """
  Resolves the node a BANKED serving `instance` relights on: the instance's
  recorded `node_id`, CONFIRMED to be a serving-capable ready node currently
  reporting the instance's `snapshot_ref` in its `serving_snapshots` inventory.
  Returns `{:ok, node_id}` or `{:error, :snapshot_lost}` when no ready node holds
  the snapshot. Placement never picks a DIFFERENT node for a relight: a node-local
  serving snapshot is restorable on exactly its owning node (standing decision 3).
  """
  @spec node_for_relight(map(), atom()) :: {:ok, String.t()} | {:error, :snapshot_lost}
  def node_for_relight(instance, capacity_table \\ NodeCapacity.table()) do
    snapshot_ref = Map.get(instance, :snapshot_ref)
    node_id = Map.get(instance, :node_id)

    if is_binary(snapshot_ref) and snapshot_ref != "" and
         node_reports_snapshot?(capacity_table, node_id, snapshot_ref) do
      {:ok, node_id}
    else
      {:error, :snapshot_lost}
    end
  end

  @doc """
  The serving_image_ref a `node_id` currently reports for `workload` (the cold-boot
  handler artifact of the node's CURRENT-runtime serving base), or `nil` when the
  node is absent from the capacity table or reports no ref for the workload yet.

  This is the turnover key (D-R3.11.3 follow-up): a banked serving snapshot's
  `base_snapshot_ref` records the ref it was BORN from, so comparing it to this
  current ref tells whether a relight would resume a stale rootfs/handler (old code
  after a runtime roll). A `nil`/empty current ref means the new base is not built
  yet, so callers fail OPEN (keep the snapshot relightable for warmth).
  """
  @spec current_serving_image_ref(atom(), term(), String.t()) :: String.t() | nil
  def current_serving_image_ref(capacity_table \\ NodeCapacity.table(), node_id, workload) do
    case NodeCapacity.fetch(capacity_table, node_id) do
      {:ok, fact} ->
        fact
        |> Map.get(:workloads, %{})
        |> Map.get(workload, %{})
        |> Map.get(:serving_image_ref)

      :error ->
        nil
    end
  end

  # -- internals -------------------------------------------------------------

  # Serving-capable: the node reports a serving subnet (the same predicate the
  # EndpointPublisher's node derivation uses), so placement and publication target
  # the same node set.
  defp serving_capable?(fact) do
    cidr = Map.get(fact, :serving_subnet_cidr)
    is_binary(cidr) and cidr != ""
  end

  # Eligible for a new cold serving instance of `workload` when it has the
  # workload's base READY (a restorable base ref) AND live-VM budget below its node
  # cap. Mirrors SessionPlacement.eligible_for_workload? exactly.
  defp eligible_for_workload?(fact, workload) do
    wc = Map.get(fact.workloads || %{}, workload)

    cond do
      is_nil(wc) -> false
      not base_ready?(wc) -> false
      not has_budget?(fact) -> false
      true -> true
    end
  end

  defp base_ready?(wc) do
    ready =
      case Map.get(wc, :base_state) do
        :BASE_BUILD_STATE_READY -> true
        3 -> true
        _ -> false
      end

    # A serving cold boot needs the SERVING IMAGE (the handler artifact), not the base
    # memory snapshot: gate eligibility on serving_image_ref, so a node that built the
    # base snapshot but not (yet) the serving handler artifact is not picked for a cold
    # serving start (D-R3.11.2). The base_state READY still guards the underlying build.
    ref = Map.get(wc, :serving_image_ref)
    ready and is_binary(ref) and ref != ""
  end

  defp has_budget?(fact) do
    max = Map.get(fact, :max_live_vms, 0)
    max > 0 and Map.get(fact, :live_vms, 0) < max
  end

  # Walk the eligible serving-capable nodes in rendezvous order, asking the sizer to
  # grow each candidate before accepting it (identical to SessionPlacement). The
  # first candidate whose grow the kubelet accepts (or whose sizer is disabled) wins;
  # a refused (:infeasible) candidate is dropped. Empty/all-refused yields nil.
  defp pick_with_sizing([], _workload, _reserve_fun), do: nil

  defp pick_with_sizing(facts, workload, reserve_fun) do
    facts
    |> Enum.sort_by(fn fact -> weight(workload, fact.configured_id) end, :desc)
    |> Enum.find(fn fact ->
      case reserve_fun.(fact.configured_id, workload) do
        {:error, :infeasible} -> false
        _ -> true
      end
    end)
  end

  # Default sizing seam: grow the chosen node's envelope via the CP dynamic sizer.
  # Any error (sizer not running / off) is treated as :disabled so placement
  # proceeds on the legacy maxLiveVMs backstop, never refusing every node.
  defp default_reserve(node_id, workload) do
    Embervm.NodeSizer.reserve(node_id, workload)
  catch
    _kind, _reason -> {:error, :disabled}
  end

  defp weight(key, node_id) do
    :erlang.phash2({key, node_id}, 4_294_967_296)
  end

  # Whether a serving-capable ready node with the given id currently reports
  # `snapshot_ref` among its banked serving-snapshot inventory. A node absent from
  # the (fail-closed) capacity table is not ready, so an instance on a down node
  # reads as snapshot_lost.
  defp node_reports_snapshot?(_capacity_table, node_id, _snapshot_ref) when not is_binary(node_id),
    do: false

  defp node_reports_snapshot?(capacity_table, node_id, snapshot_ref) do
    case NodeCapacity.fetch(capacity_table, node_id) do
      {:ok, fact} ->
        serving_capable?(fact) and
          fact
          |> Map.get(:serving_snapshots, [])
          |> Enum.any?(fn snap -> Map.get(snap, :snapshot_ref) == snapshot_ref end)

      :error ->
        false
    end
  end
end
