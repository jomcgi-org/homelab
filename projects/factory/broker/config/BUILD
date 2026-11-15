load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "config",
    srcs = ["config.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/config",
    visibility = ["//visibility:public"],
    deps = ["@io_k8s_sigs_yaml//:yaml"],
)

go_test(
    name = "config_test",
    srcs = ["config_test.go"],
    embed = [":config"],
)
