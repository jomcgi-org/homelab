load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "loopback_linux.go",
        "loopback_other.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/goosecracker/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/goosecracker/guest-init/internal/handler",
        "//projects/firecracker/goosecracker/guest-init/internal/vsockdial",
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/shim/capabilities",
        "//projects/firecracker/substrate/vsockproto",
    ] + select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix:go_default_library",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix:go_default_library",
        ],
        "//conditions:default": [],
    }),
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = ["main_test.go"],
    embed = [":cmd_lib"],
)
