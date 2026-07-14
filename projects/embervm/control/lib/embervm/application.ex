defmodule Embervm.Application do
  @moduledoc """
  Root OTP application for the EmberVM control plane.

  R0 skeleton: the supervision tree holds the op-log (durable task-state
  seam), the ETS hot-set task store built on top of it, the Finch HTTP-client
  pool (K8s API calls: TokenReview in Task 8, the Workload CRD reconcile loop
  here in Task 5), the `Embervm.WorkloadWatcher` reconciler, and the
  Bandit-hosted `Embervm.Router` serving `/healthz` (and the `/v1` task routes
  in Task 8). Later tasks add children under this same tree (the
  NodeRegistry, the Dispatcher), which is exactly why the control plane is on
  the BEAM: each of those is a supervised process with its own failure
  domain, restarted in isolation.

  Strategy is `:rest_for_one`, not `:one_for_one`: `Embervm.TaskStore` reads
  the op-log once on init to rebuild its ETS tables and otherwise depends on
  it being alive for every write. If `Embervm.OpLog.SQLite` crashes and
  restarts, `:rest_for_one` also restarts every child listed after it, so the
  rebuild-on-boot path runs again against the op-log's post-restart state
  instead of leaving ETS stale against an op-log that came back with (in the
  worst case) a different in-memory connection. The ordering also puts `Finch`
  and the `Bandit` listener LAST, after both the op-log and the task store: the
  router's handlers call `TaskStore` (and, in Task 8, an auth reviewer over
  Finch), so the HTTP surface must not start accepting requests until its
  dependencies are up. `Finch` itself has no dependency on the op-log, but
  sitting before `Bandit` guarantees the client pool exists the moment the
  router can be hit.
  """
  use Application

  @impl true
  def start(_type, _args) do
    port = http_port()

    # Quota + usage-admin config into app-env BEFORE the supervisor starts, so the
    # Dispatcher (reads the quota budgets at init) and the Router (reads them and
    # the usage-admin list per request) see them. Empty budgets = quota off.
    Application.put_env(:embervm, :quota, quota_config())
    Application.put_env(:embervm, :usage_admins, usage_admins())

    children = [
      # The sync-wait waiter registry + park-count ETS owner come FIRST: every
      # terminal task-state write in TaskStore calls Embervm.SyncWait.notify,
      # which dispatches through this registry, so it must already exist. Both
      # are dependency-free and effectively never crash, so leading the
      # rest_for_one chain costs nothing.
      {Registry, keys: :duplicate, name: Embervm.TaskWaiters},
      Embervm.SyncWait,
      {Embervm.OpLog.SQLite, path: oplog_path()},
      # TaskStore fires on_metered after a :succeeded/:failed op with usage lands,
      # so Embervm.Metering charges the quota cache off the same durable write.
      {Embervm.TaskStore, [on_metered: &Embervm.Metering.on_metered/1]},
      # Finch (the shared HTTP pool, TLS-pinned to the K8s CA in-cluster) before
      # Embervm.Auth, whose TokenReview reviewer dials the API server over it.
      Embervm.K8s.finch_child_spec(),
      {Embervm.Auth, allowed: allowed_service_accounts()},
      # The base builder (Task 10): drives the node daemon's BuildBase RPC on
      # Workload admission / spec change and writes status.snapshotRef +
      # Ready/BaseBuilt conditions. Placed BEFORE the WorkloadWatcher on purpose:
      # the watcher's boot LIST casts each valid Workload into the builder, so
      # the builder must already be up to receive them, and under :rest_for_one a
      # builder restart also restarts the watcher, whose re-LIST re-drives the
      # (idempotent) reconcile. It writes status over the Finch pool above and
      # dials the daemon over its own Mint gRPC connection (per build), so it
      # depends on Finch but not on the watcher or node registry. Empty node
      # config (no daemon wired) means it holds descriptors and builds nothing.
      {Embervm.BaseBuilder, nodes: configured_nodes()},
      # The Workload informer (Task 5): LISTs then WATCHes Workload CRs over the
      # Finch pool above and writes Embervm.WorkloadCatalog, which
      # TaskStore.cfg_for/1 reads. Placed after Finch (its watch streams over
      # that pool) and after TaskStore in the supervision list; TaskStore does
      # not depend on the watcher being up (WorkloadCatalog.retry_config/1
      # tolerates the catalog table not existing yet), so their relative order
      # here is not load-bearing.
      Embervm.WorkloadWatcher,
      # The node registry (Task 9): one supervised gRPC stream per configured node
      # daemon, consuming WatchNode into the Embervm.NodeCapacity ETS table the
      # dispatcher (Task 11) reads, and reassigning a downed node's in-flight tasks
      # via Embervm.TaskStore. Placed after TaskStore (its reassignment path calls
      # it) and after WorkloadWatcher; it dials the daemon directly over its own
      # Mint gRPC connection, so it does not depend on the Finch pool. With no node
      # wired (empty address), it supervises an empty node list and does nothing.
      {Embervm.NodeRegistry, nodes: configured_nodes()},
      # The shared per-node gRPC channel holder (Task 11): one long-lived, reused
      # Mint channel per node for the Prime/Assign hot path (unlike NodeRegistry/
      # BaseBuilder, which each own their own channel and can afford a per-op
      # connect). Lazy-dials and caches in persistent_term for lock-free worker
      # reads; workers invalidate a channel on a transport error. With no node
      # wired it caches nothing.
      {Embervm.NodeChannel, nodes: configured_nodes()},
      # Metering, audit, and quotas (Task 12). Owns the public per-principal daily
      # quota-cache ETS table (rebuilt on boot from the op-log's usage projection)
      # and appends request-scoped denials. Placed AFTER OpLog.SQLite (it reads the
      # usage projection on boot and appends denials) and BEFORE the Dispatcher and
      # Router, which read its table and quota budgets: under :rest_for_one a
      # Metering restart bounces the dispatcher (which rebuilds from backlog) and
      # Bandit, the same price BaseBuilder/NodeRegistry already pay for their
      # ordering. The TaskStore charge hook targets the module (the public table),
      # not the process, so TaskStore may start earlier.
      {Embervm.Metering, []},
      # The dispatcher (Task 11): the heart of R0. Owns the per-workload fair
      # queues, the primed-VM inventory, the enforcement caps, and drives queued
      # tasks to terminal via Assign. Placed AFTER TaskStore (drives its FSM +
      # is the target of its on_queued hook), NodeRegistry (reads NodeCapacity),
      # WorkloadWatcher (reads WorkloadCatalog), NodeChannel (the assign channel),
      # and Metering (reads its quota table + budgets). Under :rest_for_one a
      # dispatcher restart loses its in-memory queues, which its boot backlog-sweep
      # rebuilds from TaskStore.
      {Embervm.Dispatcher, dispatcher_opts()},
      # The pool manager (Task 11): the background refill loop keeping `floor`
      # VMs primed per workload (floor-first, then proportional to queue depth),
      # depositing primed vm_ids into the dispatcher's inventory and owning
      # status.primedFloorSatisfied. After the dispatcher (it deposits into it)
      # and after the node registry/watcher (it reads capacity + catalog). Primes
      # nothing in R0 (no base is ready until guest images land, Task 14).
      {Embervm.PoolManager, pool_opts()},
      # The cron trigger adapter (Task 11): fires each Workload's spec.triggers[]
      # cron as an ordinary submit (principal system:cron:<workload>); misfires
      # during downtime are skipped, not replayed. After the watcher (reads the
      # catalog's triggers) and TaskStore (submits into it).
      Embervm.Trigger.Cron,
      # Bandit + the router last: its handlers call Auth, TaskStore, SyncWait, and
      # the dispatcher's admit? gate, so the HTTP surface must not accept requests
      # until all are up.
      {Bandit, plug: Embervm.Router, scheme: :http, port: port}
    ]

    opts = [strategy: :rest_for_one, name: Embervm.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # Port comes from the environment (EMBERVM_HTTP_PORT), matching the chart's
  # env wiring; defaults to 8080 for local mix runs.
  defp http_port do
    case System.get_env("EMBERVM_HTTP_PORT") do
      nil -> 8080
      "" -> 8080
      value -> String.to_integer(value)
    end
  end

  # The op-log's durable path comes from EMBERVM_OPLOG_PATH (the chart wires
  # this to the pod's PVC mount); unset (mix test, local runs) falls back to
  # a per-run temp file so nothing collides across processes/nodes and no
  # state leaks between runs. nil here just means "let Embervm.OpLog.SQLite
  # pick its own default", which is that same tmp-file fallback.
  defp oplog_path do
    case System.get_env("EMBERVM_OPLOG_PATH") do
      nil -> nil
      "" -> nil
      path -> path
    end
  end

  # The configured node daemons the registry consumes WatchNode from. v1 wires
  # exactly one, from chart values (EMBERVM_NODE_ID + EMBERVM_NODE_ADDRESS); the
  # registry interface takes a LIST so multi-node needs no reshaping here. An
  # empty address (no daemon wired yet) yields an empty list, so the registry
  # supervises nothing rather than crash-looping on a bad dial. The id falls back
  # to the address when unset, purely for correlation labels.
  #
  # THIS STATIC-VALUES SOURCE IS SINGLE-NODE ONLY. It is correct today because
  # noded is one node-pinned Deployment (replicas: 1) behind a ClusterIP Service,
  # so the one address resolves to the one pod. It is NOT compatible with a
  # DaemonSet: a ClusterIP Service load-balances a single stream to one arbitrary
  # pod (the registry would see "one node" while N-1 daemons stay invisible, and
  # read capacity from the wrong pod), and values cannot enumerate churning pod
  # IPs. The multi-node source is endpoint discovery: a HEADLESS noded Service
  # (clusterIP: None) plus an EndpointSlice/Pod watch that opens a stream per pod
  # and ages one out when its pod drains. That swap changes ONLY this function
  # (it produces the same [%{id, address}] list); Embervm.NodeRegistry, its ETS
  # projection, age-out, and reassignment are unchanged. Land it when noded
  # actually becomes a DaemonSet.
  defp configured_nodes do
    address = trimmed_env("EMBERVM_NODE_ADDRESS")
    id = trimmed_env("EMBERVM_NODE_ID")

    cond do
      address == "" -> []
      id == "" -> [%{id: address, address: address}]
      true -> [%{id: id, address: address}]
    end
  end

  defp trimmed_env(name) do
    case System.get_env(name) do
      nil -> ""
      value -> String.trim(value)
    end
  end

  # Dispatcher tuning from the chart (values -> env). The share fraction, when
  # set, caps each principal at that fraction of a workload's cap; unset (the
  # default) means the dynamic cap/active-principals split.
  defp dispatcher_opts do
    [queue_depth_cap: queue_depth_cap()] ++ share_fraction_opt()
  end

  defp pool_opts, do: []

  defp queue_depth_cap do
    case trimmed_env("EMBERVM_QUEUE_DEPTH_CAP") do
      "" -> 10_000
      raw -> String.to_integer(raw)
    end
  end

  defp share_fraction_opt do
    case trimmed_env("EMBERVM_PRINCIPAL_SHARE_FRACTION") do
      "" ->
        []

      raw ->
        case Float.parse(raw) do
          {f, _} when f > 0 -> [share_fraction: f]
          _ -> []
        end
    end
  end

  # Per-principal daily vCPU-second budgets (Task 12), from the chart. Deliberately
  # OPT-IN and asymmetric to the auth allow-list: empty means quota OFF (a
  # principal with no configured budget is allowed), NOT deny-all. Fail-closed
  # applies only to a principal that HAS a budget when the quota cache is
  # unreadable. `EMBERVM_QUOTA_VCPU_SECONDS` is a comma list of `principal=budget`
  # pairs; `EMBERVM_QUOTA_DEFAULT_VCPU_SECONDS` is an optional blanket budget for
  # principals not named in the map. Budgets are vCPU-seconds (float).
  defp quota_config do
    %{budgets: quota_budgets(), default: quota_default()}
  end

  defp quota_budgets do
    case System.get_env("EMBERVM_QUOTA_VCPU_SECONDS") do
      nil ->
        %{}

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.reduce(%{}, fn pair, acc ->
          case String.split(pair, "=", parts: 2) do
            [k, v] ->
              case parse_budget(String.trim(v)) do
                nil -> acc
                budget -> Map.put(acc, String.trim(k), budget)
              end

            _ ->
              acc
          end
        end)
    end
  end

  defp quota_default do
    case trimmed_env("EMBERVM_QUOTA_DEFAULT_VCPU_SECONDS") do
      "" -> nil
      raw -> parse_budget(raw)
    end
  end

  # A budget of exactly 0 is VALID and means "deny this principal entirely": the
  # runtime gate (Embervm.Metering.within_quota?/4) compares `used < budget`, so a
  # 0 budget always denies. Accepting 0 here keeps the config surface consistent
  # with that runtime meaning (a 0 in values is a hard stop, not silently
  # unlimited). A negative or unparseable value is dropped (nil = no budget =
  # allowed, the opt-in default).
  defp parse_budget(raw) do
    case Float.parse(raw) do
      {f, _} when f >= 0 -> f
      _ -> nil
    end
  end

  # ServiceAccount usernames allowed to read other principals' usage at
  # GET /v1/usage (via ?principal=), from EMBERVM_USAGE_ADMINS (comma-separated).
  # Everyone else is self-scoped. Empty = nobody is an admin (self-scope only).
  defp usage_admins do
    case System.get_env("EMBERVM_USAGE_ADMINS") do
      nil ->
        []

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.map(&String.trim/1)
        |> Enum.reject(&(&1 == ""))
    end
  end

  # The TokenReview allow-list: ServiceAccount usernames permitted to submit
  # tasks, from EMBERVM_ALLOWED_SERVICE_ACCOUNTS (comma-separated), wired by the
  # chart from values.auth.allowedServiceAccounts. An empty list means deny-all
  # (fail-closed); Embervm.Auth logs a loud warning in that case.
  defp allowed_service_accounts do
    case System.get_env("EMBERVM_ALLOWED_SERVICE_ACCOUNTS") do
      nil ->
        []

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.map(&String.trim/1)
        |> Enum.reject(&(&1 == ""))
    end
  end
end
