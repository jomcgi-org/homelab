load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "config",
    srcs = ["config.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/config",
    visibility = ["//projects/agent_platform/fc-agentd:__subpackages__"],
)

go_test(
    name = "config_test",
    srcs = ["config_test.go"],
    embed = [":config"],
)
