load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "review",
    srcs = [
        "cited.go",
        "comment.go",
        "engine.go",
        "gate.go",
        "persistence.go",
        "prompt.go",
        "report.go",
        "rules.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/reviewer/review",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/github",
        "//projects/factory/connector/kagent",
        "//projects/factory/reviewer/metrics",
    ],
)

go_test(
    name = "review_test",
    srcs = [
        "cited_test.go",
        "comment_test.go",
        "engine_test.go",
        "fakes_test.go",
        "persistence_test.go",
        "report_test.go",
        "rules_test.go",
    ],
    data = ["@postgres_test//:postgres"],
    embed = [":review"],
    deps = [
        "//projects/factory/connector/github",
        "//projects/factory/connector/kagent",
        "//projects/factory/internal/pgtest",
        "//projects/factory/reviewer/metrics",
        "//projects/factory/reviewer/migrations",
        "//projects/factory/reviewer/store",
        "@com_github_prometheus_client_golang//prometheus/testutil",
    ],
)
