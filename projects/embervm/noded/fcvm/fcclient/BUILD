load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "fcclient",
    srcs = ["client.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient",
    visibility = ["//projects/embervm/noded:__subpackages__"],
)

go_test(
    name = "fcclient_test",
    srcs = ["client_test.go"],
    embed = [":fcclient"],
)
