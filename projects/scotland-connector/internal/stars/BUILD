load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "stars",
    srcs = [
        "dataset.go",
        "geo.go",
        "windows.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/stars",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = ["//projects/scotland-connector/internal/astro"],
)

go_test(
    name = "stars_test",
    srcs = ["stars_test.go"],
    embed = [":stars"],
)
