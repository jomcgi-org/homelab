load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "driver",
    srcs = [
        "driver.go",
        "launcher.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/driver",
    visibility = ["//projects/agent_platform/fc-agentd:__subpackages__"],
    deps = [
        "//projects/agent_platform/fc-agentd/internal/fcclient",
        "//projects/agent_platform/substrate",
    ],
)

go_test(
    name = "driver_test",
    srcs = ["driver_test.go"],
    embed = [":driver"],
    deps = ["//projects/agent_platform/substrate"],
)
