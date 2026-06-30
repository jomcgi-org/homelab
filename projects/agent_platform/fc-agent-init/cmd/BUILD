load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "loopback_linux.go",
        "loopback_other.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/agent_platform/fc-agent-init/internal/harness",
        "//projects/agent_platform/fc-agent-init/internal/idle",
        "//projects/agent_platform/fc-agent-init/internal/reconnect",
        "//projects/agent_platform/fc-agent-init/internal/vsockdial",
        "//projects/agent_platform/fc-agent-init/internal/watchdog",
        "//projects/firecracker/substrate/vsockproto",
    ] + select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
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
