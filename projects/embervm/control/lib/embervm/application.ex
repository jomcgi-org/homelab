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

    children = [
      # The sync-wait waiter registry + park-count ETS owner come FIRST: every
      # terminal task-state write in TaskStore calls Embervm.SyncWait.notify,
      # which dispatches through this registry, so it must already exist. Both
      # are dependency-free and effectively never crash, so leading the
      # rest_for_one chain costs nothing.
      {Registry, keys: :duplicate, name: Embervm.TaskWaiters},
      Embervm.SyncWait,
      {Embervm.OpLog.SQLite, path: oplog_path()},
      {Embervm.TaskStore, []},
      # Finch (the shared HTTP pool, TLS-pinned to the K8s CA in-cluster) before
      # Embervm.Auth, whose TokenReview reviewer dials the API server over it.
      Embervm.K8s.finch_child_spec(),
      {Embervm.Auth, allowed: allowed_service_accounts()},
      # The Workload informer (Task 5): LISTs then WATCHes Workload CRs over the
      # Finch pool above and writes Embervm.WorkloadCatalog, which
      # TaskStore.cfg_for/1 reads. Placed after Finch (its watch streams over
      # that pool) and after TaskStore in the supervision list; TaskStore does
      # not depend on the watcher being up (WorkloadCatalog.retry_config/1
      # tolerates the catalog table not existing yet), so their relative order
      # here is not load-bearing.
      Embervm.WorkloadWatcher,
      # Bandit + the router last: its handlers call Auth, TaskStore, and
      # SyncWait, so the HTTP surface must not accept requests until all are up.
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
