load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "ingress",
    srcs = ["server.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/ingress",
    visibility = ["//projects/firecracker/substrate:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/substrate",
        "@io_opentelemetry_go_otel//:go_default_library",
        "@io_opentelemetry_go_otel//attribute:go_default_library",
        "@io_opentelemetry_go_otel//codes:go_default_library",
        "@io_opentelemetry_go_otel//propagation:go_default_library",
        "@io_opentelemetry_go_otel_trace//:go_default_library",
    ],
)

go_test(
    name = "ingress_test",
    srcs = ["server_test.go"],
    embed = [":ingress"],
    deps = ["//projects/firecracker/substrate/substrate"],
)
