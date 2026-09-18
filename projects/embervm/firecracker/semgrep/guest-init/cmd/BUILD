load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/firecracker/semgrep/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/firecracker/semgrep/guest-init/internal/guestboot",
        "//projects/embervm/firecracker/semgrep/guest-init/internal/handler",
        "//projects/embervm/firecracker/semgrep/guest-init/internal/scandriver",
        "//projects/embervm/firecracker/semgrep/guest-init/internal/scanserver",
        "//projects/embervm/firecracker/substrate/shim",
        "//projects/embervm/firecracker/substrate/vsockproto",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
