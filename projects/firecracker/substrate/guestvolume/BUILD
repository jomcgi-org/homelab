load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "guestvolume",
    srcs = ["volume_linux.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/guestvolume",
    visibility = ["//visibility:public"],
)

go_test(
    name = "guestvolume_test",
    srcs = ["volume_linux_test.go"],
    embed = [":guestvolume"],
)
