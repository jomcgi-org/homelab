load("@rules_go//go:def.bzl", "go_binary", "go_library")

# publish_test runs the maintenance grants against an older database.
exports_files(["maintenance.sql"])

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    embedsrcs = ["maintenance.sql"],
    importpath = "github.com/jomcgi/homelab/projects/ships/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/ships/publish",
        "@com_github_lib_pq//:pq",
        "@io_nhooyr_websocket//:websocket",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)
