load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "ingress",
    srcs = ["server.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/ingress",
    visibility = ["//projects/firecracker/substrate:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/substrate",
        "@io_opentelemetry_go_otel//:otel",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel//codes",
        "@io_opentelemetry_go_otel//propagation",
        "@io_opentelemetry_go_otel_trace//:trace",
    ],
)

go_test(
    name = "ingress_test",
    srcs = ["server_test.go"],
    embed = [":ingress"],
    deps = ["//projects/firecracker/substrate/substrate"],
)
