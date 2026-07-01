load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = [
        "loopback_linux.go",
        "loopback_other.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "primers.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/semgrep/guest-init/internal/handler",
        "//projects/firecracker/semgrep/guest-init/internal/lspdriver",
        "//projects/firecracker/semgrep/guest-init/internal/scanserver",
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
