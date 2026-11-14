load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "metrics",
    srcs = ["metrics.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/metrics",
    visibility = ["//visibility:public"],
    deps = ["@com_github_prometheus_client_golang//prometheus"],
)
