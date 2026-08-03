load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "codexchatgpt",
    srcs = ["adapter.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider/codexchatgpt",
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
    deps = ["//projects/embervm/tokenbroker/internal/provider:provider"],
)

go_test(
    name = "codexchatgpt_test",
    srcs = ["adapter_test.go"],
    embed = [":codexchatgpt"],
)
