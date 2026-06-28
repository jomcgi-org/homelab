load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/agent_platform/fc-agent-init/internal/harness",
        "//projects/agent_platform/fc-agent-init/internal/idle",
        "//projects/agent_platform/fc-agent-init/internal/reconnect",
        "//projects/agent_platform/fc-agent-init/internal/vsockdial",
        "//projects/agent_platform/vsockproto",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = ["main_test.go"],
    embed = [":cmd_lib"],
)
