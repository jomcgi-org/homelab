load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "usage",
    srcs = ["store.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/usage",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/broker/relay",
    ],
)
