load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "invoker",
    srcs = ["invoker.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/internal/invoker",
    visibility = ["//projects/firecracker/substrate/invoke:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/invoke/internal/config",
        "//projects/firecracker/substrate/substrate",
        "@org_golang_x_sync//semaphore",
    ],
)

go_test(
    name = "invoker_test",
    srcs = ["invoker_test.go"],
    embed = [":invoker"],
    deps = [
        "//projects/firecracker/substrate/invoke/internal/config",
        "//projects/firecracker/substrate/substrate",
    ],
)
