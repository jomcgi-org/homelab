load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "connector_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/cmd/connector",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/connector/config",
        "//projects/factory/connector/dispatch",
        "//projects/factory/connector/github",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/kagent",
        "//projects/factory/connector/metrics",
        "//projects/factory/connector/migrations",
        "//projects/factory/connector/store",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/collectors",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
    ],
)

go_binary(
    name = "connector",
    embed = [":connector_lib"],
    visibility = ["//projects/factory/connector:__subpackages__"],
)
