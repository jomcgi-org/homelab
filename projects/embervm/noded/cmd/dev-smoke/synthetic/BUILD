# gazelle:ignore
load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "synthetic",
    srcs = ["scan.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/cmd/dev-smoke/synthetic",
    visibility = ["//projects/embervm/noded/cmd/dev-smoke:__subpackages__"],
)

go_test(
    name = "synthetic_test",
    srcs = ["scan_test.go"],
    embed = [":synthetic"],
)
