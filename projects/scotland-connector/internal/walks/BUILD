load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "walks",
    srcs = ["walks.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/walks",
    visibility = ["//projects/scotland-connector:__subpackages__"],
)

go_test(
    name = "walks_test",
    srcs = ["walks_test.go"],
    embed = [":walks"],
)
