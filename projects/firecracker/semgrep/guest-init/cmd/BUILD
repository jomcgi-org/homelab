load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/semgrep/guest-init/internal/guestboot",
        "//projects/firecracker/semgrep/guest-init/internal/handler",
        "//projects/firecracker/semgrep/guest-init/internal/scandriver",
        "//projects/firecracker/semgrep/guest-init/internal/scanserver",
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/vsockproto",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
