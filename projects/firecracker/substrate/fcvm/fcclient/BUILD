load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "fcclient",
    srcs = ["client.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/fcvm/fcclient",
    visibility = ["//visibility:public"],
)

go_test(
    name = "fcclient_test",
    srcs = ["client_test.go"],
    embed = [":fcclient"],
)
