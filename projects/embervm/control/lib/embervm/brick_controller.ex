defmodule Embervm.BrickController do
  @moduledoc """
  Single-writer reconciler that drives each size-class BRICK Deployment's replica
  count to its DESIRED value (brick-capacity PR-3, ADR embervm/013). Bricks are
  the fleet's capacity unit; this GenServer is the one component that scales them.

  ## what it does

  On a periodic tick it walks the configured size-classes and, for each, PATCHes
  the brick Deployment's `/scale` subresource to the class's desired replica count
  (`Embervm.K8s.scale_deployment/3`). Scaling is idempotent: patching a Deployment
  already at the desired count is a no-op write, and a class whose Deployment does
  not exist yet (`bricks.enabled=false`, or a not-yet-rendered class) simply 404s
  and is logged, never crashing the loop. It is the ONLY writer of brick replica
  counts, so there is no scale contention; ArgoCD's fleet-wide `ignoreDifferences`
  on Deployment `/spec/replicas` (the HPA rule) keeps these writes invisible to
  selfHeal.

  ## demand-driven autoscale (fleet plan Axis C)

  Beyond the static per-class desired count, the controller runs a DEMAND loop
  gated by `bricks.autoscale.mode` (values -> `EMBERVM_BRICK_AUTOSCALE_MODE`):

    * `off`     - legacy behavior: reconcile to the static `desired` only.
    * `observe` - DEFAULT. Still ACTS statically (identical live behavior to
      `off`), but computes and LOGS the autoscale decision each tick
      ("brick autoscale: would scale class X from N to M (reason=...)"), so the
      loop soaks against real traffic before it is allowed to act.
    * `up`      - scale-UP decisions act; scale-down stays observe-only.
    * `full`    - both directions act; scale-down is drain-aware (victim rail).

  The loop is level-based per class, evaluated on the reconcile tick against the
  LIVE replica count read from the Deployment's `/scale` subresource (so a CP
  restart never forgets a prior scale-up): `desired/2` is the pure decision
  function, `{target, reason} = desired(current, signals)`.

  Scale-UP signal: placement CAPACITY denials. The two choke points every
  new-placement path funnels through call `note_denial/2` (an async cast, never
  blocking placement) when demand hits a capacity wall: the cold wake pick
  (`Embervm.WakeInstance`) when NO brick on the node is slot/mem-eligible, and
  the dispatcher miss tier when ready candidates exist but none has budget/mem
  headroom. A denial is attributed to the SMALLEST authorized class whose
  usable capacity plus the admission floor fits the workload's `need_mib`; the
  chart-declared capacity works even before a brick exists. `up_threshold` denials
  (default 3) inside
  `up_window_ms` (default 60s) step the class +1, clamped to `max`.

  Scale-down signal: the class has at least one IDLE brick (a registered,
  non-draining instance with zero live VMs) continuously for `down_idle_ms`
  (default 15m) AND zero denials in the window AND no fleet-full episode inside
  the same window. Steps -1, never below `min`.

  Hysteresis: separate cooldowns per direction (`up_cooldown_ms` default 60s,
  `down_cooldown_ms` default 10m; a scale-up also blocks scale-down for the
  down cooldown), plus the asymmetric windows above, so the loop cannot flap.
  Cooldowns are stamped in `observe` mode too, so the observed log stream is a
  faithful simulation of what `full` would have done.

  ## runtime ceiling ownership

  A positive per-class `ceiling_bound` opts into ADR embervm/042. The declared
  `max` becomes a bootstrap ceiling, and sustained workload-attributed denials
  may raise the operative ceiling by one up to the outer bound. Raising the
  ceiling and growing replicas are deliberately separate reconciliation ticks.
  Missing or zero bounds preserve the fixed-max behavior above. `observe` logs
  ceiling moves, `up` and `full` may raise them, and only `full` retires a raised
  ceiling after the Deployment has stayed at zero replicas for
  `ceiling_idle_ms` (default 60m). Denials and nonzero counts reset that dwell.
  The live `/scale` count reconstructs raised state conservatively after a
  controller restart. Pinned node-floor Deployments remain outside this loop.

  Attributed demand that fits no authorized class, or continues after a class
  reaches its outer bound, latches `no_fitting_class` or `ceiling_exhausted` in
  the capacity report and the Workload `Capacity` condition. The condition is
  transition-only and clears after one denial window without recurrence. It
  never changes kubelet readiness or restarts pods.

  Recorded policy (2026-07-20, values-declared, not hardcoded): per-class
  min-floor `{2gi: 0, 4gi: 0, 8gi: 0, 16gi: 1}` (one 16Gi stays warm for the
  composite group, which must fit a single brick) and per-class max
  `{2gi: 4, 4gi: 3, 8gi: 2, 16gi: 2}` (`bricks.autoscale.minReplicas` /
  `maxReplicas` in the chart values, deep-merged like `desiredReplicas`).

  Drain-aware scale-down (mode `full`): a Deployment picks its own scale-down
  victim, which is unacceptable when a sibling replica holds live VMs or
  un-exported warmth. Before writing `current - 1` the controller selects a SAFE
  victim itself: a brick of the class with ZERO live VMs whose entire warmth
  inventory (stateful bundles, session/serving snapshots, group bundle sets) is
  `exported: true` (refuse-to-strand rail: no safe victim means the scale-down
  is SKIPPED this tick and logged, never forced). It then directs the
  Deployment's choice by annotating the victim pod with a negative
  `controller.kubernetes.io/pod-deletion-cost` and only then PATCHes `/scale`.
  If a placement races a VM onto the victim between the check and the kill, the
  existing bounded-preemption drain (noded SIGTERM -> registry drain edge ->
  `Embervm.DrainCoordinator` force-bank) is the backstop.

  ## fleet-full

  A class is FLEET-FULL when its desired count exceeds the number of dial-home
  REGISTERED bricks of that class for longer than `fleet_full_after_ms` (default
  5m): the controller asked for N bricks but only M < N ever registered, a passive
  sign the scheduler cannot place the rest (the node is full on the fixed homelab;
  on EKS Karpenter would add a node). The controller flags such classes; placement
  reads `fleet_full?/2` to turn a would-be denial into a terminal `:fleet_full`
  (503) rather than a retryable park, and the flag transition is logged + traced
  for an alert. A class recovers (unflagged) the first tick its registered
  count catches up to desired. A flagged class also refuses further autoscale
  UP steps (scaling desired past a scheduler that cannot place is runaway) and
  blocks DOWN steps for the idle window after the episode clears.

  ## catalog-derived floors

  Every reconcile derives a per-class minimum from the workload catalog through
  `Embervm.Brick.Portfolio.floors/2`. The acting minimum is the maximum of the
  computed floor and the chart's manual minimum, so warmth overrides remain in
  force. Static desired counts remain lower bounds too. If a computed floor
  exceeds `max`, the controller raises `:floor_overflow` immediately through a
  transition-only warning and trace, and suppresses scale writes for that class
  until the contradiction clears. Other classes continue reconciling. The
  computation uses declared memory, admission cushion, and slot capacity, so it
  works when a class has zero live replicas.

  ## inert until bricks exist

  With `bricks.enabled=false` the chart renders no brick classes into the CP env,
  so the configured class list is empty and every tick is a no-op: nothing is
  scaled, nothing is flagged. PR-3b flips the classes on with real desired counts;
  this module is written and merged (PR-3a) one step ahead so the mechanism, RBAC,
  and `:fleet_full` router path all soak through a normal CP roll first.

  ## supervision

  A singleton started AFTER `Embervm.Finch` in the supervision tree (it uses the
  K8s client, which dials the apiserver through Finch); the tree's `:rest_for_one`
  strategy means a controller crash restarts only it and its successors, never the
  dispatcher or the registries ahead of it.
  """

  use GenServer
  require Logger
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{BaseBuilder, Brick, K8s, NodeCapacity, WorkloadCatalog}
  alias Embervm.Brick.Portfolio

  @default_interval_ms 30_000
  @default_fleet_full_after_ms 300_000
  @default_up_threshold 3
  @default_up_window_ms 60_000
  @default_up_cooldown_ms 60_000
  @default_down_idle_ms 900_000
  @default_down_cooldown_ms 600_000
  @default_ceiling_idle_ms 3_600_000

  @typedoc """
  One size-class the controller reconciles: its label, static desired replica
  count, the autoscale clamp (`min`/`max`), and optional runtime ceiling outer
  bound. An absent min reads 0, an absent max reads `max(desired, min)`, and an
  absent or zero ceiling bound keeps max fixed.
  """
  @type class :: %{name: String.t(), desired: non_neg_integer()}

  @doc """
  Start options (all optional; production reads from Application env / K8s):

    * `:name`                 - registered name (default `#{inspect(__MODULE__)}`).
    * `:classes`              - `[%{name, desired, min, max, ceiling_bound}]`; default from
      `Application.get_env(:embervm, :brick_classes, [])` (empty = inert).
    * `:mode`                 - autoscale mode `:off | :observe | :up | :full`;
      default `Application.get_env(:embervm, :brick_autoscale_mode, :off)`.
    * `:deployment_prefix`    - the brick Deployment name prefix; a class scales
      `<prefix><class>`. Default from `EMBERVM_BRICK_DEPLOYMENT_PREFIX` app env.
    * `:namespace`            - default `Embervm.K8s.namespace/0`.
    * `:interval_ms`          - reconcile period (default 30s).
    * `:fleet_full_after_ms`  - desired>registered dwell before flagging (default 5m).
    * `:scale_fun`            - `(ns, name, replicas) -> :ok | {:error, term}`,
      default `&Embervm.K8s.scale_deployment/3` (injected in tests).
    * `:scale_get_fun`        - `(ns, name) -> {:ok, replicas} | {:error, term}`,
      the live replica read the autoscale loop bases decisions on; default
      `&Embervm.K8s.get_deployment_scale/2` (injected in tests).
    * `:pods_fun`             - `(ns, label_selector) -> {:ok, [%{name, uid}]}`,
      resolves the scale-down victim's pod name; default
      `&Embervm.K8s.list_pods/2` (injected in tests).
    * `:annotate_fun`         - `(ns, pod, annotations) -> :ok | {:error, term}`,
      sets the victim's pod-deletion-cost; default
      `&Embervm.K8s.annotate_pod/3` (injected in tests).
    * `:registered_fun`       - `() -> %{class => count}` of registered bricks,
      default derived from `Embervm.Brick.by_class/0` (injected in tests).
    * `:facts_fun`            - `() -> [facts]` raw capacity facts (idle/victim
      inputs), default `NodeCapacity.all/1` (injected in tests).
    * `:catalog_fun`          - `() -> [catalog_entry]` declared workload floors,
      default `Embervm.WorkloadCatalog.all/0` (injected in tests).
    * `:up_threshold` / `:up_window_ms` / `:up_cooldown_ms` /
      `:down_idle_ms` / `:down_cooldown_ms` / `:ceiling_idle_ms` - hysteresis
      knobs (defaults 3 / 60s / 60s / 15m / 10m / 60m).
    * `:clock`                - `() -> integer()` ms clock (injected in tests).
    * `:reconcile_on_start`   - reconcile once immediately (default true).
  """
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: Keyword.get(opts, :name, __MODULE__))
  end

  @doc "Whether `class` is currently flagged fleet-full (desired>registered past the dwell)."
  @spec fleet_full?(GenServer.server(), String.t()) :: boolean()
  def fleet_full?(server \\ __MODULE__, class) do
    GenServer.call(server, {:fleet_full?, class})
  end

  @doc "Whether a catalog-derived floor currently exceeds `class`'s declared maximum."
  @spec floor_overflow?(GenServer.server(), String.t()) :: boolean()
  def floor_overflow?(server \\ __MODULE__, class) do
    GenServer.call(server, {:floor_overflow?, class})
  end

  @doc "The set of currently fleet-full size-class labels (introspection/tests)."
  @spec flagged(GenServer.server()) :: MapSet.t()
  def flagged(server \\ __MODULE__) do
    GenServer.call(server, :flagged)
  end

  @doc "The active capacity conditions and current per-class ceilings."
  @spec capacity_health(GenServer.server()) :: map()
  def capacity_health(server \\ __MODULE__) do
    GenServer.call(server, :capacity_health)
  end

  @doc """
  Record one placement CAPACITY denial for a workload needing `need_mib` MiB
  (the scale-up demand signal). Fire-and-forget cast so the placement hot path
  never blocks on (or crashes with) the controller; when the controller is not
  running (tests, DS-only fleets) the cast is a silent no-op. The denial is
  attributed to the smallest authorized class whose capacity fits `need_mib`;
  a need no authorized class can hold is logged/traced and latched in capacity
  health when runtime ceiling ownership is enabled.
  """
  @spec note_denial(non_neg_integer()) :: :ok
  def note_denial(need_mib) when is_integer(need_mib) do
    note_denial(__MODULE__, nil, need_mib)
  end

  @spec note_denial(String.t(), non_neg_integer()) :: :ok
  def note_denial(workload, need_mib)
      when (is_binary(workload) or is_nil(workload)) and is_integer(need_mib) do
    note_denial(__MODULE__, workload, need_mib)
  end

  @spec note_denial(GenServer.server(), non_neg_integer()) :: :ok
  def note_denial(server, need_mib) when is_integer(need_mib) do
    note_denial(server, nil, need_mib)
  end

  @spec note_denial(GenServer.server(), String.t() | nil, non_neg_integer()) :: :ok
  def note_denial(server, workload, need_mib) do
    GenServer.cast(server, {:denial, workload, need_mib})
  end

  @impl true
  def init(opts) do
    state = %{
      classes: Keyword.get(opts, :classes) || Application.get_env(:embervm, :brick_classes, []),
      mode: Keyword.get(opts, :mode) || Application.get_env(:embervm, :brick_autoscale_mode, :off),
      deployment_prefix:
        Keyword.get(opts, :deployment_prefix) ||
          Application.get_env(:embervm, :brick_deployment_prefix, ""),
      namespace: Keyword.get(opts, :namespace) || K8s.namespace(),
      interval_ms: Keyword.get(opts, :interval_ms, @default_interval_ms),
      fleet_full_after_ms: Keyword.get(opts, :fleet_full_after_ms, @default_fleet_full_after_ms),
      scale_fun: Keyword.get(opts, :scale_fun, &K8s.scale_deployment/3),
      scale_get_fun: Keyword.get(opts, :scale_get_fun, &K8s.get_deployment_scale/2),
      pods_fun: Keyword.get(opts, :pods_fun, &K8s.list_pods/2),
      annotate_fun: Keyword.get(opts, :annotate_fun, &K8s.annotate_pod/3),
      registered_fun: Keyword.get(opts, :registered_fun, &registered_by_class/0),
      facts_fun: Keyword.get(opts, :facts_fun, fn -> NodeCapacity.all(NodeCapacity.table()) end),
      catalog_fun: Keyword.get(opts, :catalog_fun, &WorkloadCatalog.all/0),
      up_threshold: Keyword.get(opts, :up_threshold, @default_up_threshold),
      up_window_ms: Keyword.get(opts, :up_window_ms, @default_up_window_ms),
      up_cooldown_ms: Keyword.get(opts, :up_cooldown_ms, @default_up_cooldown_ms),
      down_idle_ms: Keyword.get(opts, :down_idle_ms, @default_down_idle_ms),
      down_cooldown_ms: Keyword.get(opts, :down_cooldown_ms, @default_down_cooldown_ms),
      ceiling_idle_ms:
        Keyword.get(
          opts,
          :ceiling_idle_ms,
          Application.get_env(:embervm, :brick_ceiling_idle_ms, @default_ceiling_idle_ms)
        ),
      condition_fun: Keyword.get(opts, :condition_fun, &BaseBuilder.capacity_condition/2),
      clock: Keyword.get(opts, :clock, &now_ms/0),
      # class => the ms timestamp desired first exceeded registered (cleared when it recovers).
      over_since: %{},
      flagged: MapSet.new(),
      # Classes whose catalog-derived floor exceeds the declared max. Unlike
      # fleet-full this is an immediate configuration contradiction, with no
      # scheduler dwell, and suppresses every scale write until it recovers.
      floor_overflow: MapSet.new(),
      # Autoscale bookkeeping, all per class-name:
      # recent denial timestamps (pruned to up_window_ms each tick),
      denials: %{},
      # since when the class has continuously had >=1 idle brick,
      idle_since: %{},
      # last up/down decision stamps (hysteresis cooldowns),
      last_up_at: %{},
      last_down_at: %{},
      # last tick the class was flagged fleet-full (blocks down for the idle window).
      last_full_at: %{},
      # Runtime-owned per-class ceilings. maxReplicas is only their bootstrap
      # value when a positive ceilingBound explicitly authorizes this feature.
      # A fresh controller reconstructs a raised ceiling from the live replica
      # count, never from an unreadable scale subresource.
      operative_ceiling: %{},
      ceiling_idle_since: %{},
      last_ceiling_up_at: %{},
      # workload => %{reason, size_class, need_mib, last_seen_at}. These are the
      # active, hysteretically held capacity conditions exposed in /v1/capacity.
      capacity_conditions: %{},
      # Classes whose latest capacity-fact snapshot disagrees with declared usable_mib.
      drifted: MapSet.new()
    }

    if Keyword.get(opts, :reconcile_on_start, true), do: send(self(), :reconcile)
    schedule(state)
    {:ok, state}
  end

  @impl true
  def handle_info(:reconcile, state) do
    state = reconcile(state)
    schedule(state)
    {:noreply, state}
  end

  @impl true
  def handle_cast({:denial, workload, need_mib}, state) do
    case class_for_need(state.classes, need_mib, state.facts_fun.()) do
      nil ->
        state =
          if ceiling_feature_enabled?(state.classes) do
            put_capacity_condition(state, workload, :no_fitting_class, nil, need_mib)
          else
            state
          end

        Logger.warning("embervm brick denial cannot be served",
          workload: workload,
          need_mib: need_mib,
          reason: :no_fitting_class
        )

        Tracer.with_span "embervm.brick.denial_unservable", %{
          attributes: %{"ember.need_mib" => need_mib}
        } do
          :ok
        end

        {:noreply, state}

      class ->
        now = state.clock.()
        denial = %{at: now, workload: workload, need_mib: need_mib}
        state = refresh_capacity_condition(state, workload, class, now)

        {:noreply,
         %{
           state
           | denials: Map.update(state.denials, class, [denial], &[denial | &1]),
             ceiling_idle_since: Map.delete(state.ceiling_idle_since, class)
         }}
    end
  end

  @impl true
  def handle_call({:fleet_full?, class}, _from, state) do
    {:reply, MapSet.member?(state.flagged, class), state}
  end

  def handle_call({:floor_overflow?, class}, _from, state) do
    {:reply, MapSet.member?(state.floor_overflow, class), state}
  end

  def handle_call(:flagged, _from, state) do
    {:reply, state.flagged, state}
  end

  def handle_call(:capacity_health, _from, state) do
    {:reply, capacity_health_snapshot(state), state}
  end

  @doc "Run one reconcile synchronously (tests drive the loop deterministically)."
  @spec reconcile_now(GenServer.server()) :: :ok
  def reconcile_now(server \\ __MODULE__) do
    GenServer.call(server, :reconcile_now)
  end

  @impl true
  def handle_call(:reconcile_now, _from, state) do
    {:reply, :ok, reconcile(state)}
  end

  # -- the pure decision function ---------------------------------------------

  @doc """
  The pure autoscale decision for one class: given the LIVE replica count and
  the signal snapshot, return `{target, reason}`. `signals` carries:

    * `:min` / `:max`          - the clamp (max is normalized to `>= min`).
    * `:denials`               - capacity denials attributed to the class inside
      the up window.
    * `:up_threshold`          - denials needed to step up.
    * `:fleet_full_now`        - the class is currently flagged fleet-full
      (refuses UP: desired already outruns what the scheduler can place).
    * `:fleet_full_recent`     - a fleet-full episode inside the down window
      (refuses DOWN: capacity was just proven scarce).
    * `:idle_dwell_ok`         - the class has had an idle brick continuously
      for the down idle window.
    * `:up_cooldown_ok` / `:down_cooldown_ok` - per-direction hysteresis gates.

  One step per decision (`+1`/`-1`); the min-floor jump is the only multi-step
  move (a class below its floor goes straight to `min`). Reasons are the
  machine-readable tail of the decision log line.
  """
  @spec desired(non_neg_integer(), map()) :: {non_neg_integer(), atom()}
  def desired(current, signals) do
    min = Map.get(signals, :min, 0)
    max = max(Map.get(signals, :max, min), min)
    pressure? = signals.denials >= signals.up_threshold

    cond do
      current < min -> {min, :min_floor}
      pressure? and current >= max -> {current, :at_max}
      pressure? and signals.fleet_full_now -> {current, :fleet_full_wait}
      pressure? and not signals.up_cooldown_ok -> {current, :up_cooldown}
      pressure? -> {current + 1, :denial_pressure}
      current > max -> {current - 1, :over_max}
      down_ok?(current, min, signals) -> {current - 1, :idle_drain}
      true -> {current, :steady}
    end
  end

  defp down_ok?(current, min, signals) do
    current > min and signals.denials == 0 and signals.idle_dwell_ok and
      signals.down_cooldown_ok and not signals.fleet_full_recent
  end

  # -- reconcile ---------------------------------------------------------------

  defp reconcile(state) do
    registered = state.registered_fun.()
    facts = state.facts_fun.()
    now = state.clock.()
    state = check_capacity_drift(state, facts)
    state =
      if state.mode == :off,
        do: state,
        else: state |> prune_denials(now) |> track_idle(now, facts)

    portfolio = Portfolio.floors(state.catalog_fun.(), state.classes)
    state = track_floor_overflow(state, portfolio)

    state
    |> reconcile_classes(registered, now, portfolio)
    |> clear_recovered_conditions(now)
  end

  defp reconcile_classes(state, registered, now, portfolio) do
    classes =
      Enum.map(state.classes, fn class ->
        case Map.get(portfolio, class_name(class)) do
          %{effective_min: effective_min} -> Map.put(class, :effective_min, effective_min)
          _ -> class
        end
      end)

    suppressed = MapSet.to_list(state.floor_overflow)
    initial_over_since = Map.take(state.over_since, suppressed)
    initial_flagged = MapSet.intersection(state.flagged, state.floor_overflow)

    {over_since, flagged, state} =
      Enum.reduce(
        classes,
        {initial_over_since, initial_flagged, state},
        fn class, {os, fl, st} ->
          name = class_name(class)

          if MapSet.member?(st.floor_overflow, name) do
            {os, fl, st}
          else
            {plan, st} = plan_class(st, class, now)

            case apply_plan(st, name, plan, now) do
              {nil, st} ->
                # A failed live-scale read leaves both replicas and fleet-full
                # bookkeeping untouched for this class.
                os =
                  case Map.fetch(st.over_since, name) do
                    {:ok, since} -> Map.put(os, name, since)
                    :error -> os
                  end

                fl = if MapSet.member?(st.flagged, name), do: MapSet.put(fl, name), else: fl
                {os, fl, st}

              {acting, st} ->
                reg = Map.get(registered, name, 0)

                if acting > reg do
                  since = Map.get(st.over_since, name, now)
                  os = Map.put(os, name, since)

                  if now - since >= st.fleet_full_after_ms do
                    maybe_flag(st, name, acting, reg)

                    {os, MapSet.put(fl, name),
                     %{st | last_full_at: Map.put(st.last_full_at, name, now)}}
                  else
                    {os, fl, st}
                  end
                else
                  # Caught up (or over-provisioned): clear any prior over-window and flag.
                  maybe_unflag(st, name)
                  {os, fl, st}
                end
            end
          end
        end
      )

    %{state | over_since: over_since, flagged: flagged}
  end

  # The per-class plan: what replica count to ACT with this tick, plus the
  # autoscale decision log. Mode :off is the legacy static reconcile untouched.
  # Every other mode computes the autoscale target off the LIVE /scale read;
  # :observe still acts statically and only logs the target, the acting modes
  # assert the live current and move it in the enabled direction(s). A failed
  # /scale read skips every write for that class. An unreadable live count is
  # never interpreted as zero or replaced with a potentially shrinking static
  # target.
  defp plan_class(state, class, now) do
    name = class_name(class)

    if state.mode == :off do
      {%{target: class_static_target(class), current: nil, decision: nil}, state}
    else
      case read_current(state, name) do
        {:ok, current} ->
          state = reconstruct_ceiling(state, class, current)
          class = clamp_effective_min(class, operative_ceiling(state, class))
          {state, ceiling_changed?} = maybe_move_ceiling(state, class, current, now)

          if ceiling_changed? do
            # Raising a ceiling authorizes headroom, it does not consume it on
            # the same reconciliation tick. The existing replica decision sees
            # the new ceiling on the next tick.
            {%{target: current, current: current, decision: nil}, state}
          else
            state = maybe_signal_ceiling_exhausted(state, class, current)
            execute(state, class, current, now)
          end

        :error ->
          {:skip, state}
      end
    end
  end

  # Decide and (mode permitting) act for one class. What the tick WRITES:
  # :observe always asserts the static desired (legacy behavior, decisions
  # log-only). The acting modes assert the LIVE current as their baseline (the
  # controller keeps re-asserting every tick, staying the single writer) and
  # move it only in the direction(s) the mode enables: :up acts on increases
  # (min-floor jumps included) and leaves decreases as would-scale logs; :full
  # acts on decreases too, but only through the drain-aware victim gate (a
  # skipped victim leaves the count as-is this tick, no cooldown stamped, so
  # the decision retries as soon as a replica is safely removable).
  defp execute(state, class, current, now) do
    name = class_name(class)
    {target, reason} = desired(current, signals(state, class, now))

    cond do
      state.mode == :observe ->
        {%{
           target: class_static_target(class),
           current: current,
           decision: decision(current, target, reason, false)
         }, state}

      target > current ->
        {%{target: target, current: current, decision: decision(current, target, reason, true)}, state}

      target < current and state.mode == :full ->
        case prepare_scale_down(state, name) do
          :ok ->
            {%{target: target, current: current, decision: decision(current, target, reason, true)},
             state}

          {:skip, why} ->
            Logger.info("brick autoscale: skipping scale-down of class #{name} (reason=#{why})")
            {%{target: current, current: current, decision: nil}, state}
        end

      target < current ->
        {%{target: current, current: current, decision: decision(current, target, reason, false)},
         state}

      true ->
        {%{target: current, current: current, decision: nil}, state}
    end
  end

  defp decision(current, target, reason, acted?) when current != target do
    %{current: current, target: target, reason: reason, acted?: acted?}
  end

  defp decision(_current, _target, _reason, _acted?), do: nil

  defp apply_plan(state, _name, :skip, _now), do: {nil, state}

  defp apply_plan(state, name, plan, now) do
    case scale(state, name, plan.target) do
      :ok ->
        state =
          case plan.decision do
            nil -> state
            d -> note_decision(state, name, d.current, d.target, d.reason, now, d.acted?)
          end

        {plan.target, state}

      :error ->
        # Report the last trustworthy read for fleet-full accounting. Most
        # importantly, do not stamp a successful-action cooldown. Static mode
        # has no live read, so a failed write cannot update fleet-full state.
        {plan.current, state}
    end
  end

  defp read_current(state, name) do
    deployment = state.deployment_prefix <> name

    case state.scale_get_fun.(state.namespace, deployment) do
      {:ok, current} ->
        {:ok, current}

      {:error, reason} ->
        # Same tolerance as scale/3: a 404 (not-yet-rendered class) or transient
        # apiserver error skips the autoscale decision for the tick, never crashes.
        Logger.warning("embervm brick autoscale current read failed",
          size_class: name,
          deployment: deployment,
          reason: inspect(reason)
        )

        :error
    end
  end

  defp signals(state, class, now) do
    name = class_name(class)

    %{
      min: class_min(class),
      max: operative_ceiling(state, class),
      denials: length(Map.get(state.denials, name, [])),
      up_threshold: state.up_threshold,
      fleet_full_now: MapSet.member?(state.flagged, name),
      fleet_full_recent: fleet_full_recent?(state, name, now),
      idle_dwell_ok: idle_dwell_ok?(state, name, now),
      up_cooldown_ok: cooldown_ok?(state.last_up_at, name, now, state.up_cooldown_ms),
      down_cooldown_ok:
        cooldown_ok?(state.last_down_at, name, now, state.down_cooldown_ms) and
          cooldown_ok?(state.last_up_at, name, now, state.down_cooldown_ms)
    }
  end

  # A positive ceilingBound explicitly opts a class into runtime ceiling
  # ownership. Missing and zero keep maxReplicas as the ordinary fixed clamp.
  defp reconstruct_ceiling(state, class, current) do
    name = class_name(class)

    if ceiling_enabled?(class) do
      bootstrap = class_max(class)
      bound = authorized_bound(class)
      previous = Map.get(state.operative_ceiling, name, bootstrap)

      # The live count is durable Kubernetes state and is therefore the safe
      # restart reconstruction source. Never reconstruct below it, even if an
      # operator accidentally lowered a bound under already-running replicas.
      ceiling = max(max(previous, bootstrap), current) |> min(max(bound, current))

      %{state | operative_ceiling: Map.put(state.operative_ceiling, name, ceiling)}
    else
      state
    end
  end

  defp maybe_move_ceiling(state, class, current, now) do
    name = class_name(class)

    cond do
      not ceiling_enabled?(class) ->
        {state, false}

      ceiling_up_ready?(state, class, current, now) ->
        move_ceiling_up(state, class, current, now)

      state.mode == :full and ceiling_down_ready?(state, class, current, now) ->
        ceiling = operative_ceiling(state, class)
        next = max(ceiling - 1, class_max(class))

        Logger.info("embervm brick ceiling lowered",
          size_class: name,
          operative_ceiling: next,
          ceiling_bound: authorized_bound(class),
          bootstrap_max: class_max(class),
          reason: :ceiling_idle
        )

        {%{
           state
           | operative_ceiling: Map.put(state.operative_ceiling, name, next),
             ceiling_idle_since: Map.put(state.ceiling_idle_since, name, now)
         }, true}

      true ->
        {track_ceiling_idle(state, class, current, now), false}
    end
  end

  defp ceiling_up_ready?(state, class, current, now) do
    name = class_name(class)
    pressure? = length(Map.get(state.denials, name, [])) >= state.up_threshold
    ceiling = operative_ceiling(state, class)

    pressure? and current >= ceiling and ceiling < authorized_bound(class) and
      cooldown_ok?(state.last_ceiling_up_at, name, now, state.up_cooldown_ms)
  end

  defp move_ceiling_up(state, class, _current, now) do
    name = class_name(class)
    ceiling = operative_ceiling(state, class)
    next = min(ceiling + 1, authorized_bound(class))
    acts? = state.mode in [:up, :full]
    verb = if acts?, do: "raising", else: "would raise"

    events = Map.get(state.denials, name, [])
    latest = Enum.max_by(events, & &1.at, fn -> %{workload: nil, need_mib: nil} end)

    Logger.info("embervm brick ceiling #{verb}",
      workload: latest.workload,
      need_mib: latest.need_mib,
      size_class: name,
      operative_ceiling: next,
      ceiling_bound: authorized_bound(class),
      bootstrap_max: class_max(class),
      reason: :denial_pressure
    )

    state = %{state | last_ceiling_up_at: Map.put(state.last_ceiling_up_at, name, now)}

    if acts? do
      {%{
         state
         | operative_ceiling: Map.put(state.operative_ceiling, name, next),
           ceiling_idle_since: Map.delete(state.ceiling_idle_since, name)
       }, true}
    else
      {state, false}
    end
  end

  # Ceiling retirement starts only after the Deployment itself is at zero.
  # Replica scale-down therefore runs first through all existing drain, warmth,
  # pod-resolution, and deletion-cost rails. A denial or any nonzero count
  # clears this dwell.
  defp track_ceiling_idle(state, class, current, now) do
    name = class_name(class)
    ceiling = operative_ceiling(state, class)
    denials = Map.get(state.denials, name, [])

    if current == 0 and denials == [] and ceiling > class_max(class) do
      %{
        state
        | ceiling_idle_since:
            Map.put(state.ceiling_idle_since, name, Map.get(state.ceiling_idle_since, name, now))
      }
    else
      %{state | ceiling_idle_since: Map.delete(state.ceiling_idle_since, name)}
    end
  end

  defp ceiling_down_ready?(state, class, current, now) do
    name = class_name(class)

    current == 0 and Map.get(state.denials, name, []) == [] and
      operative_ceiling(state, class) > class_max(class) and
      case Map.get(state.ceiling_idle_since, name) do
        nil -> false
        since -> now - since >= state.ceiling_idle_ms
      end
  end

  defp maybe_signal_ceiling_exhausted(state, class, current) do
    name = class_name(class)
    events = Map.get(state.denials, name, [])
    pressure? = length(events) >= state.up_threshold
    ceiling = operative_ceiling(state, class)

    if ceiling_enabled?(class) and pressure? and current >= ceiling and
         ceiling >= authorized_bound(class) do
      events
      |> latest_denial_per_workload()
      |> Enum.reduce(state, fn event, acc ->
        put_capacity_condition(
          acc,
          event.workload,
          :ceiling_exhausted,
          name,
          event.need_mib,
          event.at
        )
      end)
    else
      state
    end
  end

  defp latest_denial_per_workload(events) do
    events
    |> Enum.reduce(%{}, fn event, latest ->
      Map.update(latest, event.workload, event, fn prior ->
        if event.at > prior.at, do: event, else: prior
      end)
    end)
    |> Map.values()
  end

  defp clamp_effective_min(class, ceiling) do
    case class_field(class, [:effective_min, "effective_min"]) do
      value when is_integer(value) -> Map.put(class, :effective_min, min(value, ceiling))
      _ -> class
    end
  end

  # Log the decision and stamp the direction cooldown. Stamped on would-scale
  # decisions (observe, or a direction the mode has not enabled) too, so the
  # observed log stream paces exactly as the acting modes would (one line per
  # cooldown, not one per tick) and a mode flip inherits sane cooldown state.
  defp note_decision(state, name, current, target, reason, now, acted?) do
    verb = if acted?, do: "scaling", else: "would scale"

    cond do
      target == current ->
        state

      target > current ->
        Logger.info(
          "brick autoscale: #{verb} class #{name} from #{current} to #{target} (reason=#{reason})"
        )

        %{state | last_up_at: Map.put(state.last_up_at, name, now)}

      target < current ->
        Logger.info(
          "brick autoscale: #{verb} class #{name} from #{current} to #{target} (reason=#{reason})"
        )

        %{state | last_down_at: Map.put(state.last_down_at, name, now)}
    end
  end

  defp prune_denials(state, now) do
    horizon = now - state.up_window_ms

    denials =
      state.denials
      |> Enum.map(fn {class, events} ->
        {class, Enum.filter(events, &(Map.fetch!(&1, :at) > horizon))}
      end)
      |> Enum.reject(fn {_class, events} -> events == [] end)
      |> Map.new()

    %{state | denials: denials}
  end

  # A class's idle dwell: idle_since[class] holds from the first tick the class
  # had >=1 idle brick (a registered, non-draining instance with zero live VMs)
  # and clears the tick it has none, so the dwell requires CONTINUOUS idleness.
  defp track_idle(state, now, facts) do
    idle_since =
      Enum.reduce(state.classes, %{}, fn class, acc ->
        name = class_name(class)

        if Enum.any?(facts, &idle_brick?(&1, name)) do
          Map.put(acc, name, Map.get(state.idle_since, name, now))
        else
          acc
        end
      end)

    %{state | idle_since: idle_since}
  end

  defp idle_brick?(fact, class) do
    Map.get(fact, :size_class, "") == class and not Map.get(fact, :draining, false) and
      Map.get(fact, :live_vms, 0) == 0
  end

  defp idle_dwell_ok?(state, name, now) do
    case Map.get(state.idle_since, name) do
      nil -> false
      since -> now - since >= state.down_idle_ms
    end
  end

  defp fleet_full_recent?(state, name, now) do
    MapSet.member?(state.flagged, name) or
      case Map.get(state.last_full_at, name) do
        nil -> false
        at -> now - at < state.down_idle_ms
      end
  end

  defp cooldown_ok?(stamps, name, now, cooldown_ms) do
    case Map.get(stamps, name) do
      nil -> true
      at -> now - at >= cooldown_ms
    end
  end

  # -- capacity health --------------------------------------------------------

  defp put_capacity_condition(state, workload, reason, size_class, need_mib)
       when is_binary(workload) and workload != "" do
    put_capacity_condition(state, workload, reason, size_class, need_mib, state.clock.())
  end

  defp put_capacity_condition(state, _workload, _reason, _size_class, _need_mib), do: state

  defp put_capacity_condition(state, workload, reason, size_class, need_mib, seen_at)
       when is_binary(workload) and workload != "" do
    condition = %{
      workload: workload,
      reason: reason,
      size_class: size_class,
      need_mib: need_mib,
      last_seen_at: seen_at
    }

    prior = Map.get(state.capacity_conditions, workload)
    transition? = is_nil(prior) or prior.reason != reason or prior.size_class != size_class

    if transition? do
      Logger.warning("embervm workload capacity unavailable",
        workload: workload,
        need_mib: need_mib,
        size_class: size_class,
        operative_ceiling: ceiling_for_name(state, size_class),
        ceiling_bound: bound_for_name(state.classes, size_class),
        reason: reason
      )

      state.condition_fun.(workload, %{
        status: "False",
        reason: reason,
        message: capacity_message(reason, size_class, need_mib)
      })
    end

    %{state | capacity_conditions: Map.put(state.capacity_conditions, workload, condition)}
  end

  defp put_capacity_condition(state, _workload, _reason, _size_class, _need_mib, _seen_at),
    do: state

  # A continuing attributed denial keeps an already-latched exhaustion active
  # even when Kubernetes cannot provide a fresh scale read. An unknown read is
  # not proof of recovery and must not let the quiet-window timer clear health.
  defp refresh_capacity_condition(state, workload, size_class, seen_at)
       when is_binary(workload) and workload != "" do
    case Map.get(state.capacity_conditions, workload) do
      %{reason: :ceiling_exhausted, size_class: ^size_class} = condition ->
        %{
          state
          | capacity_conditions:
              Map.put(state.capacity_conditions, workload, %{condition | last_seen_at: seen_at})
        }

      _ ->
        state
    end
  end

  defp refresh_capacity_condition(state, _workload, _size_class, _seen_at), do: state

  defp clear_recovered_conditions(state, now) do
    {keep, clear} =
      Enum.split_with(state.capacity_conditions, fn {_workload, condition} ->
        now - condition.last_seen_at < state.up_window_ms
      end)

    Enum.each(clear, fn {workload, condition} ->
      Logger.info("embervm workload capacity recovered",
        workload: workload,
        need_mib: condition.need_mib,
        size_class: condition.size_class,
        operative_ceiling: ceiling_for_name(state, condition.size_class),
        ceiling_bound: bound_for_name(state.classes, condition.size_class),
        reason: condition.reason
      )

      state.condition_fun.(workload, %{
        status: "True",
        reason: :capacity_available,
        message: "capacity is available"
      })
    end)

    %{state | capacity_conditions: Map.new(keep)}
  end

  defp capacity_health_snapshot(state) do
    conditions =
      state.capacity_conditions
      |> Map.values()
      |> Enum.map(&Map.drop(&1, [:last_seen_at]))
      |> Enum.sort_by(&{&1.workload, &1.reason})

    ceilings =
      state.classes
      |> Enum.filter(&ceiling_enabled?/1)
      |> Enum.map(fn class ->
        %{
          size_class: class_name(class),
          bootstrap_max: class_max(class),
          operative_ceiling: operative_ceiling(state, class),
          ceiling_bound: authorized_bound(class)
        }
      end)
      |> Enum.sort_by(& &1.size_class)

    %{ok: conditions == [], conditions: conditions, ceilings: ceilings}
  end

  defp capacity_message(:no_fitting_class, _class, need_mib),
    do: "no authorized brick class fits #{need_mib} MiB"

  defp capacity_message(:ceiling_exhausted, class, need_mib),
    do: "#{class} reached its authorized ceiling while #{need_mib} MiB demand is denied"

  defp ceiling_for_name(_state, nil), do: nil

  defp ceiling_for_name(state, name) do
    case Enum.find(state.classes, &(class_name(&1) == name)) do
      nil -> nil
      class -> operative_ceiling(state, class)
    end
  end

  defp bound_for_name(_classes, nil), do: nil

  defp bound_for_name(classes, name) do
    case Enum.find(classes, &(class_name(&1) == name)) do
      nil -> nil
      class -> authorized_bound(class)
    end
  end

  # -- drain-aware scale-down (mode :full) -------------------------------------

  @deletion_cost_annotation "controller.kubernetes.io/pod-deletion-cost"
  # Any negative cost beats the default (an unannotated sibling reads 0), so the
  # ReplicaSet deletes the chosen victim first when /scale shrinks.
  @victim_deletion_cost "-1000"

  # The warmth inventories a brick advertises in its capacity fact; a victim may
  # only be removed when EVERY entry across them has a current store copy
  # (exported: true), else banked state would be stranded on the dying pod's
  # per-instance warmth root (PR-2.5: a successor pod cannot see it on disk,
  # only the store restore-on-miss path can, and that needs the export).
  @warmth_keys [:stateful_bundles, :session_snapshots, :serving_snapshots, :group_bundle_sets]

  # Select a safe victim and direct the Deployment's scale-down choice at it.
  # :ok means the victim is annotated and the /scale shrink may proceed;
  # {:skip, why} means no replica can be removed safely this tick (the
  # refuse-to-strand rail) or the directing write failed, and the scale-down is
  # deferred, never forced. Between the annotate and the ReplicaSet's kill a
  # placement can still race a VM onto the victim; the existing bounded
  # preemption drain (noded SIGTERM -> registry drain edge -> DrainCoordinator
  # force-bank) is the backstop for exactly that window.
  defp prepare_scale_down(state, class) do
    case pick_victim(state.facts_fun.(), class) do
      nil -> {:skip, :no_safe_victim}
      victim -> direct_victim(state, class, victim)
    end
  end

  # The refuse-to-strand rail: a victim must be a registered, non-draining brick
  # of the class with ZERO live VMs and a fully-exported warmth inventory.
  # Among the safe candidates, remove the one with the least warmth to lose
  # (fewest banked bundles/snapshots; ties fall to the first sorted).
  defp pick_victim(facts, class) do
    facts
    |> Enum.filter(fn f ->
      Map.get(f, :size_class, "") == class and not Map.get(f, :draining, false) and
        Map.get(f, :live_vms, 0) == 0 and warmth_all_exported?(f)
    end)
    |> case do
      [] -> nil
      safe -> Enum.min_by(safe, &length(warmth_inventory(&1)))
    end
  end

  defp warmth_inventory(fact), do: Enum.flat_map(@warmth_keys, &(Map.get(fact, &1) || []))

  # An entry without the flag reads NOT exported (fail-closed: unknown warmth is
  # never assumed store-recoverable).
  defp warmth_all_exported?(fact) do
    Enum.all?(warmth_inventory(fact), fn entry -> Map.get(entry, :exported, false) == true end)
  end

  # Resolve the victim's pod NAME (facts carry only the uid) via the brick
  # selector labels, then set the negative deletion cost on it. Any miss along
  # the way (pods list failure, the victim's pod already gone, the PATCH
  # refused) skips the scale-down rather than shrinking with an undirected
  # victim choice.
  defp direct_victim(state, class, victim) do
    selector = "app.kubernetes.io/component=noded-brick,embervm.jomcgi.dev/size-class=#{class}"

    with {:ok, pods} <- state.pods_fun.(state.namespace, selector),
         %{name: pod_name} <-
           Enum.find(pods, :no_pod, fn pod -> pod.uid == Map.get(victim, :pod_uid) end),
         :ok <-
           state.annotate_fun.(state.namespace, pod_name, %{
             @deletion_cost_annotation => @victim_deletion_cost
           }) do
      :ok
    else
      :no_pod -> {:skip, :victim_pod_not_found}
      {:error, reason} -> {:skip, inspect(reason)}
    end
  end

  # -- denial attribution ------------------------------------------------------

  defp class_for_need(classes, need_mib, facts) do
    reported_floors =
      facts
      |> Enum.group_by(&Map.get(&1, :size_class, ""))
      |> Map.new(fn {name, class_facts} ->
        floors =
          class_facts
          |> Enum.map(&Map.get(&1, :mem_reject_floor_mib))
          |> Enum.filter(&(is_integer(&1) and &1 > 0))

        {name, Enum.max(floors, fn -> nil end)}
      end)

    classes
    |> Enum.filter(&(authorized_bound(&1) > 0))
    |> Enum.map(fn class ->
      name = class_name(class)
      usable = class_field(class, [:usable_mib, "usable_mib"])
      capacity = if is_integer(usable), do: usable, else: class_capacity_mib(name)
      configured_floor = class_field(class, [:mem_reject_floor_mib, "mem_reject_floor_mib"])

      floor =
        Map.get(reported_floors, name) ||
          if(is_integer(configured_floor) and configured_floor > 0, do: configured_floor, else: 512)

      {name, capacity, floor}
    end)
    |> Enum.filter(fn {_name, capacity, floor} ->
      is_integer(capacity) and capacity >= need_mib + floor
    end)
    |> case do
      [] -> nil
      fits -> fits |> Enum.min_by(fn {_name, capacity, _floor} -> capacity end) |> elem(0)
    end
  end

  # Legacy compatibility path for ConfigMaps that predate chart-declared usable_mib.
  # It parses the class nameplate, which is not guest-schedulable capacity in the
  # current daemon model.
  defp class_capacity_mib(name) do
    nameplate_mib(name)
  end

  @doc "Returns the full cgroup nameplate represented by a size-class label."
  @spec nameplate_mib(String.t()) :: non_neg_integer() | nil
  def nameplate_mib(name) do
    case Regex.run(~r/^(\d+)gi$/, name) do
      [_, n] -> String.to_integer(n) * 1024
      _ -> nil
    end
  end

  # -- scale write -------------------------------------------------------------

  defp scale(state, name, desired) do
    deployment = state.deployment_prefix <> name

    case state.scale_fun.(state.namespace, deployment, desired) do
      :ok ->
        :ok

      {:error, reason} ->
        # A 404 (brick Deployment not rendered yet, e.g. bricks.enabled=false) or a
        # transient apiserver error must never crash the loop; the next tick retries.
        Logger.warning("embervm brick scale failed",
          size_class: name,
          deployment: deployment,
          desired: desired,
          reason: inspect(reason)
        )

        :error
    end
  end

  # Log + trace only on the flag TRANSITION (not every tick past the dwell), so the
  # The alert fires once per fleet-full episode, not continuously.
  defp maybe_flag(state, class, desired, registered) do
    unless MapSet.member?(state.flagged, class) do
      Logger.warning("embervm brick fleet full",
        size_class: class,
        desired: desired,
        registered: registered
      )

      Tracer.with_span "embervm.brick.fleet_full", %{
        attributes: %{
          "ember.size_class" => class,
          "ember.desired" => desired,
          "ember.registered" => registered
        }
      } do
        :ok
      end
    end
  end

  defp check_capacity_drift(state, facts) do
    drifted =
      Enum.reduce(state.classes, state.drifted, fn class, seen ->
        name = class_name(class)
        declared = class_field(class, [:usable_mib, "usable_mib"])
        reports = Enum.filter(facts, &(Map.get(&1, :size_class, "") == name))
        mismatch? = is_integer(declared) and reports != [] and
          Enum.any?(reports, &(Map.get(&1, :mem_budget_mib) != declared))

        cond do
          mismatch? and not MapSet.member?(seen, name) ->
            Logger.warning("embervm brick capacity drift",
              size_class: name,
              declared_usable_mib: declared,
              reported_mem_budget_mib: Enum.map(reports, &Map.get(&1, :mem_budget_mib))
            )
            MapSet.put(seen, name)

          not mismatch? -> MapSet.delete(seen, name)
          true -> seen
        end
      end)

    %{state | drifted: drifted}
  end

  defp maybe_unflag(state, class) do
    if MapSet.member?(state.flagged, class) do
      Logger.info("embervm brick fleet full cleared", size_class: class)
    end
  end

  defp track_floor_overflow(state, portfolio) do
    overflow =
      portfolio
      |> Enum.filter(fn {_class, result} -> result.flag == :floor_overflow end)
      |> Map.new()

    overflow_names = overflow |> Map.keys() |> MapSet.new()

    overflow_names
    |> MapSet.difference(state.floor_overflow)
    |> Enum.each(fn class ->
      result = Map.fetch!(overflow, class)

      Logger.warning("embervm brick floor overflow",
        size_class: class,
        computed_floor: result.computed_floor,
        max_replicas: class_max_by_name(state.classes, class),
        reason: :floor_overflow
      )

      Tracer.with_span "embervm.brick.floor_overflow", %{
        attributes: %{
          "ember.size_class" => class,
          "ember.computed_floor" => result.computed_floor,
          "ember.max_replicas" => class_max_by_name(state.classes, class)
        }
      } do
        :ok
      end
    end)

    state.floor_overflow
    |> MapSet.difference(overflow_names)
    |> Enum.each(fn class ->
      Logger.info("embervm brick floor overflow cleared",
        size_class: class,
        reason: :floor_overflow
      )
    end)

    %{state | floor_overflow: overflow_names}
  end

  defp class_max_by_name(classes, name) do
    case Enum.find(classes, &(class_name(&1) == name)) do
      nil -> 0
      class -> authorized_bound(class)
    end
  end

  # Registered bricks per size-class, from the dial-home capacity ledger. Wildcard
  # ("") bricks (the legacy DaemonSet) are bucketed under "" and simply do not
  # match any concrete class's fleet-full accounting.
  defp registered_by_class do
    Brick.by_class()
    |> Map.new(fn {class, bricks} -> {class, length(bricks)} end)
  end

  defp class_name(%{name: n}), do: n
  defp class_name(%{"name" => n}), do: n
  defp class_desired(%{desired: d}), do: d
  defp class_desired(%{"desired" => d}), do: d
  defp class_desired(_), do: 0

  defp class_min(class), do: class_field(class, [:effective_min, "effective_min", :min, "min"]) || 0

  defp class_static_target(class), do: max(class_desired(class), class_min(class))

  # Absent max reads max(desired, min): a class the values never granted headroom
  # cannot be scaled past its static count (fail-safe for an env that predates
  # the autoscale fields).
  defp class_max(class) do
    class_field(class, [:max, "max"]) || max(class_desired(class), class_min(class))
  end

  defp class_ceiling_bound(class), do: class_field(class, [:ceiling_bound, "ceiling_bound"]) || 0

  defp ceiling_enabled?(class), do: class_ceiling_bound(class) > 0

  defp ceiling_feature_enabled?(classes), do: Enum.any?(classes, &ceiling_enabled?/1)

  # A bound below the bootstrap max cannot revoke already-authorized static
  # capacity. It simply grants no runtime headroom.
  defp authorized_bound(class), do: max(class_ceiling_bound(class), class_max(class))

  defp operative_ceiling(state, class) do
    if ceiling_enabled?(class) do
      Map.get(state.operative_ceiling, class_name(class), class_max(class))
    else
      class_max(class)
    end
  end

  defp class_field(class, keys), do: Enum.find_value(keys, &Map.get(class, &1))

  defp schedule(state), do: Process.send_after(self(), :reconcile, state.interval_ms)

  defp now_ms, do: System.monotonic_time(:millisecond)
end
