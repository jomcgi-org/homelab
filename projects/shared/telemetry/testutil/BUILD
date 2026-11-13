load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "testutil",
    testonly = True,
    srcs = ["exporter.go"],
    importpath = "github.com/jomcgi/homelab/projects/shared/telemetry/testutil",
    visibility = [
        "//projects/embervm/noded/telemetry:__pkg__",
        "//projects/operators/oci-model-cache/internal/telemetry:__pkg__",
    ],
    deps = [
        "@io_opentelemetry_go_otel_exporters_otlp_otlptrace_otlptracegrpc//:otlptracegrpc",
        "@io_opentelemetry_go_otel_sdk//trace",
        "@io_opentelemetry_go_otel_sdk//trace/tracetest",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//resolver",
        "@org_golang_google_grpc//resolver/manual",
    ],
)
