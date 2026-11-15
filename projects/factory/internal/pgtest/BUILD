load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "pgtest",
    testonly = True,
    srcs = ["pgtest.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/internal/pgtest",
    visibility = ["//projects/factory:__subpackages__"],
    deps = ["@com_github_lib_pq//:pq"],
)
