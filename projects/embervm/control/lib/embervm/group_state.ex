defmodule Embervm.GroupState do
  @moduledoc """
  The composite-group lifecycle FSM (R5), expressed as data, mirroring
  `Embervm.StatefulState`: a module-attribute map from `{state, event}` to the
  next state. `Embervm.GroupStore` is the only caller that mutates group state,
  and every mutation goes through `transition/2` or `transition!/2` first, so an
  illegal transition fails loudly at the call site instead of corrupting ETS or
  the op-log with a state the FSM never sanctions.

  ## the class: a scale-to-zero COMPOSITE group of member microVMs

  A composite workload is a group of member VMs that live, bank, relight, and
  die as ONE unit (ADR embervm/001). Like the stateful class it is a singleton at
  the GROUP level (decision, mirrored from stateful decision 3): at most one live
  group INSTANCE per composite CR, backed by a per-group private /24 subnet and a
  whole-set banked warmth bundle. The lifecycle is the stateful lifecycle
  generalized to a set, with two structural differences: (1) `banked` is reached
  from `running` via a transient `banking` (the entry endpoint is pulled the
  instant a bank begins), and (2) a banked set can wake either WARM (relight,
  resuming the whole snapshot set) or COLD (`fresh_booting`, when the set is
  partial/unreadable and warmth must be discarded).

  ## the lifecycle

      creating      -> running                    (every member health-gated, entry published)
      running       -> banking -> banked          (idle bank / unpublish, Task 7+)
      banked        -> relighting -> running       (WARM wake, whole set resumed)
      banked        -> fresh_booting -> running    (COLD wake, set broken, warmth discarded)
      banked        -> expired                     (banked-TTL GC, terminal)
      <any live>    -> destroyed | failed          (delete/lifetime | daemon error)

  `degraded` is DELIBERATELY NOT a state: a member falling unhealthy while the
  group stays up is a FLAG on the `running` instance (`degraded_member` naming the
  dead member), never a distinct FSM node. Crash-consistency is per-VM (never
  across members), so a single member loss keeps the group `running`-with-a-flag,
  and its recovery just clears the flag. This is why the transition table has no
  `degraded` state and the store carries the flag as an instance field.

  ## naming: the projection strings are authoritative

  Exactly as `StatefulState` aligns to the shipped `stateful_instances`
  projection, this FSM's persisted states ARE the strings the MERGED PR-1
  `group_instances` projection (`Embervm.OpLog.SQLite`) writes and no others:

    * `starting`  (group_created, group_relit, group_fresh_booted) <- the
      pre-running boot/resume state the projection persists (the FSM node is
      `creating`; a relight/fresh-boot returns to `creating` semantically but the
      projection writes "starting", so `state_to_string/1` maps `creating ->
      "starting"`)
    * `running`   (group_running)          <- the live-and-in-fan-out state
    * `banked`    (group_banked)
    * `destroyed` / `failed`               (the terminal kinds; `expired` rides
      `group_destroyed{reason: expired}`, so there is no distinct `expired`
      projection string)

  The projection also writes `degraded` (group_degraded), but that is the running
  instance carrying the degraded FLAG, projected as the `degraded` string for the
  audit trail; the STORE holds it as `running` + a flag, and `state_from_string/1`
  maps the `"degraded"` projection string back to `running` (the flag is
  reconstructed from the unhealthy member rows). The transient states NOT in the
  projection are `banking`, `relighting`, and `fresh_booting`: all three are
  ETS-only markers never persisted (see below).

  ## transient (ETS-only) states

  `banking`, `relighting`, and `fresh_booting` are the group counterparts of the
  stateful FSM's `banking`/`relighting`/`cold_booting`: entered by
  `Embervm.GroupStore.mark/2` with NO op-log append (the durable log records only
  COMPLETED lifecycle transitions), and healed from node inventory by adoption
  (Task 7) if a crash strands them. The paired durable completion op
  (`group_banked`, `group_relit`, `group_fresh_booted`) is what actually persists
  the outcome; until it lands these are ETS-only markers a later op or an adoption
  reconcile resolves. Their `*_abort` edges are the ETS-only recovery when the
  daemon RPC fails but the VM set / snapshot set is intact, returning the group to
  the pre-operation state so a later attempt retries.

  ## the two wake paths off `banked`

  `relight` (WARM) resumes the banked set in place; `fresh_boot` (COLD) discards
  the set and cold-boots every member fresh on the private subnet. The activator
  (Task 7) chooses between them on the set-completeness check (is every member's
  bundle present in the set): a complete set relights, a partial/unreadable set
  fresh-boots (and evicts the stale set via a paired `group_set_evicted`). Both
  wake edges land back on `creating` (not yet re-running); a paired
  `group_running` then moves the instance to `running`.

  Terminal states each carry a recorded reason (the op payload's `reason`):

    * `destroyed`: reachable from every non-terminal state (a delete or lifetime
      expiry can destroy a group at any point). `expired` (banked-TTL /
      max-lifetime GC) rides `group_destroyed{reason: expired}`.
    * `failed`: reachable from every LIVE state (a member-start error during
      create tears the whole group to `failed`, decision 11: create is atomic;
      a StartGroupMember / StopGroupMember transport error, or an unrestorable
      set, fails the group from wherever it was). `banked` cannot `fail` (it holds
      no VMs); a broken banked set `evict`s its warmth (staying live) but the group
      instance is not failed by it.
  """

  defmodule IllegalTransition do
    defexception [:state, :event]

    @impl true
    def message(%{state: state, event: event}) do
      "illegal group transition: #{inspect(state)} -/-> on #{inspect(event)}"
    end
  end

  @states [
    :creating,
    :running,
    :banking,
    :banked,
    :relighting,
    :fresh_booting,
    :destroying,
    :destroyed,
    :failed
  ]

  @terminal_states [:destroyed, :failed]

  # The states that hold a LIVE group (member VMs up or in flight, not a banked
  # set, not terminal). A live group is the singleton the class allows exactly one
  # of. `banked` is deliberately NOT live: it holds a snapshot set, no VMs.
  # `destroying` (ADR embervm/014 decision 5) IS live: the per-member node-confirmed
  # teardown RPCs are in flight, so the singleton guard must count it until the node
  # confirms teardown and the terminal destroyed op fires.
  @live_states [:creating, :running, :banking, :relighting, :fresh_booting, :destroying]

  @events [
    :publish,
    :unpublish,
    :bank,
    :bank_ready,
    :bank_abort,
    :relight,
    :relight_ready,
    :relight_abort,
    :fresh_boot,
    :fresh_ready,
    :fresh_abort,
    :begin_destroy,
    :destroy,
    :fail
  ]

  # The transition table. Every legal (state, event) pair; anything not a key here
  # is illegal by construction. `publish` is the whole-group readiness edge
  # (creating -> running once every member is health-gated and the entry is
  # published). There is NO `degrade`/`recover` event: degradation is a FLAG on the
  # running instance, not an FSM edge (see the moduledoc).
  @transitions %{
    # Publish: a created (relit, or fresh-booted) group whose members are all up
    # and whose entry endpoint is now installed in the fan-out, moving it to
    # `running`.
    {:creating, :publish} => :running,
    # Unpublish / bank: the entry endpoint is pulled from the fan-out and the bank
    # sequence begins. Transient (ETS-only): a running group being banked leaves the
    # fan-out and enters `banking` in one ETS-only step. The VMs are not gone yet;
    # `bank_ready` completes the durable bank. `bank` is the same edge under a
    # different caller name (an explicit bank vs an unpublish-then-bank).
    {:running, :unpublish} => :banking,
    {:running, :bank} => :banking,
    # Idle-bank completion: banking -> banked (StopGroupMember BANK wrote every
    # member snapshot). The bank_abort edge (banking -> running) is the ETS-only
    # recovery when the bank RPC set fails: no complete set was written, so the
    # group returns to running (a later attempt re-banks, or it is republished).
    {:banking, :bank_ready} => :banked,
    {:banking, :bank_abort} => :running,
    # WARM wake: banked -> relighting (StartGroupMember relight in flight for the
    # whole set) -> creating (then a paired publish moves it to running). The
    # relight_abort edge (relighting -> banked) is the ETS-only recovery when a
    # transient relight failure leaves the set intact: the group returns to banked
    # and a later miss re-relights (or fresh-boots).
    {:banked, :relight} => :relighting,
    {:relighting, :relight_ready} => :creating,
    {:relighting, :relight_abort} => :banked,
    # COLD wake: banked -> fresh_booting (the set was broken, warmth discarded, a
    # fresh cold boot of every member) -> creating. Like relight, its abort edge
    # (fresh_booting -> banked) is the ETS-only recovery when the fresh boot RPC
    # fails transiently before the fresh VMs exist (the banked set is still on disk,
    # so a later attempt retries; the eviction of that set only lands with a
    # completed fresh_ready + paired group_set_evicted).
    {:banked, :fresh_boot} => :fresh_booting,
    {:fresh_booting, :fresh_ready} => :creating,
    {:fresh_booting, :fresh_abort} => :banked,
    # Destroy: from every non-terminal state (banked included: a banked group can be
    # destroyed, evicting its set and retiring the instance).
    {:creating, :destroy} => :destroyed,
    {:running, :destroy} => :destroyed,
    {:banking, :destroy} => :destroyed,
    {:banked, :destroy} => :destroyed,
    {:relighting, :destroy} => :destroyed,
    {:fresh_booting, :destroy} => :destroyed,
    # begin_destroy -> destroying -> destroy is the node-confirmed shape (ADR
    # embervm/014 decision 5), gated by EMBERVM_NODE_CONFIRMED_DESTROY.
    {:creating, :begin_destroy} => :destroying,
    {:running, :begin_destroy} => :destroying,
    {:banking, :begin_destroy} => :destroying,
    {:banked, :begin_destroy} => :destroying,
    {:relighting, :begin_destroy} => :destroying,
    {:fresh_booting, :begin_destroy} => :destroying,
    {:destroying, :destroy} => :destroyed,
    # Fail (member-start error during create, daemon transport/timeout, unrestorable
    # set): from every LIVE state that touches the daemon. banked cannot `fail` (it
    # holds no VMs); a broken banked set `evict`s its warmth, it does not fail the
    # instance.
    {:creating, :fail} => :failed,
    {:running, :fail} => :failed,
    {:banking, :fail} => :failed,
    {:relighting, :fail} => :failed,
    {:fresh_booting, :fail} => :failed
  }

  @type state ::
          :creating
          | :running
          | :banking
          | :banked
          | :relighting
          | :fresh_booting
          | :destroying
          | :destroyed
          | :failed

  @type event ::
          :publish
          | :unpublish
          | :bank
          | :bank_ready
          | :bank_abort
          | :relight
          | :relight_ready
          | :relight_abort
          | :fresh_boot
          | :fresh_ready
          | :fresh_abort
          | :begin_destroy
          | :destroy
          | :fail

  @spec states() :: [state()]
  def states, do: @states

  @spec events() :: [event()]
  def events, do: @events

  @spec live_states() :: [state()]
  def live_states, do: @live_states

  @spec terminal_states() :: [state()]
  def terminal_states, do: @terminal_states

  @spec terminal?(state()) :: boolean()
  def terminal?(state), do: state in @terminal_states

  @doc """
  Whether `state` holds a LIVE group (the singleton the class permits one of).
  True for the five non-terminal, non-banked states; false for `banked` (holds a
  snapshot set, not VMs) and every terminal state. The store's singleton invariant
  (`create/2` refuses a second live instance) reads this predicate.
  """
  @spec live?(state()) :: boolean()
  def live?(state), do: state in @live_states

  @spec transition(state(), event()) ::
          {:ok, state()} | {:error, {:illegal_transition, state(), event()}}
  def transition(state, event) do
    case Map.fetch(@transitions, {state, event}) do
      {:ok, next} -> {:ok, next}
      :error -> {:error, {:illegal_transition, state, event}}
    end
  end

  @spec transition!(state(), event()) :: state()
  def transition!(state, event) do
    case transition(state, event) do
      {:ok, next} ->
        next

      {:error, {:illegal_transition, state, event}} ->
        raise IllegalTransition, state: state, event: event
    end
  end
end
