load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "reconnect",
    srcs = ["reconnect.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/reconnect",
    visibility = ["//projects/agent_platform/fc-agent-init:__subpackages__"],
)

go_test(
    name = "reconnect_test",
    srcs = ["reconnect_test.go"],
    embed = [":reconnect"],
)
