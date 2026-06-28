load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "main.go",
        "swap.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/egress-proxy/cmd",
    visibility = ["//visibility:private"],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = [
        "main_test.go",
        "swap_test.go",
    ],
    embed = [":cmd_lib"],
)
