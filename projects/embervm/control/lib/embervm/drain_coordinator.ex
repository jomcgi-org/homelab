defmodule Embervm.DrainCoordinator do
  @moduledoc """
  Force-banks every live workload on a draining node within the bounded-preemption
  window (R6 Continuity, ADR embervm/009).

  noded has no lifecycle authority: on SIGTERM it only sets `draining` and publishes
  a deadline on its NodeStatus, then holds the gRPC surface up. The control plane is
  what actually evacuates state. NodeRegistry watches that stream and, on the drain
  RISING edge, sends this coordinator `{:node_draining, node_id, pod_uid, deadline_ms}`. This
  coordinator then asks each class sweeper to force-bank its live instances on that
  node (stateful with COMMIT-despite-parked semantics, groups as whole bundle sets,
  sessions and serving via their bank verbs), so a routine noded roll never
  cold-boots a stateful workload and never destroys a banked group.

  It is deliberately thin: each sweeper owns its own instances, admission, per-node
  bank concurrency, and worker lifecycle, so this coordinator only fans out the four
  `drain_node/2` calls and records the drain edge in the op-log. Each call returns
  promptly (it starts async bank workers and returns a count); the actual banks
  complete against noded, which holds shutdown until they do or the deadline passes.
  A class whose sweeper is down or raises is logged and skipped: the daemon's own
  deadline reap is the backstop, and a partial evacuation is strictly better than
  wedging the rest.

  The `safety_margin_ms` env (EMBERVM_DRAIN_SAFETY_MARGIN_MS, default 15000) is
  recorded on the op so the actual bank wall time can be compared against the
  window; the hard bound lives on noded (it holds until the deadline).
  """
  use GenServer

  require Logger

  # Tracer.with_span/set_attributes are OpenTelemetry.Tracer MACROS, so the module
  # is required (not aliased as a runtime dep). Matches the manager/sweeper idiom.
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.OpLog.Op

  @default_safety_margin_ms 15_000

  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @impl true
  def init(opts) do
    # The backend module the default append_fun below dispatches through,
    # threaded alongside :op_log (the server address) so a non-default backend
    # never requires editing this module. Defaults to the same SQLite module
    # :op_log defaults to the selected backend module.
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)

    state = %{
      tenant: Keyword.get(opts, :tenant, "homelab"),
      op_log: op_log,
      op_log_mod: op_log_mod,
      safety_margin_ms: Keyword.get(opts, :safety_margin_ms, @default_safety_margin_ms),
      clock: Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end),
      stateful: Keyword.get(opts, :stateful_sweeper, Embervm.StatefulSweeper),
      serving: Keyword.get(opts, :serving_sweeper, Embervm.ServingSweeper),
      session: Keyword.get(opts, :session_manager, Embervm.SessionManager),
      group: Keyword.get(opts, :group_sweeper, Embervm.GroupSweeper),
      # The per-class drain call, seamed for tests. Production dispatches to the
      # sweeper module's drain_node/2 (a GenServer.call to the named process).
      drain_fun:
        Keyword.get(opts, :drain_fun, fn _class, server, node_id ->
          server.drain_node(server, node_id)
        end),
      # The op-log append, seamed for tests. Production appends the audit op to the
      # configured backend (op_log_mod); a test records it instead.
      append_fun: Keyword.get(opts, :append_fun, fn op_log, op -> op_log_mod.append(op_log, op) end)
    }

    {:ok, state}
  end

  @impl true
  # NodeRegistry sends the drain edge scoped to the INSTANCE (node + pod_uid, R0
  # PR-2): a surge roll drains only the old pod. The sweepers key their live VMs by
  # NODE (the daemon reports VMs per node, and today there is one instance per
  # node), so force-bank is still dispatched per node; pod_uid is recorded on the
  # op and span so an instance-scoped drain is auditable and a future
  # instance-granular sweeper can consume it. The legacy 3-tuple (no pod_uid) is
  # still accepted for a NodeRegistry that predates this change.
  def handle_info({:node_draining, node_id, pod_uid, deadline_ms}, state) do
    handle_drain(state, node_id, pod_uid, deadline_ms)
    {:noreply, state}
  end

  def handle_info({:node_draining, node_id, deadline_ms}, state) do
    handle_drain(state, node_id, "", deadline_ms)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  defp handle_drain(state, node_id, pod_uid, deadline_ms) do
    # The `node_drain` ROOT span (Task 11): a timer-driven fan-out with no caller
    # trace, so a root, mirroring the forced_roll span shape. Bounds the whole
    # force-bank dispatch and carries the per-class banked counts as attributes so
    # a drain that under-banks a class is visible in SigNoz (Task 11 alert reads
    # the same counts off the op-log).
    Tracer.with_span "embervm.node_drain",
                     %{
                       attributes: %{
                         "ember.node_id" => node_id,
                         "ember.pod_uid" => pod_uid,
                         "ember.drain_deadline_ms" => deadline_ms
                       }
                     } do
      Logger.info("embervm drain: instance draining, force-banking all classes",
        node_id: node_id,
        pod_uid: pod_uid,
        deadline_ms: deadline_ms
      )

      append_op(state, :node_drain_started, %{
        node_id: node_id,
        pod_uid: pod_uid,
        deadline_ms: deadline_ms,
        safety_margin_ms: state.safety_margin_ms
      })

      # Priority order inside the drain budget (ADR embervm/009 resolved-question 5):
      # durable banks first (stateful, group, session are all durable snapshots),
      # serving banks second. In-flight builds are the last priority and are handled
      # by noded itself, not this coordinator: they are reconstructible, so noded
      # finishes-or-aborts them on the drain clock after this force-bank pass. Each
      # call is prompt (starts async workers, returns a count); the ordering decides
      # which class's workers are enqueued first, so durable state wins the budget.
      counts = %{
        stateful: drain_class(state, :stateful, node_id),
        group: drain_class(state, :group, node_id),
        session: drain_class(state, :session, node_id),
        serving: drain_class(state, :serving, node_id)
      }

      Tracer.set_attributes(%{
        "ember.stateful_banked" => counts.stateful,
        "ember.group_banked" => counts.group,
        "ember.session_banked" => counts.session,
        "ember.serving_banked" => counts.serving
      })

      finished = counts |> Map.put(:node_id, node_id) |> Map.put(:pod_uid, pod_uid)
      Logger.info("embervm drain: force-bank dispatched", Keyword.new(finished))

      append_op(state, :node_drain_finished, finished)
      :ok
    end
  end

  # Best-effort per class: a sweeper that is down or raises must not wedge the drain
  # of the other classes. Returns the count of instances whose bank was started, 0
  # on any failure.
  defp drain_class(state, class, node_id) do
    state.drain_fun.(class, Map.fetch!(state, class), node_id)
  rescue
    e ->
      Logger.warning("embervm drain: class drain raised", class: class, error: inspect(e))
      0
  catch
    kind, reason ->
      Logger.warning("embervm drain: class drain exited", class: class, reason: inspect({kind, reason}))
      0
  end

  defp append_op(state, kind, payload) do
    op = %Op{kind: kind, tenant: state.tenant, ts: state.clock.(), payload: payload}
    _ = state.append_fun.(state.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm drain: op-log append raised", kind: kind, error: inspect(e))
      :ok
  end
end
