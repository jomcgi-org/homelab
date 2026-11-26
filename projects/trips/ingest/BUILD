load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "ingest_lib",
    srcs = [
        "jpeg.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/trips/ingest",
    visibility = ["//visibility:private"],
    deps = ["@com_github_lib_pq//:pq"],
)

go_binary(
    name = "ingest",
    embed = [":ingest_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "ingest_test",
    srcs = [
        "ingest_test.go",
        "jpeg_test.go",
    ],
    data = [
        "testdata/geotagged.jpg",
        "testdata/no_gps.jpg",
        "//projects/trips:approvals.sql",
        "//projects/trips:hub_schema.sql",
        "@postgres_test//:postgres",
    ],
    embed = [":ingest_lib"],
    deps = [
        "//projects/factory/connector/internal/pgtest",
        "@com_github_lib_pq//:pq",
    ],
)
