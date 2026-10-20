load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "astro",
    srcs = ["moon.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/astro",
    visibility = ["//projects/scotland-connector:__subpackages__"],
)

go_test(
    name = "astro_test",
    srcs = ["moon_test.go"],
    embed = [":astro"],
)
