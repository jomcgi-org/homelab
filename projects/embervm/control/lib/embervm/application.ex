defmodule Embervm.Application do
  @moduledoc """
  Root OTP application for the EmberVM control plane.

  R0 skeleton: the supervision tree holds only the health endpoint. Later tasks
  add children under this same tree (the op-log GenServer, the WorkloadWatcher,
  the NodeRegistry, the Dispatcher, the submit-API listener), which is exactly
  why the control plane is on the BEAM: each of those is a supervised process
  with its own failure domain, restarted in isolation.
  """
  use Application

  @impl true
  def start(_type, _args) do
    port = http_port()

    children = [
      {Embervm.Health, port: port}
    ]

    opts = [strategy: :one_for_one, name: Embervm.Supervisor]
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
end
