import Config
require Logger

desired_capacity =
  case Embervm.CapacityReport.parse_desired_capacity(System.get_env("EMBERVM_DESIRED_CAPACITY")) do
    {:ok, value} -> value
    {:error, :invalid_desired_capacity} -> raise ArgumentError, "EMBERVM_DESIRED_CAPACITY must be a non-negative integer"
  end

capacity_horizon_seconds =
  case Embervm.CapacityReport.parse_horizon_seconds(System.get_env("EMBERVM_CAPACITY_HORIZON_SECONDS")) do
    {:ok, value} -> value
    {:error, :invalid_capacity_horizon} -> raise ArgumentError, "EMBERVM_CAPACITY_HORIZON_SECONDS must be between 1 and 86400"
  end

# Future-facing configuration seam only. Nothing in BrickController, placement,
# or pool management reads desired_capacity. Logging the parsed typed value makes
# configuration visible without introducing a scaling target or actuation path.
config :embervm,
  desired_capacity: desired_capacity,
  capacity_horizon_seconds: capacity_horizon_seconds

Logger.info("embervm desired capacity parsed", desired_capacity: desired_capacity || "unset")

# Runtime OpenTelemetry wiring, evaluated at release BOOT (and at test
# runtime). When OTEL_EXPORTER_OTLP_ENDPOINT is set (the chart points it at the
# collector over gRPC), enable the trace and metrics OTLP exporters. Otherwise
# leave both signals without exporters, so a run with no collector boots cleanly and
# exports nothing. The endpoint-less case is exactly CI and any local mix run.
otlp_endpoint = System.get_env("OTEL_EXPORTER_OTLP_ENDPOINT", "")

if otlp_endpoint != "" do
  config :opentelemetry, traces_exporter: :otlp

  config :opentelemetry_exporter,
    otlp_protocol: :grpc,
    otlp_endpoint: otlp_endpoint

  config :opentelemetry_experimental,
    readers: [
      %{
        module: :otel_metric_reader,
        config: %{
          export_interval_ms: 60_000,
          exporter:
            {:opentelemetry_exporter,
             %{protocol: :grpc, endpoints: [otlp_endpoint]}}
        }
      }
    ]
end
