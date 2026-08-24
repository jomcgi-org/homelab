load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "refresh",
    srcs = ["refresher.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/mirror/refresh",
    visibility = ["//projects/embervm/mirror:__subpackages__"],
)

go_test(
    name = "refresh_test",
    srcs = ["refresher_test.go"],
    embed = [":refresh"],
)
