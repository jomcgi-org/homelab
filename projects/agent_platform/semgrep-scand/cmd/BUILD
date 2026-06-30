load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/firecracker/substrate/fcvm/driver",
        "//projects/agent_platform/semgrep-scand/internal/config",
        "//projects/agent_platform/semgrep-scand/internal/scanner",
        "//projects/agent_platform/semgrep-scand/internal/server",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
