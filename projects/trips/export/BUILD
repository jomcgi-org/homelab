load("@rules_go//go:def.bzl", "go_binary", "go_library")

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
