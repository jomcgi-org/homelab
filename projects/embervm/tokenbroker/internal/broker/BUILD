load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "broker",
    srcs = ["broker.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/broker",
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
    deps = [
        "//projects/embervm/tokenbroker/internal/metrics",
        "//projects/embervm/tokenbroker/internal/provider",
        "//projects/embervm/tokenbroker/internal/store",
    ],
)

go_test(
    name = "broker_test",
    srcs = ["broker_test.go"],
    embed = [":broker"],
    deps = [
        "//projects/embervm/tokenbroker/internal/provider",
        "//projects/embervm/tokenbroker/internal/store",
    ],
)
