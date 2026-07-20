defmodule Embervm.WakeInstance do
  @moduledoc """
  Instance selection for the stateful/serving/group WAKE dial (brick co-location
  foundation, Step 4).

  A wake resolves its target NODE first (the volume-anchored node for stateful, the
  snapshot-owning node for serving, the network-anchored node for a group). Before
  the brick co-location work, the manager then dialled `channel_fun.(node_id)` with
  that bare NODE NAME. Under one instance per node that was unambiguous. Under
  CO-LOCATED bricks (several noded instances on one node, ADR embervm/012 / the
  brick capacity ladder) it is not: the dual-keyed `Embervm.NodeChannel` resolves a
  node-name alias to ONE arbitrary instance (the last registrant), so a Postgres
  wake could land on a 2Gi brick too small to boot it.

  This module closes that gap: given the resolved node, it selects the SPECIFIC
  instance on that node the wake must dial, and returns that instance's
  `instance_id` (`"node/pod_uid"`) so the caller dials `channel_fun.(instance_id)`
  (the exact per-instance key the dual-keyed channel resolves without the
  node-name-alias collapse).

  ## selection priority

    1. The instance that BANKED this workload's warmth on disk. PR-2.5 made banked
       bundles/snapshots per-instance ON DISK, so a relight MUST land on the instance
       that banked it or the bundle is not there. `owning_instance/4` scans the node's
       per-instance capacity facts for the one whose warmth inventory (the
       `serving_snapshots` / `stateful_bundles` / `group_bundle_sets` list named by
       `warmth_key`) contains the wake's `ref`, and returns its `instance_id`.

    2. Else (a cold boot, or no owning instance found) a mem-ELIGIBLE instance on the
       node: the node's bricks (`Embervm.BrickLedger.bricks/1`) filtered by the
       DS-wildcard rule, then `Embervm.BrickLedger.choose(list, workload)` for a
       deterministic, sticky pick. `need_mib` is the workload's `mem_mib`. The
       DS-wildcard rule: a wildcard/zero-budget instance (`size_class == ""`, the big
       burst-envelope DaemonSet with no cgroup limit, reporting `mem_headroom_mib =
       0` / `mem_budget_mib = 0`) is ALWAYS eligible on the mem gate; only a CLASSED
       brick with a real budget is gated on `mem_headroom_mib >= need_mib`. We do NOT
       reuse `BrickLedger.candidates/3` here: it gates EVERY brick on headroom (right
       for the dispatcher, where a co-located brick reports a real budget), which
       would wrongly exclude a zero-headroom wildcard on the still-DS-only fleet.

    3. If NEITHER an owning instance NOR an eligible cold candidate exists on the node,
       `{:error, :no_eligible_instance}`: the caller fails the wake with a clear reason
       rather than dialling a too-small instance.

  ## inert on the current single-instance-per-node fleet

  With one instance per node the node's only instance IS the owner (it banked
  everything) and IS the sole cold candidate, so selection always returns that
  instance and the caller dials its `instance_id`, which the dual-keyed channel
  resolves identically to the old node-name dial. Output-equivalent, so this deploys
  later at the re-canary with no behaviour change today.

  ## legacy / test facts without an instance_id

  A capacity fact that carries no `:instance_id` (a statically-seeded instance or a
  test fixture that predates the field) falls back to the fact's `node_id`, so the
  dial still resolves via the node-name alias exactly as before. The selection logic
  is unchanged; only the dial key degrades gracefully.
  """

  alias Embervm.{BrickLedger, NodeCapacity}

  @typedoc "A warmth-inventory field on a per-instance capacity fact + the id key its rows carry."
  @type warmth_key :: :serving_snapshots | :stateful_bundles | :group_bundle_sets

  @doc """
  Select the instance on `node_id` a wake must dial. Returns `{:ok, instance_id}`
  (the `"node/pod_uid"` key to pass to `channel_fun`) or
  `{:error, :no_eligible_instance}`.

  Options:

    * `:workload`  (required) - the workload id, both the `choose/2` sticky key and the
      warmth match field for serving/stateful (see `:warmth_match_field`).
    * `:need_mib`  (required) - the workload's `mem_mib`; the cold-candidate mem gate.
    * `:warmth_key` - which per-instance warmth list to scan for the owning instance
      (`:serving_snapshots` / `:stateful_bundles` / `:group_bundle_sets`). Omit for a
      pure cold pick (no owning instance to prefer, e.g. a fresh first boot).
    * `:warmth_ref` - the ref the owning instance's warmth row must carry (the
      snapshot_ref for serving/stateful, the set_id for a group). Omit/`nil` with a
      `:warmth_key` means "no warmth to prefer", so it goes straight to the cold pick.
    * `:warmth_match_field` - the row field the `:warmth_ref` is matched against
      (default `:snapshot_ref`; groups pass `:set_id`).
    * `:table` - the capacity ETS table (default `NodeCapacity.table()`).
  """
  @spec select(String.t(), keyword()) :: {:ok, String.t()} | {:error, :no_eligible_instance}
  def select(node_id, opts) when is_binary(node_id) do
    table = Keyword.get(opts, :table, NodeCapacity.table())
    workload = Keyword.fetch!(opts, :workload)
    need_mib = Keyword.fetch!(opts, :need_mib)

    case owning_instance(table, node_id, opts) do
      {:ok, instance_id} -> {:ok, instance_id}
      :none -> cold_instance(table, node_id, workload, need_mib)
    end
  end

  def select(_node_id, _opts), do: {:error, :no_eligible_instance}

  # The instance on `node_id` whose warmth inventory reports this wake's ref: a
  # relight MUST land on the instance that banked the bundle (per-instance-on-disk,
  # PR-2.5). :none when there is no warmth to prefer (a cold boot, no key/ref given)
  # or no instance on the node reports it (a true local miss / co-located-but-elsewhere,
  # which then falls through to the cold pick).
  defp owning_instance(table, node_id, opts) do
    warmth_key = Keyword.get(opts, :warmth_key)
    warmth_ref = Keyword.get(opts, :warmth_ref)
    match_field = Keyword.get(opts, :warmth_match_field, :snapshot_ref)

    if is_atom(warmth_key) and is_binary(warmth_ref) and warmth_ref != "" do
      table
      |> instances_on(node_id)
      |> Enum.find(fn fact -> fact_reports_warmth?(fact, warmth_key, warmth_ref, match_field) end)
      |> case do
        nil -> :none
        fact -> {:ok, dial_id(fact)}
      end
    else
      :none
    end
  end

  # A mem-eligible AND base-READY cold candidate on the node, then a deterministic
  # sticky pick keyed by the workload. The gate is the SHARED
  # `Embervm.Placement.pick_ready/3` predicate (the one source of truth every NEW-
  # COLD-placement path uses): a free slot, mem-eligibility (the DS wildcard/zero-
  # budget brick is always mem-eligible, the big burst envelope reporting
  # `mem_headroom_mib = 0` under no cgroup limit; a CLASSED brick needs
  # `mem_headroom_mib >= need_mib`), AND `workloads[workload].base_state ==
  # BASE_BUILD_STATE_READY` so the instance has ADVERTISED the workload's base.
  #
  # The base-readiness gate closes the live co-location gap: a freshly-rolled or
  # scaled-up instance is dispatchable and mem-eligible the instant it registers, but
  # until its noded re-provisions the runtime image and advertises the base it cannot
  # resolve `boot_image_ref` and a cold boot hard-fails `noded: boot_image_ref
  # required` (a fresh 16Gi brick picked for a demo-postgres cold boot did exactly
  # this). Requiring `base_ready?` here means the cold pick skips the not-yet-ready
  # instance; if no instance is BOTH eligible and base-ready the `nil` becomes the
  # existing RETRYABLE `{:error, :no_eligible_instance}` (the activator keeps the
  # workload published and the client's reconnect re-enters the wake), so the wake
  # WAITS for a fresh instance to finish provisioning instead of dialling one that
  # cannot boot. On a STABLE fleet every instance long ago advertised its bases, so
  # this only changes behaviour in the post-roll/post-scale-up window (the gap).
  #
  # We scope the node's bricks first, then delegate the filter+`choose` to
  # `Placement.pick_ready/3` so the wake and dispatcher/session/serving paths can
  # never drift on the readiness gate. `BrickLedger.candidates/3` is deliberately NOT
  # used: it gates EVERY brick on headroom, wrongly excluding a zero-headroom wildcard
  # on the still-DS-only fleet. This is the COLD path only; the warmth/relight-owner
  # branch of `select/2` is unchanged (the banked instance already holds the base).
  defp cold_instance(table, node_id, workload, need_mib) do
    table
    |> BrickLedger.bricks()
    |> Enum.filter(fn brick -> brick.node_id == node_id end)
    |> Embervm.Placement.pick_ready(workload, need_mib)
    |> case do
      nil -> {:error, :no_eligible_instance}
      brick -> {:ok, dial_id_from_brick(brick)}
    end
  end

  # Every per-instance capacity fact on `node_id` (co-located bricks + the DS
  # wildcard), in no particular order.
  defp instances_on(table, node_id) do
    table
    |> NodeCapacity.all()
    |> Enum.filter(fn fact -> Map.get(fact, :node_id) == node_id end)
  end

  defp fact_reports_warmth?(fact, warmth_key, ref, match_field) do
    fact
    |> Map.get(warmth_key, [])
    |> Kernel.||([])
    |> Enum.any?(fn row -> Map.get(row, match_field) == ref end)
  end

  # The dial key for a capacity fact: its instance_id when present, else the node
  # name (legacy/static facts without the field still resolve via the node-name
  # alias, keeping single-instance behaviour identical).
  defp dial_id(fact) do
    case Map.get(fact, :instance_id) do
      id when is_binary(id) and id != "" -> id
      _ -> Map.get(fact, :node_id)
    end
  end

  # Same fallback for a brick map (BrickLedger normalizes instance_id to "" when the
  # fact lacked it, so treat "" as absent and fall back to node_id).
  defp dial_id_from_brick(%{instance_id: id, node_id: node_id}) do
    if is_binary(id) and id != "", do: id, else: node_id
  end
end
