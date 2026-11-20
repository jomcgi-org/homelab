load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "export_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/trips/export",
    visibility = ["//visibility:private"],
    deps = ["@com_github_lib_pq//:pq"],
)

go_binary(
    name = "export",
    embed = [":export_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "export_test",
    srcs = ["export_test.go"],
    data = [
        "//projects/monolith/chart:migrations/20260615120000_trips_schema.sql",
        "//projects/trips:approvals.sql",
        "@postgres_test//:postgres",
    ],
    embed = [":export_lib"],
    deps = ["//projects/factory/connector/internal/pgtest"],
)
