load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "lspdriver",
    srcs = ["lspdriver.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/semgrep-guest-init/internal/lspdriver",
    visibility = ["//projects/agent_platform/semgrep-guest-init:__subpackages__"],
    deps = ["//projects/agent_platform/vsockproto"],
)

go_test(
    name = "lspdriver_test",
    srcs = ["lspdriver_test.go"],
    embed = [":lspdriver"],
    deps = ["//projects/agent_platform/vsockproto"],
)
