load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "fixture",
    testonly = True,
    srcs = [
        "aurora.go",
        "fixture.go",
        "roadworks.go",
    ],
    embedsrcs = ["testdata/roadworks.json"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = [
        "//projects/scotland-connector/internal/aurora",
        "//projects/scotland-connector/internal/connector",
        "//projects/scotland-connector/internal/roadworks",
        "//projects/scotland-connector/internal/stars",
        "//projects/scotland-connector/internal/walks",
    ],
)

filegroup(
    name = "roadworks_contract",
    srcs = ["testdata/roadworks.json"],
    visibility = ["//projects/monolith:__pkg__"],
)
