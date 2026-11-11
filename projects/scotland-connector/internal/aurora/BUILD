load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "aurora",
    srcs = [
        "aurora.go",
        "producer.go",
        "terrain.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/astro",
        "//projects/scotland-connector/internal/stars",
    ],
)

go_test(
    name = "aurora_test",
    srcs = [
        "aurora_test.go",
        "integration_test.go",
        "terrain_test.go",
    ],
    deps = [
        ":aurora",
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/fixture",
        "//projects/scotland-connector/internal/httpapi",
        "//projects/scotland-connector/internal/mcp",
        "//projects/scotland-connector/internal/meter",
        "//projects/scotland-connector/internal/r2",
        "//projects/scotland-connector/internal/store",
        "@com_github_prometheus_client_golang//prometheus",
    ],
)
