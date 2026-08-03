load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "metrics",
    srcs = ["metrics.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/metrics",
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
    deps = ["@com_github_prometheus_client_golang//prometheus"],
)
