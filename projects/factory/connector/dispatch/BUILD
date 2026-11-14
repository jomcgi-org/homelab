load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "dispatch",
    srcs = [
        "classify.go",
        "delivery.go",
        "engine.go",
        "transcript.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/dispatch",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/github",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/kagent",
        "//projects/factory/connector/metrics",
        "//projects/factory/connector/store",
    ],
)

go_test(
    name = "dispatch_test",
    srcs = [
        "delivery_test.go",
        "engine_test.go",
        "fakes_test.go",
        "transcript_test.go",
    ],
    data = ["@postgres_test//:postgres"],
    deps = [
        ":dispatch",
        "//projects/factory/connector/config",
        "//projects/factory/connector/github",
        "//projects/factory/connector/internal/pgtest",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/kagent",
        "//projects/factory/connector/metrics",
        "//projects/factory/connector/store",
        "@com_github_prometheus_client_golang//prometheus/testutil",
    ],
)
