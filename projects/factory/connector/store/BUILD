load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "store",
    srcs = [
        "delivery.go",
        "recovery.go",
        "store.go",
        "transcript.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/store",
    visibility = ["//visibility:public"],
    deps = ["@com_github_lib_pq//:pq"],
)

go_test(
    name = "store_test",
    srcs = [
        "recovery_test.go",
        "store_test.go",
        "transcript_test.go",
    ],
    data = ["@postgres_test//:postgres"],
    deps = [
        ":store",
        "//projects/factory/connector/internal/pgtest",
        "//projects/factory/connector/migrations",
    ],
)
