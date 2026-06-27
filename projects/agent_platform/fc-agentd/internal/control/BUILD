load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "control",
    srcs = ["server.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/control",
    visibility = ["//projects/agent_platform/fc-agentd:__subpackages__"],
    deps = ["//projects/agent_platform/vsockproto"],
)
