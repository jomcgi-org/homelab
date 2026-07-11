load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "fullscan",
    srcs = ["fullscan.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/fullscan",
    visibility = ["//projects/firecracker/semgrep/guest-init:__subpackages__"],
    deps = [
        "//projects/firecracker/semgrep/guest-init/internal/cliout",
        "//projects/firecracker/substrate/vsockproto",
    ],
)

go_test(
    name = "fullscan_test",
    srcs = ["fullscan_test.go"],
    embed = [":fullscan"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)
