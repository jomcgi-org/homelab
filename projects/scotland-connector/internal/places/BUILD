load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "places",
    srcs = ["places.go"],
    embedsrcs = ["seed.json"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/places",
    visibility = ["//projects/scotland-connector:__subpackages__"],
)

go_test(
    name = "places_test",
    srcs = ["places_test.go"],
    embed = [":places"],
)
