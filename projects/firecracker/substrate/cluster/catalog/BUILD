load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "catalog",
    srcs = ["config.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/catalog",
    visibility = ["//projects/firecracker/substrate:__subpackages__"],
    deps = ["//projects/firecracker/substrate/substrate"],
)

go_test(
    name = "catalog_test",
    srcs = ["config_test.go"],
    embed = [":catalog"],
)
