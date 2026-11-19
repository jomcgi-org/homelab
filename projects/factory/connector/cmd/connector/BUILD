load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "connector_lib",
    srcs = [
        "main.go",
        "reaper.go",
        "recovery.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/cmd/connector",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/connector/buildbuddy",
        "//projects/factory/connector/config",
        "//projects/factory/connector/dispatch",
        "//projects/factory/connector/github",
        "//projects/factory/connector/internal/reaper",
        "//projects/factory/connector/itsaplan",
        "//projects/factory/connector/kagent",
        "//projects/factory/connector/metrics",
        "//projects/factory/connector/migrations",
        "//projects/factory/connector/notify",
        "//projects/factory/connector/status",
        "//projects/factory/connector/store",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/collectors",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@io_k8s_client_go//kubernetes/typed/core/v1:core",
        "@io_k8s_client_go//rest",
    ],
)

go_binary(
    name = "connector",
    embed = [":connector_lib"],
    visibility = ["//projects/factory/connector:__subpackages__"],
)

go_test(
    name = "connector_test",
    srcs = ["recovery_test.go"],
    embed = [":connector_lib"],
    deps = ["//projects/factory/connector/store"],
)
