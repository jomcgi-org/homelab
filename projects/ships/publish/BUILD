load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "publish",
    srcs = [
        "gc.go",
        "products.go",
        "publish.go",
        "source.go",
        "store.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/ships/publish",
    visibility = ["//visibility:public"],
    deps = ["@com_github_lib_pq//:pq"],
)

go_test(
    name = "publish_test",
    srcs = ["publish_test.go"],
    embed = [":publish"],
)
