load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "broker",
    srcs = ["broker.go"],
    importpath = "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/broker",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/shared/go/tokenbroker/metrics",
        "//projects/shared/go/tokenbroker/provider",
        "//projects/shared/go/tokenbroker/store",
    ],
)

go_test(
    name = "broker_test",
    srcs = ["broker_test.go"],
    embed = [":broker"],
    deps = [
        "//projects/shared/go/tokenbroker/provider",
        "//projects/shared/go/tokenbroker/store",
    ],
)
