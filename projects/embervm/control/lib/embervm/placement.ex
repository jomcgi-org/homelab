defmodule Embervm.Placement do
  @moduledoc """
  The one shared brick-eligibility predicate every NEW-placement path reasons
  about (brick co-location foundation, Step 5). Before this module the memory
  gate lived in three shapes that could drift: `Embervm.WakeInstance` had the
  correct DS-wildcard-always-eligible rule (Step 4), `Embervm.BrickLedger.candidates/3`
  had a subtly WRONG one (it gates EVERY brick on headroom, so a zero-budget
  wildcard is excluded), and the task dispatcher had NO memory gate at all (it
  used `choose/2` only as a tie-break, so a miss could land on a brick too small
  to boot the workload). This module is the single source of truth those NEW-
  placement paths now share.

  ## the eligibility rule

  A brick is eligible to take a NEW placement of a workload needing `need_mib`
  MiB iff it has a free live-VM slot AND it is mem-eligible:

    * a WILDCARD brick (`size_class == ""`, the legacy DaemonSet, OR
      `mem_budget_mib == 0`, a brick under no cgroup limit reporting a zero
      budget: the big burst envelope) is ALWAYS mem-eligible, regardless of the
      headroom it reports (a zero-budget wildcard reports `mem_headroom_mib = 0`
      but can still boot the guest);
    * a CLASSED brick with a real budget is mem-eligible only when its
      `mem_headroom_mib >= need_mib + mem_reject_floor_mib`. The node rejects
      on need plus its floor, so gating on need alone places workloads the node
      refuses, forever.

  This is exactly the rule `Embervm.WakeInstance` encoded for the WAKE dial;
  extracting it here lets the dispatcher miss tier and the session/serving CREATE
  paths reuse the identical predicate instead of each re-deriving it (and
  drifting, as `candidates/3` did).

  ## why NOT `BrickLedger.candidates/3`

  `candidates/3` gates every brick on its class and headroom, including the node
  admission floor (correct for a fleet of only classed bricks, where a wildcard
  reports a real budget), which wrongly excludes a zero-headroom wildcard. On the still-DS-only fleet the single
  DS instance IS a zero-budget wildcard, so gating it on headroom would deny every
  placement. Keeping the wildcard always-eligible is what makes this change INERT
  today: the sole DS brick per node is a wildcard, so it is always the (only)
  eligible candidate and every path is output-equivalent to its pre-Step-5 form.

  ## pure functions over brick maps

  Every function here is pure over the normalized brick maps
  `Embervm.BrickLedger.bricks/1` produces (or a raw capacity fact, which carries
  the same `size_class` / `mem_headroom_mib` / `mem_budget_mib` fields). No
  process, no ETS handle of its own: callers pass the already-read brick list, so
  this module never drifts from the capacity view its caller dispatched against.
  """

  alias Embervm.BrickLedger

  @typedoc "A brick or capacity-fact map carrying at least the fields the rule reads."
  @type brick :: map()

  @doc """
  Whether `brick` can take a NEW placement of a workload needing `need_mib` MiB:
  it has a free live-VM slot AND is `mem_eligible?/2`. This is the gate the
  dispatcher miss tier, the session/serving create pick, and the wake cold pick
  all apply so nothing lands on a brick too small to boot the guest.
  """
  @spec eligible?(brick(), non_neg_integer()) :: boolean()
  def eligible?(brick, need_mib) do
    Map.get(brick, :free_slots, 0) > 0 and mem_eligible?(brick, need_mib)
  end

  @doc """
  The MEMORY half of eligibility (no slot check): a wildcard brick is always
  mem-eligible; a classed brick with a real budget needs `mem_headroom_mib >=
  need_mib + mem_reject_floor_mib`. The node rejects on need plus its floor, so
  gating on need alone places workloads the node refuses, forever. Split out so
  a caller that has already applied its own slot check
  (the dispatcher's `has_budget?`) can reuse just the memory rule.
  """
  @spec mem_eligible?(brick(), non_neg_integer()) :: boolean()
  def mem_eligible?(brick, need_mib) do
    wildcard?(brick) or
      Map.get(brick, :mem_headroom_mib, 0) >=
        need_mib + Map.get(brick, :mem_reject_floor_mib, 0)
  end

  @doc """
  Whether `brick` is the always-mem-eligible WILDCARD: an empty size-class (the
  legacy DaemonSet) OR a zero/absent memory budget (no cgroup limit, the big
  burst envelope). A wildcard satisfies any `need_mib`.
  """
  @spec wildcard?(brick()) :: boolean()
  def wildcard?(brick) do
    Map.get(brick, :size_class, "") == "" or Map.get(brick, :mem_budget_mib, 0) == 0
  end

  @doc """
  Whether `brick` has ADVERTISED `workload`'s base as READY: its
  `workloads[workload].base_state` is the daemon's `BASE_BUILD_STATE_READY`. This
  is the co-location READINESS half of a NEW COLD placement, distinct from the
  memory/slot gate: a freshly-rolled or scaled-up instance is dispatchable and
  mem-eligible the instant it registers, but until its noded re-provisions the
  runtime image and advertises the base it CANNOT resolve `boot_image_ref` (the
  live `noded: boot_image_ref required` failure a fresh 16Gi brick hit on a
  demo-postgres cold boot). An instance adopts its node-shared bases from disk
  READY internally but only projects `base_state = READY` after `imageProvisioned`
  syncs, so this predicate is false in exactly the post-roll/post-scale-up window
  and true once the instance can actually boot the guest.

  Absent workload entry, or any non-READY `base_state`, is false. The daemon
  reports `base_state` as the proto enum, projected by the registry as the
  protobuf-elixir atom `:BASE_BUILD_STATE_READY`; the integer form `3` is accepted
  defensively, matching the tolerant check the dispatcher and session/serving
  placement already use so the four NEW-placement paths cannot drift on the
  readiness representation.

  This gates only the COLD / fresh-placement pick, NEVER the warmth/relight-owner
  path: a relight targets the specific instance that BANKED the bundle on disk,
  which by construction already holds (and advertises) that workload's base.
  """
  @spec base_ready?(brick(), term()) :: boolean()
  def base_ready?(brick, workload) do
    case Map.get(Map.get(brick, :workloads) || %{}, workload) do
      %{base_state: base_state} -> base_state_ready?(base_state)
      _ -> false
    end
  end

  # The daemon reports base_state as the proto enum. Accept the protobuf-elixir
  # atom form and the integer form (3) defensively, identical to the tolerant
  # checks in Dispatcher/SessionPlacement/ServingPlacement.
  defp base_state_ready?(:BASE_BUILD_STATE_READY), do: true
  defp base_state_ready?(3), do: true
  defp base_state_ready?(_), do: false

  @doc """
  Filter `bricks` to those `eligible?/2` for `need_mib`, then deterministically
  pick one keyed by `key` (`BrickLedger.choose/2`: sorted by `instance_id`,
  hashed on the key, so the same key sticks and distinct keys spread across the
  eligible bricks). Returns the chosen brick map or `nil` when none is eligible.
  The NEW-placement primitive the wake cold pick and the session create pick
  share.
  """
  @spec pick([brick()], term(), non_neg_integer()) :: brick() | nil
  def pick(bricks, key, need_mib) do
    bricks
    |> Enum.filter(&eligible?(&1, need_mib))
    |> BrickLedger.choose(key)
  end

  @doc """
  The COLD NEW-placement primitive: filter `bricks` to those `eligible?/2` for
  `need_mib` AND `base_ready?/2` for `workload` (`workload` is also the sticky
  `key`), then deterministically pick one via `BrickLedger.choose/2`. Returns the
  chosen brick or `nil` when no brick is BOTH eligible and base-ready.

  This is the wake cold pick's primitive. It adds the co-location READINESS gate on
  top of `pick/3`'s slot+memory gate so a fresh stateful/serving/task/group wake
  never dials a dispatchable, mem-eligible instance that has not yet advertised the
  workload's base and so would hard-fail `noded: boot_image_ref required`. When no
  brick qualifies, the caller returns its existing retryable no-eligible outcome
  (the activators keep retrying), so the wake WAITS for a fresh instance to finish
  provisioning and advertise the base instead of hard-failing on the first miss.

  Kept distinct from `pick/3` (which stays a pure slot+memory gate) so a caller that
  legitimately needs only the memory gate is unaffected; the warmth/relight-owner
  path in `Embervm.WakeInstance` also stays on the owner scan (the banked instance
  already holds the base) and never routes through here.
  """
  @spec pick_ready([brick()], term(), non_neg_integer()) :: brick() | nil
  def pick_ready(bricks, workload, need_mib) do
    bricks
    |> ready_candidates(workload, need_mib)
    |> BrickLedger.choose(workload)
  end

  @doc """
  The ORDERED cold-placement candidate list `pick_ready/3` chooses one from: the
  bricks that are BOTH `eligible?/2` for `need_mib` AND `base_ready?/2` for
  `workload`, sorted deterministically by the same `BrickLedger.choose/2` key
  (`workload`) so the FIRST element is exactly what `pick_ready/3` would return and
  the tail is the reject/retry frontier (ADR embervm/014 decision 3).

  This is the reject/retry counterpart of `pick_ready/3`: a cold NEW-placement path
  that wants to try the next co-located brick when the first rejects under node
  pressure feeds this whole list to `Embervm.Placement.Retry.run/3`, rather than
  picking one and failing on a reject. Sharing `ready_candidates/3` with
  `pick_ready/3` keeps the single-attempt and retry paths on ONE eligibility +
  ordering rule, so they cannot drift. Returns `[]` when no brick qualifies (the
  caller's existing no-eligible outcome).
  """
  @spec candidates_ready([brick()], term(), non_neg_integer()) :: [brick()]
  def candidates_ready(bricks, workload, need_mib) do
    bricks
    |> ready_candidates(workload, need_mib)
    |> sort_by_choose_key(workload)
  end

  # The shared eligible + base-ready filter both pick_ready/3 and candidates_ready/3
  # apply, so the single-attempt pick and the retry list can never diverge on which
  # bricks are cold-placement candidates.
  defp ready_candidates(bricks, workload, need_mib) do
    Enum.filter(bricks, fn brick -> eligible?(brick, need_mib) and base_ready?(brick, workload) end)
  end

  # Order the candidate list the SAME way BrickLedger.choose/2 would traverse it:
  # sorted by :instance_id, then rotated so the choose/2-selected brick is first and
  # the deterministic sticky order continues from there. This makes candidates_ready/3
  # head == pick_ready/3 result, and the tail the natural next-candidate sequence.
  defp sort_by_choose_key([], _key), do: []

  defp sort_by_choose_key(candidates, key) do
    sorted = Enum.sort_by(candidates, fn c -> Map.get(c, :instance_id, "") end)
    idx = :erlang.phash2(key, length(sorted))
    {head, tail} = Enum.split(sorted, idx)
    tail ++ head
  end
end
