load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = [
        "clock_linux.go",
        "clock_other.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "vsock_linux.go",
        "vsock_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/sandbox/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/sandbox/guest-init/internal/handler",
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/vsockproto",
    ] + select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
