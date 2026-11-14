load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "github",
    srcs = [
        "broker_comments.go",
        "checks.go",
        "client.go",
        "etag.go",
        "pulls.go",
        "recovery.go",
        "status.go",
        "tokens.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/github",
    visibility = ["//visibility:public"],
)

go_test(
    name = "github_test",
    srcs = [
        "broker_comments_test.go",
        "github_test.go",
        "recovery_test.go",
        "status_test.go",
    ],
    embed = [":github"],
)
