load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "telemetry",
    srcs = ["tracing.go"],
    importpath = "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/telemetry",
    visibility = ["//projects/operators/oci-model-cache:__subpackages__"],
    deps = [
        "@io_opentelemetry_go_otel//:go_default_library",
        "@io_opentelemetry_go_otel//attribute:go_default_library",
        "@io_opentelemetry_go_otel_exporters_otlp_otlptrace_otlptracegrpc//:go_default_library",
        "@io_opentelemetry_go_otel_sdk//resource:go_default_library",
        "@io_opentelemetry_go_otel_sdk//trace:go_default_library",
        "@io_opentelemetry_go_otel_trace//:go_default_library",
    ],
)

go_test(
    name = "telemetry_test",
    srcs = ["tracing_test.go"],
    embed = [":telemetry"],
    deps = [
        "@com_github_stretchr_testify//assert:go_default_library",
        "@com_github_stretchr_testify//require:go_default_library",
        "@io_opentelemetry_go_otel//:go_default_library",
    ],
)
