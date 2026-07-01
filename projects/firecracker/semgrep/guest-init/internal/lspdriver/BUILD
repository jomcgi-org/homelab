load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "lspdriver",
    srcs = ["lspdriver.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/lspdriver",
    visibility = ["//projects/firecracker/semgrep/guest-init:__subpackages__"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)

go_test(
    name = "lspdriver_test",
    srcs = ["lspdriver_test.go"],
    embed = [":lspdriver"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)
