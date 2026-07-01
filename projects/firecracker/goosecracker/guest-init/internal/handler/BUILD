load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "handler",
    srcs = ["handler.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/goosecracker/guest-init/internal/handler",
    visibility = ["//projects/firecracker/goosecracker/guest-init:__subpackages__"],
    deps = [
        "//projects/firecracker/goosecracker/guest-init/internal/harness",
        "//projects/firecracker/substrate/shim",
    ],
)

go_test(
    name = "handler_test",
    srcs = ["handler_test.go"],
    embed = [":handler"],
    deps = [
        "//projects/firecracker/substrate/shim",
    ],
)
