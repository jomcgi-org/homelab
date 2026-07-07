load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/substrate/cluster/auth",
        "//projects/firecracker/substrate/cluster/catalog",
        "//projects/firecracker/substrate/cluster/ingress",
        "//projects/firecracker/substrate/invoke/internal/telemetry",
        "//projects/firecracker/substrate/node/fcvm/driver",
        "//projects/firecracker/substrate/node/invoker",
        "//projects/firecracker/substrate/node/vsockhttp",
        "//projects/firecracker/substrate/substrate",
    ],
)

go_binary(
    name = "invoke",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
