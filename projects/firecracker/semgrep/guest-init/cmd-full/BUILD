load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd-full_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/cmd-full",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/semgrep/guest-init/internal/fullscan",
        "//projects/firecracker/semgrep/guest-init/internal/guestboot",
        "//projects/firecracker/semgrep/guest-init/internal/handler",
        "//projects/firecracker/semgrep/guest-init/internal/scanserver",
        "//projects/firecracker/substrate/shim",
        "//projects/firecracker/substrate/vsockproto",
    ],
)

go_binary(
    name = "cmd-full",
    embed = [":cmd-full_lib"],
    visibility = ["//visibility:public"],
)
