load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "connector_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/cmd/connector",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/httpapi",
        "//projects/scotland-connector/internal/mcp",
        "//projects/scotland-connector/internal/meter",
        "//projects/scotland-connector/internal/r2",
        "//projects/scotland-connector/internal/store",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/collectors",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
    ],
)

go_binary(
    name = "connector",
    embed = [":connector_lib"],
    visibility = ["//projects/scotland-connector:__subpackages__"],
)

go_test(
    name = "connector_test",
    srcs = ["main_test.go"],
    embed = [":connector_lib"],
    deps = [
        "//projects/scotland-connector/internal/fixture",
        "//projects/scotland-connector/internal/mcp",
    ],
)
