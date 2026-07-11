load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "cliout",
    srcs = ["cliout.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/cliout",
    visibility = ["//projects/firecracker/semgrep/guest-init:__subpackages__"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)

go_test(
    name = "cliout_test",
    srcs = ["cliout_test.go"],
    embed = [":cliout"],
)
