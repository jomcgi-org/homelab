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
  headroom. A denial is attributed to the SMALLEST configured class whose
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

  alias Embervm.{Brick, K8s, NodeCapacity}

  @default_interval_ms 30_000
  @default_fleet_full_after_ms 300_000
  @default_up_threshold 3
  @default_up_window_ms 60_000
  @default_up_cooldown_ms 60_000
  @default_down_idle_ms 900_000
  @default_down_cooldown_ms 600_000

  @typedoc """
  One size-class the controller reconciles: its label, static desired replica
  count, and the autoscale clamp (`min`/`max`, optional: absent min reads 0,
  absent max reads `max(desired, min)`, i.e. no autoscale headroom).
  """
  @type class :: %{name: String.t(), desired: non_neg_integer()}

  @doc """
  Start options (all optional; production reads from Application env / K8s):

    * `:name`                 - registered name (default `#{inspect(__MODULE__)}`).
    * `:classes`              - `[%{name, desired, min, max}]`; default from
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
    * `:up_threshold` / `:up_window_ms` / `:up_cooldown_ms` /
      `:down_idle_ms` / `:down_cooldown_ms` - hysteresis knobs (defaults
      3 / 60s / 60s / 15m / 10m).
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

  @doc "The set of currently fleet-full size-class labels (introspection/tests)."
  @spec flagged(GenServer.server()) :: MapSet.t()
  def flagged(server \\ __MODULE__) do
    GenServer.call(server, :flagged)
  end

  @doc """
  Record one placement CAPACITY denial for a workload needing `need_mib` MiB
  (the scale-up demand signal). Fire-and-forget cast so the placement hot path
  never blocks on (or crashes with) the controller; when the controller is not
  running (tests, DS-only fleets) the cast is a silent no-op. The denial is
  attributed to the smallest configured class whose capacity fits `need_mib`;
  a need no class can hold is logged/traced and dropped (scaling any class would not help).
  """
  @spec note_denial(GenServer.server(), non_neg_integer()) :: :ok
  def note_denial(server \\ __MODULE__, need_mib) do
    GenServer.cast(server, {:denial, need_mib})
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
      up_threshold: Keyword.get(opts, :up_threshold, @default_up_threshold),
      up_window_ms: Keyword.get(opts, :up_window_ms, @default_up_window_ms),
      up_cooldown_ms: Keyword.get(opts, :up_cooldown_ms, @default_up_cooldown_ms),
      down_idle_ms: Keyword.get(opts, :down_idle_ms, @default_down_idle_ms),
      down_cooldown_ms: Keyword.get(opts, :down_cooldown_ms, @default_down_cooldown_ms),
      clock: Keyword.get(opts, :clock, &now_ms/0),
      # class => the ms timestamp desired first exceeded registered (cleared when it recovers).
      over_since: %{},
      flagged: MapSet.new(),
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
  def handle_cast({:denial, need_mib}, state) do
    case class_for_need(state.classes, need_mib, state.facts_fun.()) do
      nil ->
        Logger.warning("embervm brick denial cannot be served", need_mib: need_mib)

        Tracer.with_span "embervm.brick.denial_unservable", %{
          attributes: %{"ember.need_mib" => need_mib}
        } do
          :ok
        end

        {:noreply, state}

      class ->
        now = state.clock.()
        {:noreply, %{state | denials: Map.update(state.denials, class, [now], &[now | &1])}}
    end
  end

  @impl true
  def handle_call({:fleet_full?, class}, _from, state) do
    {:reply, MapSet.member?(state.flagged, class), state}
  end

  def handle_call(:flagged, _from, state) do
    {:reply, state.flagged, state}
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
    state = if state.mode == :off, do: state, else: state |> prune_denials(now) |> track_idle(now)

    {over_since, flagged, state} =
      Enum.reduce(state.classes, {%{}, MapSet.new(), state}, fn class, {os, fl, st} ->
        name = class_name(class)
        {acting, st} = plan_class(st, class, now)

        scale(st, name, acting)

        reg = Map.get(registered, name, 0)

        if acting > reg do
          since = Map.get(st.over_since, name, now)
          os = Map.put(os, name, since)

          if now - since >= st.fleet_full_after_ms do
            maybe_flag(st, name, acting, reg)
            {os, MapSet.put(fl, name), %{st | last_full_at: Map.put(st.last_full_at, name, now)}}
          else
            {os, fl, st}
          end
        else
          # Caught up (or over-provisioned): clear any prior over-window and flag.
          maybe_unflag(st, name)
          {os, fl, st}
        end
      end)

    %{state | over_since: over_since, flagged: flagged}
  end

  # The per-class plan: what replica count to ACT with this tick, plus the
  # autoscale decision log. Mode :off is the legacy static reconcile untouched.
  # Every other mode computes the autoscale target off the LIVE /scale read;
  # :observe still acts statically and only logs the target, the acting modes
  # assert the live current and move it in the enabled direction(s). A failed
  # /scale read degrades that class to the static path for the tick (the
  # decision needs a trustworthy current).
  defp plan_class(state, class, now) do
    name = class_name(class)
    static = class_desired(class)

    with true <- state.mode != :off,
         {:ok, current} <- read_current(state, name) do
      execute(state, class, current, now)
    else
      _ -> {static, state}
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
        {class_desired(class), note_decision(state, name, current, target, reason, now, false)}

      target > current ->
        {target, note_decision(state, name, current, target, reason, now, true)}

      target < current and state.mode == :full ->
        case prepare_scale_down(state, name) do
          :ok ->
            {target, note_decision(state, name, current, target, reason, now, true)}

          {:skip, why} ->
            Logger.info("brick autoscale: skipping scale-down of class #{name} (reason=#{why})")
            {current, state}
        end

      target < current ->
        {current, note_decision(state, name, current, target, reason, now, false)}

      true ->
        {current, state}
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
      max: class_max(class),
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
      |> Enum.map(fn {class, stamps} -> {class, Enum.filter(stamps, &(&1 > horizon))} end)
      |> Enum.reject(fn {_class, stamps} -> stamps == [] end)
      |> Map.new()

    %{state | denials: denials}
  end

  # A class's idle dwell: idle_since[class] holds from the first tick the class
  # had >=1 idle brick (a registered, non-draining instance with zero live VMs)
  # and clears the tick it has none, so the dwell requires CONTINUOUS idleness.
  defp track_idle(state, now) do
    facts = state.facts_fun.()

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
    floors =
      facts
      |> Enum.group_by(&Map.get(&1, :size_class, ""))
      |> Map.new(fn {name, class_facts} ->
        floors = Enum.map(class_facts, &mem_reject_floor_mib/1)
        {name, Enum.max(floors, fn -> 512 end)}
      end)

    classes
    |> Enum.map(fn class ->
      name = class_name(class)
      usable = class_field(class, [:usable_mib, "usable_mib"])
      capacity = if is_integer(usable), do: usable, else: class_capacity_mib(name)
      floor = Map.get(floors, name, 512)
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

  # Mirrors noded/server/pressure.go's memRejectFloorMib rule: only a configured
  # positive integer is used; zero, negative, and non-integer values mean unset.
  defp mem_reject_floor_mib(fact) do
    case Map.get(fact, :mem_reject_floor_mib) do
      floor when is_integer(floor) and floor > 0 -> floor
      _ -> 512
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

  defp class_min(class), do: class_field(class, [:min, "min"]) || 0

  # Absent max reads max(desired, min): a class the values never granted headroom
  # cannot be scaled past its static count (fail-safe for an env that predates
  # the autoscale fields).
  defp class_max(class) do
    class_field(class, [:max, "max"]) || max(class_desired(class), class_min(class))
  end

  defp class_field(class, keys), do: Enum.find_value(keys, &Map.get(class, &1))

  defp schedule(state), do: Process.send_after(self(), :reconcile, state.interval_ms)

  defp now_ms, do: System.monotonic_time(:millisecond)
end
