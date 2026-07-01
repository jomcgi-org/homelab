load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/substrate/fcvm/driver",
        "//projects/firecracker/substrate/invoke/internal/config",
        "//projects/firecracker/substrate/invoke/internal/invoker",
        "//projects/firecracker/substrate/invoke/internal/server",
        "//projects/firecracker/substrate/invoke/internal/vsockhttp",
    ],
)

go_binary(
    name = "invoke",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
