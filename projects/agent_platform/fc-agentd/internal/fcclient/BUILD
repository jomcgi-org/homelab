load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "fcclient",
    srcs = ["client.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/fcclient",
    visibility = ["//projects/agent_platform/fc-agentd:__subpackages__"],
)

go_test(
    name = "fcclient_test",
    srcs = ["client_test.go"],
    embed = [":fcclient"],
)
