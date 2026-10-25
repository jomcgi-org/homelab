load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "analytics",
    srcs = ["analytics.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/analytics",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = ["@com_github_prometheus_client_golang//prometheus"],
)

go_test(
    name = "analytics_test",
    srcs = ["analytics_test.go"],
    embed = [":analytics"],
    deps = ["@com_github_prometheus_client_golang//prometheus"],
)
