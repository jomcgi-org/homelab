load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "buildbuddy",
    srcs = ["buildbuddy.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/buildbuddy",
    visibility = ["//visibility:public"],
)

go_test(
    name = "buildbuddy_test",
    srcs = ["buildbuddy_test.go"],
    embed = [":buildbuddy"],
)
