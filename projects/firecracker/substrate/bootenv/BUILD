load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "bootenv",
    srcs = ["codec.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/bootenv",
    visibility = ["//visibility:public"],
)

go_test(
    name = "bootenv_test",
    srcs = ["codec_test.go"],
    embed = [":bootenv"],
)
