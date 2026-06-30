load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "watchdog",
    srcs = ["watchdog.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/watchdog",
    visibility = ["//projects/agent_platform/fc-agent-init:__subpackages__"],
)

go_test(
    name = "watchdog_test",
    srcs = ["watchdog_test.go"],
    embed = [":watchdog"],
)
