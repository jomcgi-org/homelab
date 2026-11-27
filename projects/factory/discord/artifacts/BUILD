load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "artifacts",
    srcs = [
        "artifacts.go",
        "mcp.go",
        "s3.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/artifacts",
    visibility = ["//visibility:public"],
    deps = ["//projects/factory/broker/actorjwt"],
)

go_test(
    name = "artifacts_test",
    srcs = [
        "artifacts_test.go",
        "sigv4_test.go",
    ],
    embed = [":artifacts"],
    deps = ["//projects/factory/broker/actorjwt"],
)
