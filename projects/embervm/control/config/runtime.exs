import Config

# Runtime OpenTelemetry wiring (Task 13), evaluated at release BOOT (and at test
# runtime). When OTEL_EXPORTER_OTLP_ENDPOINT is set (the chart points it at the
# collector over gRPC, enable the OTLP exporter; otherwise leave tracing
# OFF (the config.exs default), so a run with no collector boots cleanly and
# exports nothing. The endpoint-less case is exactly CI and any local mix run.
otlp_endpoint = System.get_env("OTEL_EXPORTER_OTLP_ENDPOINT", "")

if otlp_endpoint != "" do
  config :opentelemetry, traces_exporter: :otlp

  config :opentelemetry_exporter,
    otlp_protocol: :grpc,
    otlp_endpoint: otlp_endpoint
end
