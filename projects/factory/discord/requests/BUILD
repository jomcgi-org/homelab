load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "requests",
    srcs = [
        "mcp.go",
        "replies.go",
        "store.go",
        "turns.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/requests",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "@com_github_lib_pq//:pq",
    ],
)

go_test(
    name = "requests_test",
    srcs = ["requests_test.go"],
    data = ["@postgres_test//:postgres"],
    embed = [":requests"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/connector/internal/pgtest",
        "@com_github_lib_pq//:pq",
    ],
)
