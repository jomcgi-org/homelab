load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "classify.go",
        "main.go",
        "quota.go",
        "swap.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/egress-proxy/cmd",
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
        "classify_test.go",
        "main_test.go",
        "quota_test.go",
        "swap_test.go",
    ],
    embed = [":cmd_lib"],
)
