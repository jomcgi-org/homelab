load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "actorjwt",
    srcs = ["verifier.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/actorjwt",
    visibility = ["//visibility:public"],
)

go_test(
    name = "actorjwt_test",
    srcs = ["verifier_test.go"],
    embed = [":actorjwt"],
    deps = ["//projects/factory/broker/internal/fakes"],
)
