load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "bosun-libs_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/cmd/bosun-libs",
    visibility = ["//visibility:private"],
    deps = ["//projects/factory/discord/artifacts"],
)

go_binary(
    name = "bosun-libs",
    embed = [":bosun-libs_lib"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)
