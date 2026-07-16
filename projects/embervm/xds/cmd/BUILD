load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/xds/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/xds/server",
        "@org_golang_google_grpc//:grpc",
    ],
)

go_binary(
    name = "xds",
    embed = [":cmd_lib"],
    visibility = ["//projects/embervm/xds:__subpackages__"],
)
