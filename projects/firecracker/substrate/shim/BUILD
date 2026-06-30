load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "shim",
    srcs = [
        "hooks.go",
        "server.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/shim",
    visibility = ["//visibility:public"],
)

go_test(
    name = "shim_test",
    srcs = [
        "hooks_test.go",
        "server_test.go",
    ],
    embed = [":shim"],
)
