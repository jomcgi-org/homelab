load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "codexchatgpt",
    srcs = [
        "adapter.go",
        "usage.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/shared/go/tokenbroker/provider",
        "//projects/shared/go/tokenbroker/quota",
    ],
)

go_test(
    name = "codexchatgpt_test",
    srcs = ["adapter_test.go"],
    embed = [":codexchatgpt"],
    deps = ["//projects/shared/go/tokenbroker/provider"],
)
