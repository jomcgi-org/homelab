load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "notify",
    srcs = ["client.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/notify",
    visibility = ["//visibility:public"],
)

go_test(
    name = "notify_test",
    srcs = ["client_test.go"],
    embed = [":notify"],
)
