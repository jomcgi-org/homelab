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

  ## fleet-full

  A class is FLEET-FULL when its desired count exceeds the number of dial-home
  REGISTERED bricks of that class for longer than `fleet_full_after_ms` (default
  5m): the controller asked for N bricks but only M < N ever registered, a passive
  sign the scheduler cannot place the rest (the node is full on the fixed homelab;
  on EKS Karpenter would add a node). The controller flags such classes; placement
  reads `fleet_full?/2` to turn a would-be denial into a terminal `:fleet_full`
  (503) rather than a retryable park, and the flag transition is logged + traced
  for a SigNoz alert. A class recovers (unflagged) the first tick its registered
  count catches up to desired.

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

  alias Embervm.{BrickLedger, K8s}

  @default_interval_ms 30_000
  @default_fleet_full_after_ms 300_000

  @typedoc "One size-class the controller reconciles: its label and desired replica count."
  @type class :: %{name: String.t(), desired: non_neg_integer()}

  @doc """
  Start options (all optional; production reads from Application env / K8s):

    * `:name`                 - registered name (default `#{inspect(__MODULE__)}`).
    * `:classes`              - `[%{name, desired}]`; default from
      `Application.get_env(:embervm, :brick_classes, [])` (empty = inert).
    * `:deployment_prefix`    - the brick Deployment name prefix; a class scales
      `<prefix><class>`. Default from `EMBERVM_BRICK_DEPLOYMENT_PREFIX` app env.
    * `:namespace`            - default `Embervm.K8s.namespace/0`.
    * `:interval_ms`          - reconcile period (default 30s).
    * `:fleet_full_after_ms`  - desired>registered dwell before flagging (default 5m).
    * `:scale_fun`            - `(ns, name, replicas) -> :ok | {:error, term}`,
      default `&Embervm.K8s.scale_deployment/3` (injected in tests).
    * `:registered_fun`       - `() -> %{class => count}` of registered bricks,
      default derived from `Embervm.BrickLedger.by_class/0` (injected in tests).
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

  @impl true
  def init(opts) do
    state = %{
      classes: Keyword.get(opts, :classes) || Application.get_env(:embervm, :brick_classes, []),
      deployment_prefix:
        Keyword.get(opts, :deployment_prefix) ||
          Application.get_env(:embervm, :brick_deployment_prefix, ""),
      namespace: Keyword.get(opts, :namespace) || K8s.namespace(),
      interval_ms: Keyword.get(opts, :interval_ms, @default_interval_ms),
      fleet_full_after_ms: Keyword.get(opts, :fleet_full_after_ms, @default_fleet_full_after_ms),
      scale_fun: Keyword.get(opts, :scale_fun, &K8s.scale_deployment/3),
      registered_fun: Keyword.get(opts, :registered_fun, &registered_by_class/0),
      clock: Keyword.get(opts, :clock, &now_ms/0),
      # class => the ms timestamp desired first exceeded registered (cleared when it recovers).
      over_since: %{},
      flagged: MapSet.new()
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

  # -- reconcile ---------------------------------------------------------------

  defp reconcile(state) do
    registered = state.registered_fun.()
    now = state.clock.()

    {over_since, flagged} =
      Enum.reduce(state.classes, {%{}, MapSet.new()}, fn class, {os, fl} ->
        name = class_name(class)
        desired = class_desired(class)

        scale(state, name, desired)

        reg = Map.get(registered, name, 0)

        if desired > reg do
          since = Map.get(state.over_since, name, now)
          os = Map.put(os, name, since)

          if now - since >= state.fleet_full_after_ms do
            maybe_flag(state, name, desired, reg)
            {os, MapSet.put(fl, name)}
          else
            {os, fl}
          end
        else
          # Caught up (or over-provisioned): clear any prior over-window and flag.
          maybe_unflag(state, name)
          {os, fl}
        end
      end)

    %{state | over_since: over_since, flagged: flagged}
  end

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
  # SigNoz alert fires once per fleet-full episode, not continuously.
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

  defp maybe_unflag(state, class) do
    if MapSet.member?(state.flagged, class) do
      Logger.info("embervm brick fleet full cleared", size_class: class)
    end
  end

  # Registered bricks per size-class, from the dial-home capacity ledger. Wildcard
  # ("") bricks (the legacy DaemonSet) are bucketed under "" and simply do not
  # match any concrete class's fleet-full accounting.
  defp registered_by_class do
    BrickLedger.by_class()
    |> Map.new(fn {class, bricks} -> {class, length(bricks)} end)
  end

  defp class_name(%{name: n}), do: n
  defp class_name(%{"name" => n}), do: n
  defp class_desired(%{desired: d}), do: d
  defp class_desired(%{"desired" => d}), do: d
  defp class_desired(_), do: 0

  defp schedule(state), do: Process.send_after(self(), :reconcile, state.interval_ms)

  defp now_ms, do: System.monotonic_time(:millisecond)
end
