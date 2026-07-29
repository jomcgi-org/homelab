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
       node: the node's bricks (`Embervm.Brick.bricks/1`) filtered by the
       DS-wildcard rule, then the scheduler's score order for a
       deterministic, sticky pick. `need_mib` is the workload's `mem_mib`. The
       DS-wildcard rule: a wildcard/zero-budget instance (`size_class == ""`, the big
       burst-envelope DaemonSet with no cgroup limit, reporting `mem_headroom_mib =
       0` / `mem_budget_mib = 0`) is ALWAYS eligible on the mem gate; only a CLASSED
       brick with a real budget is gated on `mem_headroom_mib >= need_mib`. We do NOT
       reuse a class-specific query here: it gates EVERY brick on headroom (right
       for the dispatcher, where a co-located brick reports a real budget), which
       would wrongly exclude a zero-headroom wildcard on the still-DS-only fleet.

    3. If NEITHER an owning instance NOR an eligible cold candidate exists on the node,
       `{:error, :no_eligible_instance}`: the caller fails the wake with a clear reason
       rather than dialling a too-small instance.

  ## inert on the current single-instance-per-node fleet

  With one instance per node the node's only instance IS the owner (it banked
  everything) and IS the sole cold candidate, so selection always returns that
  instance and the caller dials its `instance_id`, which is the single key
  NodeChannel is registered under (post-B0c the node-name alias is gone). The dial
  key is the instance_id in every case.

  ## legacy / test facts without an instance_id

  A capacity fact that carries no `:instance_id` (a statically-seeded instance or a
  test fixture that predates the field) falls back to the fact's `node_id`. For a
  node-scoped instance that node_id IS the key it registers under, so the dial still
  resolves; a dial-home fact always carries `:instance_id`, so this fallback is not
  hit for co-located bricks (and post-B0c a bare node name no longer misroutes: it
  fails closed to `:unknown_node`). The selection logic is unchanged; only the dial
  key degrades gracefully.
  """

  require Logger

  alias Embervm.{Brick, NodeCapacity}
  alias Embervm.Scheduler
  alias Embervm.Scheduler.Request

  # How often one {workload, node} pair may log a cold-rejection diagnostic. An
  # autoWake workload whose placement fails retries every ~10s indefinitely
  # (issue #4077 ran ~900 rejections over 2.5h), so an unthrottled per-brick dump
  # would bury the event it is meant to expose.
  @rejection_log_interval_ms 60_000

  @typedoc "A warmth-inventory field on a per-instance capacity fact + the id key its rows carry."
  @type warmth_key :: :serving_snapshots | :stateful_bundles | :group_bundle_sets | :session_snapshots

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

  @doc """
  The ORDERED cold-placement dial-id candidates on `node_id`, for the reject/retry
  path (ADR embervm/014 decision 3): the ready + mem-eligible co-located bricks,
  in the deterministic order whose HEAD is exactly what `select/2`'s cold pick
  returns and whose tail is the next-candidate frontier. Returns
  `{:ok, [dial_id]}` (never empty on success) or `{:error, :no_eligible_instance}`.

  Same node scoping, same `Embervm.Scheduler` readiness/eligibility rule, and the
  same Axis-C denial note as `cold_instance/4`, so the single-attempt cold pick and
  the retry candidate list cannot drift. A cold create passes NO `:warmth_key`
  (there is no owning snapshot to prefer); this is the cold frontier only.
  """
  @spec cold_candidates(String.t(), keyword()) ::
          {:ok, [String.t()]} | {:error, :no_eligible_instance}
  def cold_candidates(node_id, opts) when is_binary(node_id) do
    table = Keyword.get(opts, :table, NodeCapacity.table())
    workload = Keyword.fetch!(opts, :workload)
    need_mib = Keyword.fetch!(opts, :need_mib)

    case Scheduler.place(%Request{table: table, node_id: node_id, workload: workload, need_mib: need_mib, base: :ready}) do
      [] ->
        # Same demand-signal discrimination as cold_instance/4: only a true
        # no-eligible-brick wall feeds the autoscaler, not a base-not-ready wait.
        if Scheduler.place(%Request{table: table, node_id: node_id, need_mib: need_mib}) == [] do
          Embervm.BrickController.note_denial(need_mib)
        end

        {:error, :no_eligible_instance}

      bricks ->
        {:ok, Enum.map(bricks, &dial_id_from_brick/1)}
    end
  end

  def cold_candidates(_node_id, _opts), do: {:error, :no_eligible_instance}

  @doc """
  The channel key of the instance on `node_id` currently RUNNING `vm_id` (its
  per-instance capacity fact reports the `vm_id` in `stateful_vms`), or the bare
  `node_id` when no reporting instance is found (a legacy/single-instance fact, or
  the VM not yet re-reported after a roll). Post-B0c the bare `node_id` resolves only
  for a node-scoped instance; a co-located VM not yet re-reported fails closed to
  `:unknown_node` (a retryable miss) rather than misrouting to a sibling brick.

  This is the post-wake dial resolution the sweeper's bank/checkpoint/resolve
  workers use so a StopStateful/ResolveStateful lands on the instance that actually
  holds the live VM, not the collapsing node-name alias (which co-location made
  point at an arbitrary sibling brick). Same fail-open-to-node_id contract as the
  wake dial's `select/2`.
  """
  @spec dial_for_vm(atom(), String.t(), String.t()) :: String.t()
  def dial_for_vm(table \\ NodeCapacity.table(), node_id, vm_id)

  def dial_for_vm(table, node_id, vm_id),
    do: dial_owning(table, node_id, :stateful_vms, :vm_id, vm_id)

  @doc """
  The channel key of the instance on `node_id` currently HOLDING the banked bundle
  `snapshot_ref` on disk (its `stateful_bundles` report it), or the bare `node_id`
  when none is found. The evict/GC dial resolution: an EvictSnapshot/EvictArtifact
  must land on the instance that owns the bundle on disk (per-instance-on-disk,
  PR-2.5), not the node-name alias. Fail-open to `node_id` (legacy/single-instance
  facts, or a bundle not re-reported yet) exactly like `dial_for_vm/3`.
  """
  @spec dial_for_bundle(atom(), String.t(), String.t()) :: String.t()
  def dial_for_bundle(table \\ NodeCapacity.table(), node_id, snapshot_ref),
    do: dial_owning(table, node_id, :stateful_bundles, :snapshot_ref, snapshot_ref)

  @doc """
  The channel key of the instance on `node_id` currently RUNNING the SERVING VM
  `vm_id` (its `serving_vms` report it), or the bare `node_id` when none is found.
  The serving-sweeper's live-VM dial resolution (StopServing BANK/DESTROY): a serving
  VM lives on ONE instance, so co-location's node-name alias misroutes to a sibling.
  Same fail-open-to-node_id contract as `dial_for_vm/3`.
  """
  @spec dial_for_serving_vm(atom(), String.t(), String.t()) :: String.t()
  def dial_for_serving_vm(table \\ NodeCapacity.table(), node_id, vm_id),
    do: dial_owning(table, node_id, :serving_vms, :vm_id, vm_id)

  @doc """
  The channel key of the instance on `node_id` currently RUNNING the SESSION VM
  `vm_id` (its `session_vms` report it), or the bare `node_id` when none is found.
  The session restart/adoption dial resolution: a session's live VM is on ONE
  instance, so co-location's node-name alias misroutes to a sibling. Same fail-open-
  to-node_id contract as `dial_for_vm/3`.
  """
  @spec dial_for_session_vm(atom(), String.t(), String.t()) :: String.t()
  def dial_for_session_vm(table \\ NodeCapacity.table(), node_id, vm_id),
    do: dial_owning(table, node_id, :session_vms, :vm_id, vm_id)

  @doc """
  The channel key of the instance on `node_id` HOLDING the banked SERVING snapshot
  `snapshot_ref` on disk (its `serving_snapshots` report it), or the bare `node_id`
  when none is found. The serving evict dial resolution (EvictArtifact SERVING). Same
  fail-open-to-node_id contract as `dial_for_bundle/3`.
  """
  @spec dial_for_serving_bundle(atom(), String.t(), String.t()) :: String.t()
  def dial_for_serving_bundle(table \\ NodeCapacity.table(), node_id, snapshot_ref),
    do: dial_owning(table, node_id, :serving_snapshots, :snapshot_ref, snapshot_ref)

  @doc """
  The channel key of the instance on `node_id` HOLDING the banked SESSION snapshot
  `snapshot_ref` on disk (its `session_snapshots` report it), or the bare `node_id`
  when none is found. The session evict dial resolution (EvictSnapshot / EvictArtifact
  SESSION). Same fail-open-to-node_id contract as `dial_for_bundle/3`.
  """
  @spec dial_for_session_bundle(atom(), String.t(), String.t()) :: String.t()
  def dial_for_session_bundle(table \\ NodeCapacity.table(), node_id, snapshot_ref),
    do: dial_owning(table, node_id, :session_snapshots, :snapshot_ref, snapshot_ref)

  @doc """
  The channel key of the instance on `node_id` OWNING the composite group instance
  `group_instance_id` (it reports the id in either `group_member_vms`, a live group,
  or `group_bundle_sets`, a banked group), or the bare `node_id` when none is found.
  The group-sweeper's dial resolution (StopGroupMember/DeleteGroupNetwork/Evict): a
  group's members are all co-located on ONE instance, keyed by the durable
  group_instance_id, so the node-name alias misroutes to a sibling under co-location.
  Same fail-open-to-node_id contract as `dial_for_vm/3`.
  """
  @spec dial_for_group(atom(), String.t(), String.t()) :: String.t()
  def dial_for_group(table \\ NodeCapacity.table(), node_id, group_instance_id)

  def dial_for_group(table, node_id, group_instance_id)
      when is_binary(node_id) and is_binary(group_instance_id) and group_instance_id != "" do
    table
    |> instances_on(node_id)
    |> Enum.find(fn fact ->
      fact_reports_warmth?(fact, :group_member_vms, group_instance_id, :group_instance_id) or
        fact_reports_warmth?(fact, :group_bundle_sets, group_instance_id, :group_instance_id)
    end)
    |> case do
      nil -> node_id
      fact -> dial_id(fact)
    end
  end

  def dial_for_group(_table, node_id, _group_instance_id), do: node_id

  @doc """
  The channel key of the instance on `node_id` HOLDING `ref` in the inventory list
  `key` under `match_field`, as `{:ok, dial_key}`, or `:none` when no instance on the
  node reports it. Unlike the `dial_for_*` helpers this fails CLOSED (`:none`, not the
  node name), for a caller whose absence must be a distinct outcome (the session
  relight's `snapshot_lost`, where a missing snapshot must 410, not silently dial the
  node alias). The dial key is still the owning instance_id (co-location safe).
  """
  @spec owning_instance_for(atom(), String.t(), atom(), atom(), String.t()) ::
          {:ok, String.t()} | :none
  def owning_instance_for(table \\ NodeCapacity.table(), node_id, key, match_field, ref)

  def owning_instance_for(table, node_id, key, match_field, ref)
      when is_binary(node_id) and is_binary(ref) and ref != "" do
    table
    |> instances_on(node_id)
    |> Enum.find(fn fact -> fact_reports_warmth?(fact, key, ref, match_field) end)
    |> case do
      nil -> :none
      fact -> {:ok, dial_id(fact)}
    end
  end

  def owning_instance_for(_table, _node_id, _key, _match_field, _ref), do: :none

  # The owning instance on `node_id` whose capacity fact reports `ref` in the
  # inventory list `key` under `field`, else the bare `node_id` (legacy/single-
  # instance fact, or the row not re-reported yet). The shared core of the
  # instance-key dial resolution: every stateful/serving live-VM and bundle dial
  # routes through here so they cannot drift on the fail-open contract.
  defp dial_owning(table, node_id, key, field, ref)
       when is_binary(node_id) and is_binary(ref) and ref != "" do
    table
    |> instances_on(node_id)
    |> Enum.find(fn fact -> fact_reports_warmth?(fact, key, ref, field) end)
    |> case do
      nil -> node_id
      fact -> dial_id(fact)
    end
  end

  defp dial_owning(_table, node_id, _key, _field, _ref), do: node_id

  # The instance on `node_id` whose warmth inventory reports this wake's ref: a
  # relight MUST land on the instance that banked the bundle (per-instance-on-disk,
  # PR-2.5). :none when there is no warmth to prefer (a cold boot, no key/ref given)
  # or no instance on the node reports it (a true local miss / co-located-but-elsewhere,
  # which then falls through to the cold pick).
  defp owning_instance(table, node_id, opts) do
    warmth_key = Keyword.get(opts, :warmth_key)
    warmth_ref = Keyword.get(opts, :warmth_ref)
    match_field = Keyword.get(opts, :warmth_match_field, :snapshot_ref)
    # Additional fact keys that also count as "this node owns the instance", tried
    # after warmth_key. The group lane passes group_member_vms here so a bank of a
    # RUNNING instance (which has no banked set yet) still resolves to the node it is
    # on rather than cold-picking by free capacity (#4006). Other lanes pass none.
    also_keys = Keyword.get(opts, :warmth_also_keys, [])

    if is_atom(warmth_key) and is_binary(warmth_ref) and warmth_ref != "" do
      table
      |> instances_on(node_id)
      |> Enum.find(fn fact ->
        fact_reports_warmth?(fact, warmth_key, warmth_ref, match_field) or
          Enum.any?(also_keys, fn k -> fact_reports_warmth?(fact, k, warmth_ref, match_field) end)
      end)
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
  # `Embervm.Scheduler.place/1` predicate (the one source of truth every NEW-
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
  # `Scheduler.place/1` so the wake and dispatcher/session/serving paths can
  # never drift on the readiness gate. A class-specific query is deliberately NOT
  # used: it gates EVERY brick on headroom, wrongly excluding a zero-headroom wildcard
  # on the still-DS-only fleet. This is the COLD path only; the warmth/relight-owner
  # branch of `select/2` is unchanged (the banked instance already holds the base).
  defp cold_instance(table, node_id, workload, need_mib) do
    case Scheduler.place(%Request{table: table, node_id: node_id, workload: workload, need_mib: need_mib, base: :ready}) do
      [] ->
        # Distinguish WHY the pick failed before feeding the autoscaler (Axis C):
        # if some brick was slot/mem-eligible but merely not base-READY yet, the
        # wake is waiting on provisioning, not on capacity, and a scale-up would
        # not help (a fresh brick is equally un-ready). Only when NO brick is
        # eligible at all is this a CAPACITY denial worth a demand signal. The
        # note is an async cast: a missing controller (tests, DS-only fleet)
        # makes it a silent no-op, and the wake outcome is unchanged either way.
        capacity_denial? = Scheduler.place(%Request{table: table, node_id: node_id, need_mib: need_mib}) == []

        node_bricks = Brick.bricks(table) |> Enum.filter(&(&1.node_id == node_id))
        log_cold_rejection(node_id, workload, need_mib, node_bricks, capacity_denial?)

        if capacity_denial? do
          Embervm.BrickController.note_denial(need_mib)
        else
          # Bricks here were slot/mem-eligible but NONE advertised the workload's
          # base, so this is a provisioning gap, not a capacity wall. Tell the base
          # builder, which pins a workload's base to ONE instance and would
          # otherwise never place one on this node: a stateful wake anchors to the
          # VOLUME's node, and when that diverges from the pin the anchor is nobody's
          # responsibility and the wake fails forever (issue #4127). An async cast
          # that no-ops when the builder is not running, exactly like note_denial
          # above; the wake outcome is unchanged either way.
          Embervm.BaseBuilder.note_base_missing(workload, node_id)
        end

        {:error, :no_eligible_instance}

      [brick | _] ->
        {:ok, dial_id_from_brick(brick)}
    end
  end

  # Record WHY a cold pick found nothing, at the moment it happens (issue #4077).
  #
  # `:no_eligible_instance` is returned for two unrelated reasons -- no brick had a
  # free slot / the memory, or every eligible brick was not base-READY -- and from
  # outside the process they are indistinguishable. #4077 burned hours on exactly
  # that ambiguity: `/v1/nodes` (NodeRegistry) showed a brick with 6 free slots,
  # 15698 MiB against a 512 MiB ask, AND the workload's base READY, while placement
  # (which reads NodeCapacity, a SEPARATE store fed by the same node reports) kept
  # rejecting it. Nothing in the logs said which half of the cold filter was false.
  #
  # `workload_facts` is the field that discriminates the leading hypothesis:
  # `Brick.to_brick/1` defaults a missing `:workloads` submap to `%{}`, which
  # `Scheduler.base_ready?/2` reads as not-ready (fail-closed, deliberately). So a
  # fact that LOST its workloads map is indistinguishable from a brick that has
  # simply not advertised yet -- unless we record whether the map was there at all.
  # A brick reporting `workload_facts: 0` while NodeRegistry shows bases READY is
  # the capacity-write-path bug; `base_state` present but not READY is a genuine
  # provisioning wait and not a bug at all.
  defp log_cold_rejection(node_id, workload, need_mib, bricks, capacity_denial?) do
    if throttle_rejection_log?(workload, node_id) do
      # The payload rides the MESSAGE, not Logger metadata. Embervm.LogFormatter
      # encodes a fixed @meta_keys whitelist and drops everything else, on purpose,
      # so an un-encodable term can never crash the formatter -- and its jsonable/1
      # only accepts binary/integer/float/boolean, so a list of brick maps was never
      # going to survive. Shipping this as metadata emitted the line with the two
      # whitelisted keys and silently discarded every field worth reading, which is
      # exactly what happened the first time this fired in prod.
      Logger.warning(
        "embervm wake: no cold-placement candidate on node " <>
          rejection_summary(need_mib, capacity_denial?, bricks, workload),
        workload: workload,
        node_id: node_id
      )
    end
  end

  # One flat line describing the rejection. `capacity_denial=true` is a real
  # capacity wall (the only case that feeds the autoscaler); `false` means bricks
  # were slot/mem-eligible but none base-READY, i.e. a provisioning wait a
  # scale-up would not help.
  @doc false
  def rejection_summary(need_mib, capacity_denial?, bricks, workload) do
    rendered =
      case bricks do
        [] -> "none"
        _ -> bricks |> Enum.map(&render_brick(&1, workload, need_mib)) |> Enum.join(" ")
      end

    "need_mib=#{need_mib} capacity_denial=#{capacity_denial?} " <>
      "brick_count=#{length(bricks)} bricks=[#{rendered}]"
  end

  defp render_brick(brick, workload, need_mib) do
    v = brick_rejection_view(brick, workload, need_mib)

    "{id=#{v.instance_id} class=#{v.size_class} free=#{v.free_slots} " <>
      "hr=#{v.mem_headroom_mib} budget=#{v.mem_budget_mib} eligible=#{v.eligible} " <>
      "base_ready=#{v.base_ready} workload_facts=#{v.workload_facts} " <>
      "base_state=#{inspect(v.base_state)}}"
  end

  @doc false
  # Public only so the tests can assert the discriminator as DATA. Asserting it
  # through the log would mean ExUnit.CaptureLog inside an async module, which
  # captures concurrently-running tests' output too and would make these counts
  # flaky, the exact failure class #4078 just cleaned up.
  def brick_rejection_view(brick, workload, need_mib) do
    workloads = Map.get(brick, :workloads) || %{}

    %{
      instance_id: Map.get(brick, :instance_id, ""),
      size_class: Map.get(brick, :size_class, ""),
      free_slots: Map.get(brick, :free_slots, 0),
      mem_headroom_mib: Map.get(brick, :mem_headroom_mib, 0),
      mem_reject_floor_mib: Map.get(brick, :mem_reject_floor_mib, 0),
      mem_budget_mib: Map.get(brick, :mem_budget_mib, 0),
      eligible: Embervm.Scheduler.eligible?(brick, need_mib),
      base_ready: Embervm.Scheduler.base_ready?(brick, workload),
      # How many workloads this fact advertises at all. 0 means the submap is
      # absent or empty, i.e. base_ready is false by the fail-closed default
      # rather than by anything the daemon actually said about this workload.
      workload_facts: map_size(workloads),
      base_state: workloads |> Map.get(workload, %{}) |> Map.get(:base_state)
    }
  end

  # At most one diagnostic per {workload, node} per @rejection_log_interval_ms.
  # Kept in the process dictionary rather than ETS or :persistent_term because the
  # retry loop for a given workload runs in one long-lived process (StatefulManager),
  # so a process-local timestamp throttles it without adding a table, an owner, or a
  # global write. A caller in another process simply gets its own budget, which is
  # the desired behaviour: each caller reports its own first rejection.
  @doc false
  def throttle_rejection_log?(workload, node_id) do
    key = {__MODULE__, :cold_rejection_logged_at, workload, node_id}
    now = System.monotonic_time(:millisecond)

    case Process.get(key) do
      last when is_integer(last) and now - last < @rejection_log_interval_ms ->
        false

      _ ->
        Process.put(key, now)
        true
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

  # Same fallback for a brick map (Brick normalizes instance_id to "" when the
  # fact lacked it, so treat "" as absent and fall back to node_id).
  defp dial_id_from_brick(%{instance_id: id, node_id: node_id}) do
    if is_binary(id) and id != "", do: id, else: node_id
  end
end
