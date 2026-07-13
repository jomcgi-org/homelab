load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "config",
    srcs = ["config.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/config",
    visibility = ["//projects/embervm/noded:__subpackages__"],
)

go_test(
    name = "config_test",
    srcs = ["config_test.go"],
    embed = [":config"],
)
