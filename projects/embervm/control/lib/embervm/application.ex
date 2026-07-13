defmodule Embervm.Application do
  @moduledoc """
  Root OTP application for the EmberVM control plane.

  R0 skeleton: the supervision tree holds the op-log (durable task-state
  seam), the ETS hot-set task store built on top of it, and the health
  endpoint. Later tasks add children under this same tree (the
  WorkloadWatcher, the NodeRegistry, the Dispatcher, the submit-API
  listener), which is exactly why the control plane is on the BEAM: each of
  those is a supervised process with its own failure domain, restarted in
  isolation.

  Strategy is `:rest_for_one`, not `:one_for_one`: `Embervm.TaskStore` reads
  the op-log once on init to rebuild its ETS tables and otherwise depends on
  it being alive for every write. If `Embervm.OpLog.SQLite` crashes and
  restarts, `:rest_for_one` also restarts every child listed after it (today
  just `TaskStore`), so the rebuild-on-boot path runs again against the
  op-log's post-restart state instead of leaving ETS stale against an op-log
  that came back with (in the worst case) a different in-memory connection.
  `Health` has no dependency on either, so ordering it last costs nothing.
  """
  use Application

  @impl true
  def start(_type, _args) do
    port = http_port()

    children = [
      {Embervm.OpLog.SQLite, path: oplog_path()},
      {Embervm.TaskStore, []},
      {Embervm.Health, port: port}
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
end
