load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "metrics",
    srcs = ["metrics.go"],
    importpath = "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/metrics",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/shared/go/tokenbroker/quota",
        "@com_github_prometheus_client_golang//prometheus",
    ],
)
