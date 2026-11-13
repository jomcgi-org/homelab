load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "telemetry",
    srcs = ["tracing.go"],
    importpath = "github.com/jomcgi/homelab/projects/shared/telemetry",
    visibility = [
        "//projects/embervm/noded/telemetry:__pkg__",
        "//projects/operators/oci-model-cache/internal/telemetry:__pkg__",
    ],
    deps = [
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel_sdk//resource",
        "@io_opentelemetry_go_otel_sdk//trace",
        "@io_opentelemetry_go_otel_trace//:trace",
    ],
)

go_test(
    name = "telemetry_test",
    srcs = ["tracing_test.go"],
    embed = [":telemetry"],
    deps = [
        "@io_opentelemetry_go_otel//:otel",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel_sdk//resource",
        "@io_opentelemetry_go_otel_sdk//trace",
        "@io_opentelemetry_go_otel_sdk//trace/tracetest",
        "@io_opentelemetry_go_otel_trace//:trace",
    ],
)
