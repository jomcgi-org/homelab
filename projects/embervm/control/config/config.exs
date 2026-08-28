import Config

# Force exqlite to compile its bundled sqlite3.c amalgamation from source via
# elixir_make instead of downloading a precompiled NIF from GitHub releases. The
# RBE build executor is offline, so the download path (cc_precompiler's default)
# would fail; from-source uses the executor's cc/make. The build host and every
# cluster node are amd64, so the resulting amd64 NIF is correct for deployment.
config :exqlite, force_build: true

# Structured JSON logs (Task 13): set the DEFAULT HANDLER's formatter to our
# custom Erlang :logger formatter, so every line is one JSON object a collector's
# pod-log pipeline ingests as structured fields. NOTE: this is :default_handler
# (which accepts `formatter: {module, config}`), NOT :default_formatter (which
# expects keyword options for Elixir's BUILT-IN formatter and crashes boot on a
# module tuple).
config :logger, :default_handler,
  formatter: {Embervm.LogFormatter, %{}}

# OpenTelemetry tracing (Task 13). Default OFF (traces_exporter: :none) so tests
# and any endpoint-less run boot cleanly and export nothing; config/runtime.exs
# turns on the OTLP/gRPC exporter when OTEL_EXPORTER_OTLP_ENDPOINT is set (the
# configured collector, wired by the chart). A batch processor buffers spans off the
# hot path.
config :opentelemetry,
  span_processor: :batch,
  traces_exporter: :none
