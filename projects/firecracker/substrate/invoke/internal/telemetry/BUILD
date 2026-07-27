load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "telemetry",
    srcs = ["tracing.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/internal/telemetry",
    visibility = ["//projects/firecracker/substrate/invoke:__subpackages__"],
    deps = [
        "@io_opentelemetry_go_otel//:go_default_library",
        "@io_opentelemetry_go_otel//attribute:go_default_library",
        "@io_opentelemetry_go_otel//propagation:go_default_library",
        "@io_opentelemetry_go_otel_exporters_otlp_otlptrace_otlptracegrpc//:go_default_library",
        "@io_opentelemetry_go_otel_sdk//resource:go_default_library",
        "@io_opentelemetry_go_otel_sdk//trace:go_default_library",
    ],
)
