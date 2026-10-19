load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "store",
    srcs = ["store.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/store",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/r2",
        "//projects/scotland-connector/internal/stars",
    ],
)

go_test(
    name = "store_test",
    srcs = ["store_test.go"],
    embed = [":store"],
    deps = ["//projects/scotland-connector/internal/r2"],
)
