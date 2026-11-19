load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "usage",
    srcs = [
        "reviewer.go",
        "store.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/usage",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/broker/config",
        "//projects/factory/broker/relay",
        "//projects/factory/connector/provenance",
    ],
)
