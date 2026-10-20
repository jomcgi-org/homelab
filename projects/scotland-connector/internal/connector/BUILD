load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "connector",
    srcs = [
        "connector.go",
        "walks.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/connector",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/stars",
        "//projects/scotland-connector/internal/walks",
    ],
)

go_test(
    name = "connector_test",
    srcs = [
        "connector_test.go",
        "walks_test.go",
    ],
    deps = [
        ":connector",
        "//projects/scotland-connector/internal/fixture",
    ],
)
