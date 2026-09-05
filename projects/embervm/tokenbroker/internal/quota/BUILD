load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "quota",
    srcs = ["quota.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/quota",
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
)

go_test(
    name = "quota_test",
    srcs = ["quota_test.go"],
    embed = [":quota"],
)
