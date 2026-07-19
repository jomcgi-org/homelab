defmodule Embervm.SessionPlacement do
  @moduledoc """
  The session placement seam (R2, Task 8): the ONLY module that computes WHERE a
  session lives. The invocation front-end (the router) and the lifecycle brain
  (`Embervm.SessionManager`) call this and never inspect node capacity facts
  themselves, keeping the R2 held invariant intact: the front-end is split from
  placement, so R3 can later swap a proxied hit for an xDS-published route without
  touching either caller (standing decision 7).

  ## the two placement questions

    * `node_for_create/2`: where does a NEW session's VM get placed? A rendezvous
      hash of the session-id over the READY nodes with live-VM budget for the
      workload. v1 has exactly one node, so the hash is trivially that node, but
      the interface takes the registry's node list (via the capacity table) so
      multi-node is a DATA change (more rows), not a code change. Because a session
      id does not exist until create mints it, create passes the WORKLOAD and lets
      placement pick a ready node deterministically; the caller then mints the id
      pinned to that node's snapshot_ref (the node's base for the workload).

    * `node_for_relight/2`: where does a BANKED session relight? NOT a fresh choice:
      a node-local snapshot lives on exactly one node (standing decision 3), so the
      session relights ONLY on the node that reports its snapshot. This reads the
      session's recorded `node_id` and CONFIRMS the node currently reports that
      session's snapshot in its inventory. If no ready node reports the snapshot
      (node death, evicted out of band), relight is impossible and the caller fails
      the session `snapshot_lost` -> 410 (loud, never a silent blank VM).

  ## why placement is pure functions over the capacity table

  Placement is a read over the `Embervm.NodeCapacity` ETS table (the registry's
  fail-closed projection of node truth) plus the session row. It holds no state and
  runs no process: a stale/empty capacity read simply yields "no node", which the
  caller turns into a `:no_capacity` create denial or a `:snapshot_lost` relight
  failure. Keeping it a pure module (not a GenServer) means the router and manager
  can call it inline without a message round-trip, and it is trivially testable.
  """

  alias Embervm.NodeCapacity

  @type node_choice :: {:ok, node_id :: String.t(), snapshot_ref :: String.t() | nil} | {:error, :no_capacity}

  @doc """
  Picks the node for a new session of `workload`: the rendezvous-hash winner among
  the ready nodes that have this workload's base built AND live-VM budget. Returns
  `{:ok, node_id, snapshot_ref}` (the node's base snapshot for the workload, passed
  to Prime on a claim miss) or `{:error, :no_capacity}` when no node qualifies.

  The rendezvous key is the WORKLOAD (a session id does not exist yet); with one
  node this is deterministic anyway. When multi-node lands, hashing per-workload
  spreads a workload's sessions' creates across nodes without central state.

  ## grow-eager sizing gate (PR-I, ADR embervm/012)

  Before returning a node, placement asks the CP dynamic sizer to GROW that node's
  noded pod envelope to cover the new guest (`reserve_fun.(node_id, workload)`). A
  resize the kubelet reports Infeasible/Deferred (`{:error, :infeasible}`) is a
  PLACEMENT REFUSAL: that candidate is dropped and placement falls to the next
  rendezvous candidate, so a session never lands on a node whose pod could not grow
  (no overcommit past the accepted envelope). `:ok` (grow accepted) and
  `{:error, :disabled}` (sizer off, legacy maxLiveVMs backstop is the only gate)
  both let the candidate stand. The default seam consults `Embervm.NodeSizer`; tests
  inject a fake to drive the refusal path without a live kubelet.
  """
  @spec node_for_create(String.t(), atom(), (String.t(), String.t() -> :ok | {:error, atom()})) ::
          node_choice()
  def node_for_create(workload, capacity_table \\ NodeCapacity.table(), reserve_fun \\ &default_reserve/2) do
    NodeCapacity.all(capacity_table)
    |> Enum.filter(&eligible_for_workload?(&1, workload))
    |> pick_with_sizing(workload, reserve_fun)
    |> case do
      nil ->
        {:error, :no_capacity}

      fact ->
        wc = Map.get(fact.workloads, workload)
        {:ok, fact.configured_id, Map.get(wc, :snapshot_ref)}
    end
  end

  @doc """
  Resolves the node a BANKED `session` relights on: the session's recorded
  `node_id`, CONFIRMED to be a ready node currently reporting the session's
  `snapshot_ref` in its `session_snapshots` inventory. Returns `{:ok, node_id}` or
  `{:error, :snapshot_lost}` when no ready node holds the snapshot (node death,
  out-of-band eviction). Placement never picks a DIFFERENT node for a relight: a
  node-local snapshot is restorable on exactly its owning node (standing decision 3).
  """
  @spec node_for_relight(map(), atom()) :: {:ok, String.t()} | {:error, :snapshot_lost}
  def node_for_relight(session, capacity_table \\ NodeCapacity.table()) do
    snapshot_ref = Map.get(session, :snapshot_ref)
    node_id = Map.get(session, :node_id)

    if is_binary(snapshot_ref) and snapshot_ref != "" and node_reports_snapshot?(capacity_table, node_id, snapshot_ref) do
      {:ok, node_id}
    else
      {:error, :snapshot_lost}
    end
  end

  # -- internals -------------------------------------------------------------

  # A node is eligible for a new session of `workload` when it has the workload's
  # base READY (a restorable snapshot_ref) AND live-VM budget below its node cap.
  # Mirrors SessionManager's PR-3 inline pick_node, now owned here.
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

    ref = Map.get(wc, :snapshot_ref)
    ready and is_binary(ref) and ref != ""
  end

  defp has_budget?(fact) do
    max = Map.get(fact, :max_live_vms, 0)
    max > 0 and Map.get(fact, :live_vms, 0) < max
  end

  # Walk the eligible nodes in rendezvous order, asking the sizer to grow each
  # candidate's pod before accepting it. The FIRST candidate whose grow the kubelet
  # accepts (or whose sizer is disabled) wins; a candidate the sizer REFUSES
  # (:infeasible) is dropped and the next-highest-weight candidate is tried, so a
  # node that cannot grow never takes the session. Empty list (or all refused)
  # yields nil (the caller denies :no_capacity).
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
  # A sizer that is not running (tests, or a control plane with sizing off) EXITs on
  # the call; we treat any error as :disabled so placement proceeds on the legacy
  # maxLiveVMs backstop rather than refusing every node when the sizer is absent.
  defp default_reserve(node_id, workload) do
    Embervm.NodeSizer.reserve(node_id, workload)
  catch
    _kind, _reason -> {:error, :disabled}
  end

  defp weight(key, node_id) do
    :erlang.phash2({key, node_id}, 4_294_967_296)
  end

  # Whether a READY node with the given id currently reports `snapshot_ref` among
  # its banked-snapshot inventory. A node absent from the (fail-closed) capacity
  # table is not ready, so a session on a down node reads as snapshot_lost.
  defp node_reports_snapshot?(_capacity_table, node_id, _snapshot_ref) when not is_binary(node_id), do: false

  defp node_reports_snapshot?(capacity_table, node_id, snapshot_ref) do
    case NodeCapacity.fetch(capacity_table, node_id) do
      {:ok, fact} ->
        fact
        |> Map.get(:session_snapshots, [])
        |> Enum.any?(fn snap -> Map.get(snap, :snapshot_ref) == snapshot_ref end)

      :error ->
        false
    end
  end
end
