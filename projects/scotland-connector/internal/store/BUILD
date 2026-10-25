load("@rules_go//go:def.bzl", "go_library", "go_test")

# Shared producer-byte contract. The Python test regenerates these bytes.
filegroup(
    name = "snapshot_fixture",
    srcs = ["testdata/connector-sites.json"],
    visibility = ["//projects/monolith:__pkg__"],
)

go_library(
    name = "store",
    srcs = [
        "aurora.go",
        "store.go",
        "walks.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/store",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/aurora",
        "//projects/scotland-connector/internal/r2",
        "//projects/scotland-connector/internal/stars",
        "//projects/scotland-connector/internal/walks",
    ],
)

go_test(
    name = "store_test",
    srcs = [
        "store_test.go",
        "walks_test.go",
    ],
    embedsrcs = ["testdata/connector-sites.json"],
    embed = [":store"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/r2",
        "//projects/scotland-connector/internal/verdict",
    ],
)
