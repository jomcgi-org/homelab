load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "quota",
    srcs = ["quota.go"],
    importpath = "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota",
    visibility = ["//visibility:public"],
)

go_test(
    name = "quota_test",
    srcs = ["quota_test.go"],
    embed = [":quota"],
)
