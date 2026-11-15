load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "pgtest",
    testonly = True,
    srcs = ["pgtest.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest",
    visibility = ["//projects/factory/connector:__subpackages__"],
    deps = [
        "//projects/factory/connector/migrations",
        "@com_github_lib_pq//:pq",
    ],
)
