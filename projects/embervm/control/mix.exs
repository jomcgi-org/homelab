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
  # from the hex tarball closure before mix runs (see the project/0 note). Each
  # dep's own mix.exs declares ITS deps as hex deps (e.g. bandit declares
  # `{:plug, "~> 1.18"}`); declaring every node of the closure here as a path dep
  # with `override: true` forces the WHOLE graph through the Path SCM, so mix
  # never reaches hex.pm (no mix.lock, no `mix deps.get`).
  #
  # Two groups (see bazel/erlang/repositories.bzl for the resolved closure):
  #   - op-log: exqlite + db_connection -> telemetry, plus build-time elixir_make
  #     and cc_precompiler (`runtime: false`, kept out of the release). exqlite
  #     builds its bundled sqlite3.c from source (force_build in config.exs)
  #     rather than downloading a precompiled NIF, which offline RBE cannot fetch.
  #   - HTTP + K8s client: bandit + plug (submit-API router, replaces the raw
  #     :gen_tcp health endpoint) and finch + mint (K8s TokenReview + CRD watch),
  #     with their transitive closure (hpax, mime, plug_crypto, thousand_island,
  #     websock, nimble_options, nimble_pool). All pure Elixir/Erlang, so no NIF
  #     build step. mime and telemetry are shared across both groups; hpax is
  #     shared by bandit and mint.
  defp deps do
    [
      {:exqlite, path: "deps/exqlite"},
      {:db_connection, path: "deps/db_connection", override: true},
      {:telemetry, path: "deps/telemetry", override: true},
      {:elixir_make, path: "deps/elixir_make", runtime: false, override: true},
      {:cc_precompiler, path: "deps/cc_precompiler", runtime: false, override: true},
      {:bandit, path: "deps/bandit", override: true},
      {:plug, path: "deps/plug", override: true},
      {:plug_crypto, path: "deps/plug_crypto", override: true},
      {:mime, path: "deps/mime", override: true},
      {:thousand_island, path: "deps/thousand_island", override: true},
      {:websock, path: "deps/websock", override: true},
      {:hpax, path: "deps/hpax", override: true},
      {:finch, path: "deps/finch", override: true},
      {:mint, path: "deps/mint", override: true},
      {:nimble_options, path: "deps/nimble_options", override: true},
      {:nimble_pool, path: "deps/nimble_pool", override: true}
    ]
  end
end
