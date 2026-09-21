load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "snapshotmeta",
    srcs = ["device.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/snapshotmeta",
    visibility = ["//projects/embervm/noded:__subpackages__"],
)

go_test(
    name = "snapshotmeta_test",
    srcs = ["device_test.go"],
    embed = [":snapshotmeta"],
)
