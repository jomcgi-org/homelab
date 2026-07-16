load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "volume",
    srcs = [
        "stat_other.go",
        "stat_unix.go",
        "volume.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/volume",
    visibility = ["//visibility:public"],
)

go_test(
    name = "volume_test",
    srcs = ["volume_test.go"],
    embed = [":volume"],
)
