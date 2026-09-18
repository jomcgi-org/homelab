load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "fullscan",
    srcs = ["fullscan.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/firecracker/semgrep/guest-init/internal/fullscan",
    visibility = ["//projects/embervm/firecracker/semgrep/guest-init:__subpackages__"],
    deps = [
        "//projects/embervm/firecracker/semgrep/guest-init/internal/cliout",
        "//projects/embervm/firecracker/substrate/vsockproto",
    ],
)

go_test(
    name = "fullscan_test",
    srcs = ["fullscan_test.go"],
    embed = [":fullscan"],
    deps = ["//projects/embervm/firecracker/substrate/vsockproto"],
)
