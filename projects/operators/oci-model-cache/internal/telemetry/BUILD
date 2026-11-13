load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "telemetry",
    srcs = ["tracing.go"],
    importpath = "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/telemetry",
    visibility = ["//projects/operators/oci-model-cache:__subpackages__"],
    deps = [
        "//projects/shared/telemetry",
        "@io_opentelemetry_go_otel//:otel",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel_exporters_otlp_otlptrace_otlptracegrpc//:otlptracegrpc",
        "@io_opentelemetry_go_otel_sdk//trace",
        "@io_opentelemetry_go_otel_trace//:trace",
    ],
)

go_test(
    name = "telemetry_test",
    srcs = [
        "adapter_test.go",
        "tracing_test.go",
    ],
    embed = [":telemetry"],
    deps = [
        "//projects/shared/telemetry/testutil",
        "@com_github_stretchr_testify//assert",
        "@com_github_stretchr_testify//require",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel_sdk//trace",
        "@io_opentelemetry_go_otel_sdk//trace/tracetest",
        "@io_opentelemetry_go_otel_trace//:trace",
    ],
)
