load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "reconnect",
    srcs = ["reconnect.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/goosecracker/guest-init/internal/reconnect",
    visibility = ["//projects/firecracker/goosecracker/guest-init:__subpackages__"],
)

go_test(
    name = "reconnect_test",
    srcs = ["reconnect_test.go"],
    embed = [":reconnect"],
)
