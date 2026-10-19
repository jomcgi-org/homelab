load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "meter",
    srcs = ["meter.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/meter",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = ["@com_github_prometheus_client_golang//prometheus"],
)

go_test(
    name = "meter_test",
    srcs = ["meter_test.go"],
    embed = [":meter"],
    deps = [
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/testutil",
    ],
)
