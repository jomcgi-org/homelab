defmodule Embervm.MixProject do
  use Mix.Project

  # EmberVM control plane. The health endpoint stays dependency-free; hex deps
  # enter here for the SQLite-WAL op-log (exqlite). They are vendored hermetically
  # by bazel/erlang: the hex tarball closure is fetched at repo-fetch time and
  # unpacked into deps/, then consumed below as `path:` deps so mix uses the Path
  # SCM and never contacts hex.pm on the offline RBE executor (no mix.lock, no
  # `mix deps.get`). gRPC (node.proto client) and the HTTP submit API follow.
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

  # Every dep is a `path:` dep pointing at deps/<name>/, which bazel/erlang unpacks
  # from the hex tarball closure before mix runs (see the project/0 note). exqlite
  # declares db_connection, elixir_make, and cc_precompiler as hex deps, and
  # db_connection declares telemetry; declaring each here as a path dep with
  # `override: true` forces the WHOLE graph through the Path SCM, so mix never
  # reaches hex.pm. elixir_make and cc_precompiler are build-time only
  # (`runtime: false`, kept out of the release). exqlite builds its bundled
  # sqlite3.c from source (config :exqlite, force_build: true in config/config.exs)
  # rather than downloading a precompiled NIF, which offline RBE cannot fetch.
  defp deps do
    [
      {:exqlite, path: "deps/exqlite"},
      {:db_connection, path: "deps/db_connection", override: true},
      {:telemetry, path: "deps/telemetry", override: true},
      {:elixir_make, path: "deps/elixir_make", runtime: false, override: true},
      {:cc_precompiler, path: "deps/cc_precompiler", runtime: false, override: true}
    ]
  end
end
