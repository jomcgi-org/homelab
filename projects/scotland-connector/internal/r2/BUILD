load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "r2",
    srcs = ["client.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/r2",
    visibility = ["//projects/scotland-connector:__subpackages__"],
)

go_test(
    name = "r2_test",
    srcs = ["client_test.go"],
    embed = [":r2"],
)
