load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "verdict",
    srcs = ["verdict.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict",
    visibility = ["//projects/scotland-connector:__subpackages__"],
)

go_test(
    name = "verdict_test",
    srcs = ["verdict_test.go"],
    embed = [":verdict"],
)
