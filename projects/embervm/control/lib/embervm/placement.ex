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
      `mem_headroom_mib >= need_mib`.

  This is exactly the rule `Embervm.WakeInstance` encoded for the WAKE dial;
  extracting it here lets the dispatcher miss tier and the session/serving CREATE
  paths reuse the identical predicate instead of each re-deriving it (and
  drifting, as `candidates/3` did).

  ## why NOT `BrickLedger.candidates/3`

  `candidates/3` gates every brick on `mem_headroom_mib >= need_mib` (correct for
  a fleet of only classed bricks, where a wildcard reports a real budget), which
  wrongly excludes a zero-headroom wildcard. On the still-DS-only fleet the single
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
  need_mib`. Split out so a caller that has already applied its own slot check
  (the dispatcher's `has_budget?`) can reuse just the memory rule.
  """
  @spec mem_eligible?(brick(), non_neg_integer()) :: boolean()
  def mem_eligible?(brick, need_mib) do
    wildcard?(brick) or Map.get(brick, :mem_headroom_mib, 0) >= need_mib
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
end
