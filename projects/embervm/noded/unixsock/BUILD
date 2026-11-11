load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "unixsock",
    srcs = ["unixsock.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/unixsock",
    visibility = ["//projects/embervm/noded:__subpackages__"],
)

go_test(
    name = "unixsock_test",
    srcs = ["unixsock_linux_test.go"],
    embed = [":unixsock"],
)
