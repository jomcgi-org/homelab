load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/agent_platform/fc-agentd/internal/config",
        "//projects/agent_platform/fc-agentd/internal/reconcile",
        "//projects/agent_platform/fc-agentd/internal/store",
        "//projects/agent_platform/fc-agentd/internal/telemetry",
        "//projects/firecracker/substrate/fcvm/driver",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
