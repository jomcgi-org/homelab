defmodule Embervm.MixProject do
  use Mix.Project

  # EmberVM control plane. R0 skeleton: a supervision tree with a dependency-free
  # health endpoint only. Deliberately NO hex deps yet, so the first Bazel/apko
  # build only has to solve the from-source OTP+Elixir toolchain, not hermetic
  # hex-dependency vendoring. gRPC (node.proto client), SQLite-WAL (op-log), and
  # the HTTP submit API are added once a dep-free OTP release + ExUnit are proven
  # green in CI.
  def project do
    [
      app: :embervm,
      version: "0.1.0",
      elixir: "~> 1.18",
      start_permanent: Mix.env() == :prod,
      deps: deps(),
      # OTP release with include_erts: false — the .beam bytecode is
      # architecture-independent, so ONE release artifact serves both arches, and
      # the apko runtime image supplies erlang-27 per-arch from Wolfi. This is
      # what turns "dual-arch OTP release" from a cross-compile into a package
      # dependency (see PR-A build-approach note).
      releases: [
        embervm: [
          include_executables_for: [:unix],
          include_erts: false
        ]
      ]
    ]
  end

  def application do
    [
      extra_applications: [:logger],
      mod: {Embervm.Application, []}
    ]
  end

  # No hex deps in the R0 skeleton (see the project/0 note above).
  defp deps do
    []
  end
end
