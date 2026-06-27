load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "harness",
    srcs = ["harness.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/harness",
    visibility = ["//projects/agent_platform/fc-agent-init:__subpackages__"],
)

go_test(
    name = "harness_test",
    srcs = ["harness_test.go"],
    embed = [":harness"],
)
