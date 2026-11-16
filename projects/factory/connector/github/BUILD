load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "github",
    srcs = [
        "app_comments.go",
        "broker_comments.go",
        "broker_quota.go",
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
        "app_comments_test.go",
        "broker_comments_test.go",
        "broker_quota_test.go",
        "github_test.go",
        "recovery_test.go",
        "status_test.go",
    ],
    embed = [":github"],
)
