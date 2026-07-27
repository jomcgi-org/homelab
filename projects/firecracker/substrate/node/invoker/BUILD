load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "invoker",
    srcs = ["invoker.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/node/invoker",
    visibility = ["//projects/firecracker/substrate:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/node/egress",
        "//projects/firecracker/substrate/substrate",
        "@io_opentelemetry_go_otel//:otel",
        "@io_opentelemetry_go_otel//attribute",
        "@io_opentelemetry_go_otel//codes",
        "@io_opentelemetry_go_otel_trace//:trace",
        "@org_golang_x_sync//semaphore",
    ],
)

go_test(
    name = "invoker_test",
    srcs = ["invoker_test.go"],
    embed = [":invoker"],
    deps = ["//projects/firecracker/substrate/substrate"],
)
