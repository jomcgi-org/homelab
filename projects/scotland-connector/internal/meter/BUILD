load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "meter",
    srcs = [
        "export.go",
        "meter.go",
        "policy.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/meter",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/analytics",
        "@com_github_google_uuid//:uuid",
        "@com_github_prometheus_client_golang//prometheus",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel_exporters_otlp_otlpmetric_otlpmetrichttp//:otlpmetrichttp",
        "@io_opentelemetry_go_otel_metric//:metric",
        "@io_opentelemetry_go_otel_sdk//resource",
        "@io_opentelemetry_go_otel_sdk_metric//:metric",
        "@io_opentelemetry_go_otel_sdk_metric//metricdata",
    ],
)

go_test(
    name = "meter_test",
    srcs = [
        "export_test.go",
        "meter_test.go",
        "policy_test.go",
    ],
    embed = [":meter"],
    deps = [
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/testutil",
        "@io_opentelemetry_go_proto_otlp//collector/metrics/v1:metrics",
        "@org_golang_google_protobuf//proto",
    ],
)

# Read by the chart's rate-limit acceptance test, which keeps the Envoy
# assistant-bucket pattern equal to the ranges here.
filegroup(
    name = "meter_source",
    srcs = ["meter.go"],
    visibility = ["//projects/scotland-connector/chart:__pkg__"],
)
