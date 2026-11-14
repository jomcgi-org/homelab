load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "github",
    srcs = [
        "client.go",
        "tokens.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/github",
    visibility = ["//visibility:public"],
)

go_test(
    name = "github_test",
    srcs = ["github_test.go"],
    embed = [":github"],
)
