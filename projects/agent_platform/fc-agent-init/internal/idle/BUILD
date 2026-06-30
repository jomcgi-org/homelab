load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "idle",
    srcs = ["idle.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/idle",
    visibility = ["//projects/agent_platform/fc-agent-init:__subpackages__"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)

go_test(
    name = "idle_test",
    srcs = ["idle_test.go"],
    embed = [":idle"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)
