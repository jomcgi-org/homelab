load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "handler",
    srcs = ["handler.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/firecracker/semgrep/guest-init/internal/handler",
    visibility = ["//projects/embervm/firecracker/semgrep/guest-init:__subpackages__"],
    deps = [
        "//projects/embervm/firecracker/substrate/shim",
        "//projects/embervm/firecracker/substrate/vsockproto",
    ],
)

go_test(
    name = "handler_test",
    srcs = ["handler_test.go"],
    embed = [":handler"],
    deps = [
        "//projects/embervm/firecracker/substrate/shim",
        "//projects/embervm/firecracker/substrate/vsockproto",
    ],
)
