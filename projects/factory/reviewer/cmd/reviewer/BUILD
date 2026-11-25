load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "reviewer_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/reviewer/cmd/reviewer",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/connector/github",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/kagent",
        "//projects/factory/reviewer/config",
        "//projects/factory/reviewer/metrics",
        "//projects/factory/reviewer/review",
        "//projects/factory/taskpin",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/collectors",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@io_opentelemetry_go_otel//:otel",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel_exporters_otlp_otlptrace_otlptracegrpc//:otlptracegrpc",
        "@io_opentelemetry_go_otel_sdk//resource",
        "@io_opentelemetry_go_otel_sdk//trace",
    ],
)

go_binary(
    name = "reviewer",
    embed = [":reviewer_lib"],
    visibility = ["//projects/factory/reviewer:__subpackages__"],
)
