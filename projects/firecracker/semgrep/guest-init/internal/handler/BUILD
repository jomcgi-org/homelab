load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "handler",
    srcs = ["handler.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/handler",
    visibility = ["//projects/firecracker/semgrep/guest-init:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/vsockproto",
    ],
)

go_test(
    name = "handler_test",
    srcs = ["handler_test.go"],
    embed = [":handler"],
    deps = [
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/vsockproto",
    ],
)
