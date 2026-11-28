load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "archive",
    srcs = [
        "mcp.go",
        "read.go",
        "sql.go",
        "store.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/archive",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "@com_github_lib_pq//:pq",
    ],
)

go_test(
    name = "archive_test",
    srcs = [
        "archive_test.go",
        "sql_test.go",
    ],
    data = ["@postgres_test//:postgres"],
    embed = [":archive"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/connector/internal/pgtest",
        "@com_github_lib_pq//:pq",
    ],
)
