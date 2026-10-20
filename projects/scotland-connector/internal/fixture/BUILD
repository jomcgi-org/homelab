load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "fixture",
    testonly = True,
    srcs = ["fixture.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/stars",
        "//projects/scotland-connector/internal/walks",
    ],
)
