load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "review",
    srcs = [
        "engine.go",
        "gate.go",
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
        "engine_test.go",
        "fakes_test.go",
        "report_test.go",
        "rules_test.go",
    ],
    embed = [":review"],
    deps = [
        "//projects/factory/connector/github",
        "//projects/factory/connector/kagent",
        "//projects/factory/reviewer/metrics",
        "@com_github_prometheus_client_golang//prometheus/testutil",
    ],
)
