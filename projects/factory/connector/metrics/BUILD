load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "metrics",
    srcs = [
        "metrics.go",
        "pooldemand.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/metrics",
    visibility = ["//visibility:public"],
    deps = ["@com_github_prometheus_client_golang//prometheus"],
)

go_test(
    name = "metrics_test",
    srcs = ["pooldemand_test.go"],
    embed = [":metrics"],
    deps = [
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/testutil",
    ],
)
