load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "walks",
    srcs = [
        "verdict.go",
        "walks.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/walks",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = ["//projects/scotland-connector/internal/verdict"],
)

go_test(
    name = "walks_test",
    srcs = [
        "verdict_test.go",
        "walks_test.go",
    ],
    embed = [":walks"],
    deps = ["//projects/scotland-connector/internal/verdict"],
)
